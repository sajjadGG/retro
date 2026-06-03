"""SQLite index over flat-file memory sources."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import Layout
from .utils import iter_jsonl

SCHEMA_VERSION = 1
RRF_K = 60
WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


@dataclass(frozen=True)
class ReindexReport:
    indexed: int
    source_records: int
    mined_records: int
    evidence_refs: int
    links: int


@dataclass(frozen=True)
class AuthoredImportReport:
    imported: int
    skipped: int


@dataclass(frozen=True)
class UtilityUpdateReport:
    memory_id: str
    old_q_value: float
    new_q_value: float
    hits: int
    successes: int
    failures: int


@dataclass(frozen=True)
class DoctorReport:
    memory_count: int
    counts_by_status: dict[str, int]
    counts_by_scope: dict[str, int]
    counts_by_kind: dict[str, int]
    dangling_links: int
    sqlite_vec: bool


@dataclass(frozen=True)
class RetrievedMemory:
    id: str
    kind: str
    scope: str
    status: str
    text: str
    when_to_use: str
    origin_repo: str | None
    score: float
    rank: int


@dataclass(frozen=True)
class WeaveResult:
    query: str
    memories: list[RetrievedMemory]

    def to_markdown(self) -> str:
        if not self.memories:
            return ""
        lines = ["## Relevant Memory", ""]
        for memory in self.memories:
            lines.append(f"- [{memory.kind}/{memory.scope}] {memory.text}")
            if memory.when_to_use:
                lines.append(f"  Use when: {memory.when_to_use}")
        return "\n".join(lines)


def init(layout: Layout) -> None:
    layout.ensure()
    _connect(layout).close()


def reindex(layout: Layout) -> ReindexReport:
    layout.ensure()
    db_path = layout.memory_index_path()
    for path in (db_path, db_path.with_suffix(".sqlite-wal"), db_path.with_suffix(".sqlite-shm")):
        if path.exists():
            path.unlink()
    con = _connect(layout)
    source_records = 0
    mined_records = 0
    evidence_refs = 0
    links = 0
    try:
        with con:
            for record in _iter_source_records(layout):
                source_records += 1
                evidence_refs += _upsert_memory(con, record)
            for record in _iter_mined_records(layout):
                mined_records += 1
                evidence_refs += _upsert_memory(con, record)
            for event in _iter_memory_events(layout):
                _record_memory_event(con, event)
            links = _refresh_links(con)
            _refresh_fts(con)
            indexed = con.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    finally:
        con.close()
    return ReindexReport(
        indexed=indexed,
        source_records=source_records,
        mined_records=mined_records,
        evidence_refs=evidence_refs,
        links=links,
    )


def retrieve(
    layout: Layout,
    query: str,
    *,
    cwd: str | None = None,
    limit: int = 10,
    include_candidates: bool = False,
) -> list[RetrievedMemory]:
    con = _connect(layout)
    try:
        rows = _keyword_rows(con, query=query, cwd=cwd, limit=max(limit * 4, 20))
        fused: dict[str, float] = {}
        for rank, row in enumerate(rows, start=1):
            fused[row["id"]] = fused.get(row["id"], 0.0) + 1.0 / (RRF_K + rank)

        if not fused:
            return []

        _expand_linked_cluster(con, fused)

        placeholders = ",".join("?" for _ in fused)
        status_filter = "" if include_candidates else "AND status = 'accepted'"
        records = con.execute(
            f"""
            SELECT id, kind, scope, status, text, when_to_use, origin_repo,
                   q_value, quality_avg, risk, last_used_at
            FROM memory
            WHERE id IN ({placeholders}) {status_filter}
            """,
            list(fused),
        ).fetchall()

        ranked = []
        max_recall = max(fused.values()) if fused else 1.0
        for row in records:
            scope_match = _scope_match(row["scope"], row["origin_repo"], cwd)
            risk_penalty = {"low": 0.0, "medium": 0.05, "high": 0.2}.get(row["risk"], 0.05)
            semantic_score = fused[row["id"]] / max_recall
            recency_score = _recency_score(row["last_used_at"])
            score = (semantic_score * 0.45) + (row["q_value"] * 0.35)
            score += (row["quality_avg"] * 0.10) + (recency_score * 0.05)
            score += (scope_match * 0.05) - risk_penalty
            ranked.append((score, row))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [
            RetrievedMemory(
                id=row["id"],
                kind=row["kind"],
                scope=row["scope"],
                status=row["status"],
                text=row["text"],
                when_to_use=row["when_to_use"],
                origin_repo=row["origin_repo"],
                score=score,
                rank=i,
            )
            for i, (score, row) in enumerate(ranked[:limit], start=1)
        ]
    finally:
        con.close()


def weave(
    layout: Layout,
    query: str,
    *,
    cwd: str | None = None,
    limit: int = 6,
    include_candidates: bool = True,
) -> WeaveResult:
    return WeaveResult(
        query=query,
        memories=retrieve(
            layout,
            query,
            cwd=cwd,
            limit=limit,
            include_candidates=include_candidates,
        ),
    )


def doctor(layout: Layout) -> DoctorReport:
    con = _connect(layout)
    try:
        memory_count = con.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        dangling_links = con.execute(
            """
            SELECT COUNT(*)
            FROM memory_link l
            LEFT JOIN memory m ON m.id = l.dst_slug
            WHERE m.id IS NULL
            """
        ).fetchone()[0]
        return DoctorReport(
            memory_count=memory_count,
            counts_by_status=_counts(con, "status"),
            counts_by_scope=_counts(con, "scope"),
            counts_by_kind=_counts(con, "kind"),
            dangling_links=dangling_links,
            sqlite_vec=_sqlite_vec_available(con),
        )
    finally:
        con.close()


def append_memory(layout: Layout, record: dict[str, Any]) -> str:
    layout.ensure()
    normalized = _normalize_record(record, source="mined")
    item_path = layout.memory_items_path()
    with item_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(normalized, ensure_ascii=False, sort_keys=True) + "\n")
    con = _connect(layout)
    try:
        with con:
            _upsert_memory(con, normalized)
            _refresh_links(con)
            _refresh_fts(con)
    finally:
        con.close()
    return normalized["id"]


def update_utility(
    layout: Layout,
    memory_id: str,
    reward: float,
    *,
    session_id: str | None = None,
    reason: str | None = None,
) -> UtilityUpdateReport:
    layout.ensure()
    reward = max(0.0, min(1.0, reward))
    event = {
        "memory_id": memory_id,
        "event": "memory_rewarded",
        "session_id": session_id,
        "reward": reward,
        "reason": reason,
        "created_at": _now(),
    }
    with layout.memory_events_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    con = _connect(layout)
    try:
        with con:
            row = con.execute(
                "SELECT q_value, hits, successes, failures FROM memory WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise KeyError(memory_id)
            _record_memory_event(con, event)
            updated = con.execute(
                "SELECT q_value, hits, successes, failures FROM memory WHERE id = ?",
                (memory_id,),
            ).fetchone()
            return UtilityUpdateReport(
                memory_id=memory_id,
                old_q_value=row["q_value"],
                new_q_value=updated["q_value"],
                hits=updated["hits"],
                successes=updated["successes"],
                failures=updated["failures"],
            )
    finally:
        con.close()


def import_authored(layout: Layout, directory: Path) -> AuthoredImportReport:
    layout.ensure()
    imported = 0
    skipped = 0
    for path in sorted(directory.rglob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            skipped += 1
            continue
        frontmatter, body = _split_frontmatter(raw)
        if not body.strip():
            skipped += 1
            continue
        record = {
            "id": str(frontmatter.get("id") or path.stem),
            "source": "authored",
            "kind": str(frontmatter.get("kind") or "case"),
            "scope": str(frontmatter.get("scope") or "user"),
            "status": str(frontmatter.get("status") or "accepted"),
            "text": body.strip(),
            "when_to_use": str(frontmatter.get("when_to_use") or frontmatter.get("when") or ""),
            "origin_repo": frontmatter.get("origin_repo"),
            "confidence": float(frontmatter.get("confidence") or 0.8),
            "priority": int(frontmatter.get("priority") or 3),
            "risk": str(frontmatter.get("risk") or "medium"),
            "structured": {"frontmatter": frontmatter, "path": str(path)},
        }
        append_memory(layout, record)
        imported += 1
    return AuthoredImportReport(imported=imported, skipped=skipped)


def _connect(layout: Layout) -> sqlite3.Connection:
    layout.memories_dir().mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(layout.memory_index_path())
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        _create_schema(con)
    return con


def _create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        DROP TABLE IF EXISTS memory_fts;
        DROP TABLE IF EXISTS memory_vec;
        DROP TABLE IF EXISTS memory_event;
        DROP TABLE IF EXISTS memory_link;
        DROP TABLE IF EXISTS memory_evidence;
        DROP TABLE IF EXISTS memory;

        CREATE TABLE memory (
          id            TEXT PRIMARY KEY,
          source        TEXT NOT NULL,
          kind          TEXT NOT NULL,
          scope         TEXT NOT NULL,
          status        TEXT NOT NULL DEFAULT 'candidate',
          text          TEXT NOT NULL,
          when_to_use   TEXT DEFAULT '',
          origin_repo   TEXT,
          confidence    REAL DEFAULT 0.5,
          priority      INTEGER DEFAULT 3,
          risk          TEXT DEFAULT 'medium',
          q_value       REAL DEFAULT 0.5,
          hits          INTEGER DEFAULT 0,
          successes     INTEGER DEFAULT 0,
          failures      INTEGER DEFAULT 0,
          quality_avg   REAL DEFAULT 0.0,
          structured    TEXT,
          content_hash  TEXT NOT NULL,
          created_at    TEXT NOT NULL,
          updated_at    TEXT NOT NULL,
          last_used_at  TEXT
        );
        CREATE INDEX idx_memory_scope_repo ON memory(scope, origin_repo, status);
        CREATE INDEX idx_memory_kind ON memory(kind, status);
        CREATE UNIQUE INDEX idx_memory_hash ON memory(content_hash);

        CREATE VIRTUAL TABLE memory_fts USING fts5(
          text, when_to_use, content='memory', content_rowid='rowid'
        );

        CREATE TABLE memory_evidence (
          memory_id  TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
          ref        TEXT NOT NULL,
          PRIMARY KEY (memory_id, ref)
        );
        CREATE INDEX idx_evidence_mem ON memory_evidence(memory_id);

        CREATE TABLE memory_link (
          src_id   TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
          dst_slug TEXT NOT NULL,
          PRIMARY KEY (src_id, dst_slug)
        );
        CREATE INDEX idx_link_dst ON memory_link(dst_slug);

        CREATE TABLE memory_event (
          memory_id  TEXT NOT NULL,
          event      TEXT NOT NULL,
          session_id TEXT,
          reward     REAL,
          reason     TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX idx_event_mem ON memory_event(memory_id, created_at);

        CREATE TABLE memory_vec (
          memory_id  TEXT PRIMARY KEY REFERENCES memory(id) ON DELETE CASCADE,
          dim        INTEGER NOT NULL,
          vec        BLOB NOT NULL,
          model      TEXT NOT NULL
        );

        PRAGMA user_version = 1;
        """
    )
    con.commit()


