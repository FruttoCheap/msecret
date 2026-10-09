# mcp-secret

Indexes your documents locally and lets an AI agent search them over MCP, **pseudonymized
only**: names, tax codes, IBANs, emails, phones become tokens like `[PERSONA_3]`, the same
token for the same value in every document. The agent never sees a real value, a file name
or any metadata.

- `ingest` (no network) parses, detects PII, writes the token map to `private/private.db` and
  the pseudonymized chunks to `public/public.db`.
- `serve` (MCP over stdio) opens only `public/public.db`, read-only, inside `sandbox-exec`: no
  network, no reads of `$HOME` except `public/` and the Python runtime. It refuses to start if
  the sandbox is not active.

## Install

macOS, [uv](https://docs.astral.sh/uv/):

```sh
git clone <this repo> mcp_secret && cd mcp_secret
uv sync
uv run mcp-secret models     # one-time model download, the only command that uses the network
```

## Use

```sh
uv run mcp-secret ingest ~/Documents/clienti    # files or folders; re-run any time
# 3f9c0a1b2c3d4e5f	4 chunks	/Users/you/Documents/clienti/contratto.pdf
uv run mcp-secret show 3f9c0a1b2c3d4e5f         # exactly what the agent sees
```

Formats: PDF, DOCX, XLSX, PPTX, HTML, MD, images (OCR), EML, TXT. Unchanged files are skipped;
changed files replace their chunks; files deleted or moved out of an ingested folder lose theirs.

The data root is `~/Library/Application Support/mcp-secret` (override with `MCP_SECRET_HOME`):

| File | Holds |
| --- | --- |
| `private/private.db` | file paths and the value → token map (0600, never opened by `serve`) |
| `private/deny.txt` | values to always mask, one per line (masked as `PERSONA`) |
| `private/allow.txt` | values never to mask, one per line (applies to new ingests) |
| `public/public.db` | pseudonymized chunks + full-text index, nothing else |

`show` reveals a missed value? Add it to `deny.txt` and run `ingest` again: it is masked in
new documents and scrubbed from those already indexed.

## Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`, then restart Claude:

```json
{
  "mcpServers": {
    "mcp-secret": {
      "command": "/absolute/path/to/mcp_secret/.venv/bin/mcp-secret",
      "args": ["serve"]
    }
  }
}
```

Add `"env": {"MCP_SECRET_HOME": "/path/to/data"}` if you moved the data root.

Try it: *"List my sources, then search for the IBANs and tell me which person each belongs
to."* Answers come back as tokens (`[IBAN_1]` belongs to `[PERSONA_2]`); searching
`PERSONA_2` finds every document about that person.

## Develop

```sh
uv run ruff check && uv run pytest    # needs `mcp-secret models` once
```

`tests/test_leak.py` hides canaries in every corner of a DOCX, PDF, PNG, XLSX, EML and HTML,
ingests them, drives `serve` through a real MCP client and checks that no canary reaches
`public.db` or tool output; it also probes the sandbox and kills an ingest mid-transaction.
