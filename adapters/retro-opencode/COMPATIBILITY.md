# OpenCode compatibility

Version `0.1.0` is intentionally conservative and fixture-backed.

## Supported

- JSONL files under `sessions/`.
- JSONL files under `projects/<project>/sessions/`.
- Session metadata in top-level records or nested `session` objects.
- User, assistant, and system messages.
- Tool calls and results.
- File-read, file-edit, command, reasoning, permission, and attachment events.
- Preservation of malformed, non-object, and unknown records.

## Not yet supported

- OpenCode databases or binary stores.
- Attachments and sidecars outside the source JSONL.
- Source layouts not represented by a sanitized fixture.

Add fixture coverage before expanding mappings.