def _iter_source_records(layout: Layout) -> list[dict[str, Any]]:
    path = layout.memory_items_path()
    if not path.exists():
        return []
    records = []
    for _, raw in iter_jsonl(path):
        if "candidates" in raw:
            records.extend(_records_from_mining_result(raw))
        else:
            records.append(_normalize_record(raw, source=raw.get("source", "mined")))
    return records


def _iter_mined_records(layout: Layout) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    mined_dir = layout.root / "mined"
    if not mined_dir.exists():
        return records
    for path in sorted(mined_dir.glob("*/*/*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records.extend(_records_from_mining_result(raw))
    return records


def _iter_memory_events(layout: Layout) -> list[dict[str, Any]]:
    path = layout.memory_events_path()
    if not path.exists():
        return []
    return [raw for _, raw in iter_jsonl(path)]


def _records_from_mining_result(raw: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for candidate in raw.get("candidates") or []:
        merged = dict(candidate)
        merged.setdefault("source", "mined")
        merged.setdefault("status", "candidate")
        merged.setdefault("method", raw.get("method", merged.get("method", "")))
        merged.setdefault("host", raw.get("host"))
        merged.setdefault("session_id", raw.get("session_id"))
        records.append(_normalize_record(merged, source="mined"))
    return records


def _normalize_record(record: dict[str, Any], *, source: str) -> dict[str, Any]:
    now = _now()
    text = str(record.get("text") or "")
    scope = str(record.get("scope") or "repo")
    kind = str(record.get("kind") or "case")
    origin_repo = record.get("origin_repo")
    origin = str(origin_repo) if origin_repo else None
    normalized = {
        "id": str(record.get("id") or _slug(text)),
        "source": str(record.get("source") or source),
        "kind": kind,
        "scope": scope,
        "status": _safe_status(str(record.get("status") or "candidate"), text),
        "text": text,
        "when_to_use": str(record.get("when_to_use") or ""),
        "origin_repo": origin,
        "confidence": float(record.get("confidence") or 0.5),
        "priority": int(record.get("priority") or 3),
        "risk": str(record.get("risk") or "medium"),
        "q_value": float(record.get("q_value") or 0.5),
        "hits": int(record.get("hits") or 0),
        "successes": int(record.get("successes") or 0),
        "failures": int(record.get("failures") or 0),
        "quality_avg": float(record.get("quality_avg") or 0.0),
        "structured": record.get("structured"),
        "content_hash": str(record.get("content_hash") or _content_hash(text, scope, kind, origin)),
        "created_at": str(record.get("created_at") or now),
        "updated_at": str(record.get("updated_at") or now),
        "last_used_at": record.get("last_used_at"),
        "evidence_refs": list(record.get("evidence_refs") or []),
    }
    return normalized


def _record_memory_event(con: sqlite3.Connection, event: dict[str, Any]) -> None:
    memory_id = str(event.get("memory_id") or "")
    event_name = str(event.get("event") or "")
    created_at = str(event.get("created_at") or _now())
    reward = event.get("reward")
    reward_value = float(reward) if reward is not None else None
    con.execute(
        """
        INSERT INTO memory_event(memory_id, event, session_id, reward, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            event_name,
            event.get("session_id"),
            reward_value,
            event.get("reason"),
            created_at,
        ),
    )
    if event_name != "memory_rewarded" or reward_value is None:
        return
    row = con.execute(
        "SELECT q_value, hits, successes, failures FROM memory WHERE id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        return
    q_value = row["q_value"] + 0.2 * (reward_value - row["q_value"])
    successes = row["successes"] + (1 if reward_value >= 0.5 else 0)
    failures = row["failures"] + (1 if reward_value < 0.5 else 0)
    con.execute(
        """
        UPDATE memory
        SET q_value = ?, hits = ?, successes = ?, failures = ?, last_used_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (q_value, row["hits"] + 1, successes, failures, created_at, created_at, memory_id),
    )


def _upsert_memory(con: sqlite3.Connection, record: dict[str, Any]) -> int:
    existing = con.execute(
        "SELECT id FROM memory WHERE content_hash = ?",
        (record["content_hash"],),
    ).fetchone()
    if existing and existing["id"] != record["id"]:
        record = dict(record)
        record["id"] = existing["id"]

    con.execute(
        """
        INSERT INTO memory (
          id, source, kind, scope, status, text, when_to_use, origin_repo,
          confidence, priority, risk, q_value, hits, successes, failures,
          quality_avg, structured, content_hash, created_at, updated_at,
          last_used_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          source=excluded.source,
          kind=excluded.kind,
          scope=excluded.scope,
          status=excluded.status,
          text=excluded.text,
          when_to_use=excluded.when_to_use,
          origin_repo=excluded.origin_repo,
          confidence=excluded.confidence,
          priority=excluded.priority,
          risk=excluded.risk,
          q_value=excluded.q_value,
          hits=excluded.hits,
          successes=excluded.successes,
          failures=excluded.failures,
          quality_avg=excluded.quality_avg,
          structured=excluded.structured,
          content_hash=excluded.content_hash,
          updated_at=excluded.updated_at,
          last_used_at=excluded.last_used_at
        """,
        (
            record["id"],
            record["source"],
            record["kind"],
            record["scope"],
            record["status"],
            record["text"],
            record["when_to_use"],
            record["origin_repo"],
            record["confidence"],
            record["priority"],
            record["risk"],
            record["q_value"],
            record["hits"],
            record["successes"],
            record["failures"],
            record["quality_avg"],
            json.dumps(record["structured"], ensure_ascii=False, sort_keys=True)
            if record["structured"] is not None
            else None,
            record["content_hash"],
            record["created_at"],
            record["updated_at"],
            record["last_used_at"],
        ),
    )
    count = 0
    for ref in record["evidence_refs"]:
        con.execute(
            "INSERT OR IGNORE INTO memory_evidence(memory_id, ref) VALUES (?, ?)",
            (record["id"], str(ref)),
        )
        count += 1
    return count


def _refresh_links(con: sqlite3.Connection) -> int:
    con.execute("DELETE FROM memory_link")
    count = 0
    rows = con.execute("SELECT id, text, when_to_use FROM memory").fetchall()
    for row in rows:
        for dst_slug in _wiki_links(f"{row['text']}\n{row['when_to_use']}"):
            con.execute(
                "INSERT OR IGNORE INTO memory_link(src_id, dst_slug) VALUES (?, ?)",
                (row["id"], dst_slug),
            )
            count += 1
    return count


def _refresh_fts(con: sqlite3.Connection) -> None:
    con.execute("INSERT INTO memory_fts(memory_fts) VALUES ('rebuild')")


def _keyword_rows(
    con: sqlite3.Connection,
    *,
    query: str,
    cwd: str | None,
    limit: int,
) -> list[sqlite3.Row]:
    match = _fts_match_query(query)
    scope_sql, params = _scope_sql(cwd)
    try:
        return con.execute(
            f"""
            SELECT m.id, bm25(memory_fts) AS bm25_score
            FROM memory_fts
            JOIN memory m ON m.rowid = memory_fts.rowid
            WHERE memory_fts MATCH ?
              AND ({scope_sql})
            ORDER BY bm25_score ASC
            LIMIT ?
            """,
            [match, *params, limit],
        ).fetchall()
    except sqlite3.OperationalError:
        quoted = '"' + query.replace('"', '""') + '"'
        return con.execute(
            f"""
            SELECT m.id, bm25(memory_fts) AS bm25_score
            FROM memory_fts
            JOIN memory m ON m.rowid = memory_fts.rowid
            WHERE memory_fts MATCH ?
              AND ({scope_sql})
            ORDER BY bm25_score ASC
            LIMIT ?
            """,
            [quoted, *params, limit],
        ).fetchall()


def _expand_linked_cluster(con: sqlite3.Connection, fused: dict[str, float]) -> None:
    ids = list(fused)
    placeholders = ",".join("?" for _ in ids)
    rows = con.execute(
        f"""
        SELECT dst.id AS id
        FROM memory_link l
        JOIN memory dst ON dst.id = l.dst_slug
        WHERE l.src_id IN ({placeholders})
        UNION
        SELECT src.id AS id
        FROM memory_link l
        JOIN memory src ON src.id = l.src_id
        WHERE l.dst_slug IN ({placeholders})
        """,
        [*ids, *ids],
    ).fetchall()
    for row in rows:
        fused.setdefault(row["id"], 1.0 / (RRF_K + len(fused) + 1))


def _scope_sql(cwd: str | None) -> tuple[str, list[str]]:
    if cwd:
        return "m.scope IN ('global', 'user', 'task') OR (m.scope = 'repo' AND m.origin_repo = ?)", [cwd]
    return "m.scope IN ('global', 'user', 'task') OR m.scope = 'repo'", []


def _scope_match(scope: str, origin_repo: str | None, cwd: str | None) -> float:
    if scope in {"global", "user"}:
        return 1.0
    if scope == "repo" and cwd and origin_repo == cwd:
        return 1.0
    if scope == "task":
        return 0.8
    return 0.4


def _recency_score(last_used_at: str | None) -> float:
    if not last_used_at:
        return 0.0
    try:
        then = datetime.fromisoformat(last_used_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    age_days = max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400)
    return 1.0 / (1.0 + age_days)


def _counts(con: sqlite3.Connection, column: str) -> dict[str, int]:
    rows = con.execute(f"SELECT {column}, COUNT(*) AS n FROM memory GROUP BY {column}").fetchall()
    return {row[0]: row[1] for row in rows}


def _sqlite_vec_available(con: sqlite3.Connection) -> bool:
    try:
        con.enable_load_extension(True)
    except (AttributeError, sqlite3.OperationalError):
        return False
    return False


def _fts_match_query(query: str) -> str:
    terms = re.findall(r"[\w./:-]+", query)
    if not terms:
        return '""'
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _wiki_links(text: str) -> list[str]:
    seen = set()
    links = []
    for match in WIKI_LINK_RE.finditer(text):
        slug = match.group(1).strip()
        if slug and slug not in seen:
            seen.add(slug)
            links.append(slug)
    return links


def _safe_status(status: str, text: str) -> str:
    if status != "accepted":
        return status
    lowered = text.lower()
    suspicious = (
        "ignore previous instructions",
        "disregard previous instructions",
        "system prompt",
        "api_key",
        "secret_key",
        "password=",
        "-----begin private key-----",
    )
    if any(marker in lowered for marker in suspicious):
        return "needs_review"
    if any(_is_invisible_control(ch) for ch in text):
        return "needs_review"
    return status


def _is_invisible_control(ch: str) -> bool:
    codepoint = ord(ch)
    return codepoint in {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF}


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---", 4)
    if end == -1:
        return {}, raw
    fm_raw = raw[4:end]
    body = raw[end + 4 :].lstrip("\n")
    frontmatter: dict[str, Any] = {}
    for line in fm_raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = _parse_scalar(value.strip())
    return frontmatter, body


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _content_hash(text: str, scope: str, kind: str, origin_repo: str | None) -> str:
    normalized = " ".join(text.lower().split())
    origin = origin_repo if scope == "repo" else ""
    return hashlib.sha256(f"{scope}\0{kind}\0{origin}\0{normalized}".encode()).hexdigest()


def _slug(text: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{base or 'memory'}-{digest}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
