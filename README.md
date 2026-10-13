# mcp-secret

Indexes your documents locally and lets an AI agent search them over MCP, **pseudonymized
only**: names, tax codes, IBANs, emails, phones become tokens like `[PERSONA_3]`, the same
token for the same value in every document. The agent sees no file names and no file metadata
(title, author, dates). Emails keep their From/To/Cc/Subject lines: they are text, masked like
the body, so whatever detection misses there is published like a miss in the body.

- `ingest` (no network) parses, detects PII, writes the token map to `private/private.db` and
  the pseudonymized chunks to `public/public.db`.
- `serve` (MCP over stdio) opens only `public/public.db`, read-only, inside `sandbox-exec`: no
  network, no programs but Python itself, no system services (mach-lookup), no reads of your
  home folder, `/Users`, `/Volumes` or the temp folders except `public/` and the Python runtime,
  no file writes anywhere, no peeking at or signalling other processes. It refuses to start if
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
uv run mcp-secret list                          # indexed files: changed or missing ones flagged
uv run mcp-secret review 3f9c0a1b2c3d4e5f       # the text in clear, what is masked, which token
uv run mcp-secret deny add "Mario Rossi"        # always mask it (`allow add`: never), then ingest
```

`ingest` and `list` print each file's full path, and `review` the document's text, in clear, to
your terminal: run them yourself, not through an agent whose output leaves the machine (a cloud
agent would send the paths and the text along).

Formats: PDF, DOCX, XLSX, PPTX, HTML, MD, images (OCR), EML, TXT. Unchanged files are skipped;
changed files replace their chunks; files deleted or moved out of an ingested folder lose theirs.

The data root is `~/Library/Application Support/mcp-secret` (override with `MCP_SECRET_HOME`):

| File | Holds |
| --- | --- |
| `private/private.db` | file paths, the value → token map, where the detector found values (offsets, no text); 0600, never opened by `serve` |
| `private/deny.txt` | values to always mask, one per line (masked as `PERSONA`) |
| `private/allow.txt` | values never to mask, one per line (applies to documents indexed afterwards: `ingest --force`) |
| `public/public.db` | pseudonymized chunks + full-text index, nothing else |

`show` reveals a missed value? `deny add` it (or add a line to `deny.txt`) and run `ingest`
again: it is masked in new documents and scrubbed from those already indexed.

Every command takes `--json` (before the command) and then prints one JSON object per line:
the contract a UI builds on, in [docs/cli-json.md](docs/cli-json.md).

## Residual risks

- Detection recall is not 1.0: a missed value is published in clear. Check documents with
  `show` and add what slipped through to `deny.txt`.
- The metrics in [fixtures/README.md](fixtures/README.md) come from Faker templates and
  overestimate real recall. Before trusting it with sensitive files, annotate about ten of your
  own documents by hand and measure on them, locally; never commit them.
- Known detector limits, left to that hand check: name parts in lowercase, OCR-garbled values,
  surname columns of plain CSV files, UK National Insurance and Turkish TC Kimlik numbers,
  foreign street lines, label phrases the model tags as places, 8-digit staff numbers in birth
  tables, `/@word` URL paths, `Vite,0,0312345678`, an 8-digit landline after a number with a
  comma (`Bianchi 7,03456789` reads as an amount).
- The sandbox cannot hide `sysctl KERN_PROCARGS2` (no sandbox rule gates it): a compromised
  `serve` could read the command lines of your other processes, and the environment of those
  without hardened runtime, and return them to the agent. Keep secrets out of the environment
  of the app that launches it.

## Claude Desktop

`uv run mcp-secret desktop-config --install` adds the server to Claude Desktop's config (the
old file is kept as `.bak`; `--uninstall` removes it again), then restart Claude. By hand: add to
`~/Library/Application Support/Claude/claude_desktop_config.json`, then restart Claude:

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

## UI

A macOS app (SwiftUI, macOS 15+) for personal use from this repo: no bundle, no signing. It
only runs the CLI with `--json` ([docs/cli-json.md](docs/cli-json.md)); it never opens the
databases or the deny/allow lists, never starts `serve`, never loads a model.

```sh
swift run --package-path apps/macos MCPSecret
```

- Drop files and folders on the sidebar (or ⌘O) and watch them index; each document shows its
  state: indexed, unchanged, error, file missing, changed since indexing.
- A document opens in two columns: the original in clear, each masked value tinted by type and
  tagged with its token (legend on top), and what the agent sees. Select a value to light up
  its token on the right; ⌘[ and ⌘] step through them. One core process keeps the models loaded
  for the session (about 2.5 GB of memory): it starts with the app, then each document opens in
  well under a second.
- Select text that slipped through and choose Always Mask (⇧⌘M). Select a value that should not
  be masked and choose Don't Mask (⇧⌘U); other documents keep it masked until Re-index All
  (⇧⌘R).
- Settings (⌘,): CLI path and data root for the session (`-cliPath`, `-dataRoot` launch
  arguments preset them), model download, Claude Desktop install and removal.

Document text, values and paths stay in the app's memory: no caches, logs, temporary files or
restored windows. Italian or English, following the system (`-AppleLanguages "(en)"` forces
English). Tests: `swift test --package-path apps/macos` (one of them drives the real CLI);
`SCREENSHOTS=<dir>` in front of it, with `--filter screenshots`, draws the main screens in light
and dark.

## Develop

```sh
uv run ruff check && uv run pytest    # needs `mcp-secret models` once
```

`tests/test_leak.py` hides canaries in every corner of a DOCX, PDF, PNG, XLSX, EML and HTML,
ingests them, drives `serve` through a real MCP client and checks that no canary reaches
`public.db` or tool output; it also probes the sandbox and kills an ingest mid-transaction.
