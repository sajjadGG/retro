from __future__ import annotations

from retro.renderer import _short_json


def test_short_json_bounds_large_nested_payloads():
    payload = {
        "output": "x" * 1_000_000,
        "items": [{"value": "y" * 100_000} for _ in range(200)],
    }

    rendered = _short_json(payload, indent=2, limit=2000)

    assert len(rendered) <= 2000
    assert rendered.endswith("…")


def test_short_json_preserves_small_payload():
    assert _short_json({"ok": True, "value": "small"}) == (
        '{"ok": true, "value": "small"}'
    )
