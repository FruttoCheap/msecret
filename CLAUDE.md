# mcp-secret

Spec: PLAN.md. Lean on purpose (ponytail): build only what the plan asks.

## Context
Local macOS app: indexes the user's documents and exposes them to an AI agent through
an MCP server, ONLY pseudonymized. The only data leaving the machine is pseudonymized
text returned by MCP tools.

## Invariant (non-negotiable)
- public/public.db holds only pseudonymized text. No paths, titles, metadata, or vectors
  computed from clear text.
- The MCP process (`serve`) opens only public/public.db, read-only, under sandbox-exec:
  no network, no reads of $HOME except public/ and the Python runtime.
- No path exists from a token back to its real value, not even for queries.
Any choice weakening one of these is reported, not implemented.

## Rules
- One Python package `mcp_secret`, Python 3.12, uv. Flat modules. No Protocol with one
  implementation, no fakes where the real code works.
- Never real personal data: generated fixtures only.
- Logs carry types, counts, offsets. Never text or values.
- Offline at runtime (HF_HUB_OFFLINE=1); models are fetched by an explicit command.
- Code, comments, commits in English, Conventional Commits.
- `uv run ruff check && uv run pytest` green before every commit.

## Contract
`mcp_secret.pii.detect(text: str) -> list[Span]` — sorted, non-overlapping spans,
character offsets on NFC text. `pii.fetch_models()` downloads detector weights
(called by `mcp-secret models`). pii.py and fixtures/ belong to the PII work; everything
else to the pipeline work.

## Stack
Docling (OcrMac for images/scans), stdlib email for EML, GLiNER for names/orgs/places,
own regex+checksums for CF/P.IVA/IBAN/email/phone, stdlib sqlite3 with FTS5,
official MCP SDK (FastMCP, stdio).
