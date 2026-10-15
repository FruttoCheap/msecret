# CLI JSON contract

A UI drives the core only by launching the CLI with `--json` and reading its stdout. It never
opens the databases, `deny.txt` or `allow.txt`, never launches `serve` and never loads models.

```sh
mcp-secret --json <command> [arguments]
```

## Launching

- Executable: `<repo>/.venv/bin/mcp-secret` (Windows: `<repo>\.venv\Scripts\mcp-secret.exe`).
- `--json` goes before the command.
- Data root: the `MCP_SECRET_HOME` environment variable, absolute; unset means the default
  (`~/Library/Application Support/mcp-secret`). `status` reports the one in use.
- A value that may start with `-` goes after `--`: `deny add -- "-value"`.
- stdin is not read.

## Output

- stdout: one JSON object per line, flushed line by line (read it incrementally for progress).
  ASCII only (anything else is escaped as `\uXXXX`), lines end with `\n` on every OS.
- Every object has an `event` field naming its kind. Ignore fields you do not know.
- stderr: messages for humans and library warnings. Not part of the contract; it can hold
  document text, so keep it in memory like stdout.
- Text offsets count Unicode code points (Python string indices): not UTF-16 code units (C#,
  NSString), not bytes.
- Paths are absolute, with the platform's separators.
- Timestamps are ISO 8601 UTC, `2026-10-13T09:41:00Z`.

## Errors and exit codes

| Exit | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | The command failed: its last line is an `error` event. `ingest` also exits 1 when some file failed, without an `error` event: see its `doc` events, it still ends with `done`. |
| 2 | Usage error (unknown command, missing argument): nothing on stdout, usage on stderr. |
| 143 | `ingest` cancelled with SIGTERM. |
| other | Crash or kill: no `error` event. |

```json
{"event": "error", "error": "no_document", "message": "no document 3f9c0a1b2c3d4e5f"}
```

| `error` | When |
| --- | --- |
| `no_index` | `show`, `review`, `cat`, `forget`, `tokens`, `export` before the first `ingest`. |
| `no_document` | `show`, `review`, `cat` with an unknown `doc_id`. `show` says it also for a document with 0 chunks (an empty file): check `chunks` in `list` first. |
| `bad_value` | `deny`, `allow`, `cat`: the value or name is empty once normalized. `rules --set`: the name is empty or holds `:`. |
| `bad_dir` | `export`: the folder is the data root, inside it or around it. Nothing is written. |
| `bad_config` | `desktop-config`: Claude Desktop's config is not a JSON object whose `mcpServers` is an object. Nothing is written. |
| `no_client` | `drive login`: no OAuth client file given yet, or the file is not a Desktop app client. |
| `not_connected` | `drive add`, `drive sync` before `drive login`, or Google refused the stored login (revoked, client deleted); `drive login` when consent was denied or never came. |
| `no_folder` | `drive add`: not a folder link or id, or not a folder you can open. `drive remove`: not in the list. |
| `failed` | Anything else. `message` is the exception class (`DatabaseError`, `OperationalError`...); details on stderr. |

`message` is English, for logs and fallbacks: show the user a text chosen from `error`.

## Commands

### status

Data root, models, index size. One line.

```json
{"event": "status", "root": "/Users/me/Library/Application Support/mcp-secret", "models": true, "index": true, "docs": 30, "chunks": 41}
```

| Field | Type | |
| --- | --- | --- |
| `root` | string | data root in use |
| `models` | bool | detector and Docling models are in the local caches (`models` downloads them) |
| `index` | bool | an `ingest` has run on this data root |
| `docs`, `chunks` | int | indexed documents and published chunks |

`status` also recovers the index after an ingest killed hard (see Cancelling): call it at startup.

### models

Downloads the models; with `drive`, the only command that uses the network. Run it once, before the first
`ingest`. Progress is per step, not per byte.

```json
{"event": "start", "steps": 2}
{"event": "step", "step": 1, "name": "docling"}
{"event": "step", "step": 2, "name": "detector"}
{"event": "done"}
```

`step` is 1-based and announces the step that begins: progress is `(step - 1) / steps` until
`done`. `name` is `docling` or `detector`.

### ingest `<path>... [--force]`

Indexes files and folders (recursively: PDF, DOCX, XLSX, PPTX, HTML, MD, images, EML, TXT).
An unchanged file (same sha256) is skipped unless `--force`: use it after `allow add`, which
only applies to documents indexed afterwards. Every run first applies `deny.txt` to everything
already published, and forgets documents whose file disappeared from an ingested folder.

```json
{"event": "start", "files": 2}
{"event": "doc", "doc_id": "a1b2c3d4e5f60718", "path": "/Users/me/docs/old.pdf", "chunks": 0, "status": "removed", "error": null}
{"event": "doc", "doc_id": "3f9c0a1b2c3d4e5f", "path": "/Users/me/docs/contratto.pdf", "chunks": 4, "status": "indexed", "error": null}
{"event": "doc", "doc_id": null, "path": "/Users/me/docs/locked.pdf", "chunks": null, "status": "error", "error": "PermissionError"}
{"event": "done"}
```

| Field | Type | |
| --- | --- | --- |
| `files` | int | files this run reads: one `doc` event each; `removed` events come on top |
| `doc_id` | string or null | 16 hex characters; null for a file that failed and was never indexed |
| `path` | string | the file |
| `chunks` | int or null | chunks published for it; 0 when `removed`, null on `error` |
| `status` | string | `indexed` (parsed and published), `unchanged` (skipped), `removed` (file gone, its chunks deleted), `error` |
| `error` | string or null | exception class when `status` is `error`; never the message, which may quote the document |

Documents are committed one at a time: a `doc` event means that document is final, whatever
happens to the rest of the run.

### list

One line per indexed document, sorted by path. Nothing when there is no index yet.

```json
{"event": "doc", "doc_id": "3f9c0a1b2c3d4e5f", "path": "/Users/me/docs/contratto.pdf", "chunks": 4, "exists": true, "stale": false, "categories": ["Fatture", "docs"]}
```

| Field | Type | |
| --- | --- | --- |
| `exists` | bool | the file can still be read at `path` |
| `stale` | bool | it changed since it was indexed (sha256 differs); false when it does not exist |
| `categories` | array of string | sorted, case-insensitively: see Categories |

`list` hashes every file: it reads them all.

### show `<doc_id>`

What the agent sees: the published chunks, in order.

```json
{"event": "chunk", "chunk_id": 12, "text": "# Contratto\n\nTra [PERSONA_1] (CF [CF_1]) e [ORG_2]..."}
```

`chunk_id` is the id the agent's tools use; it changes when the document is re-indexed.

### review `<doc_id>`

The document in clear as ingest reads it now, and what ingest masks in it. It re-parses the
file with the ingest pipeline (parse, normalization, detection, `deny.txt`, `allow.txt`,
`public.txt`, masking) and writes nothing. Detection, most of the time, is skipped while the
parsed text is the one indexed: the spans found then are reused (`ingest --force` refreshes
them). One line.

```json
{"event": "review", "doc_id": "3f9c0a1b2c3d4e5f", "path": "/Users/me/docs/contratto.txt", "exists": true, "stale": false, "text": "contratto.txt\n\nTra Mario Rossi (CF RSSMRA80A01H501U) e...", "spans": [{"start": 19, "end": 30, "type": "PERSONA", "token": "[PERSONA_1]"}, {"start": 35, "end": 51, "type": "CF", "token": "[CF_1]"}]}
```

| Field | Type | |
| --- | --- | --- |
| `exists`, `stale` | bool | as in `list` |
| `text` | string | the text in clear: the file name (no folders, `_` and `-` as spaces), a blank line, the content. The agent sees the name masked as `name`, the rest as chunks. `""` when the file does not exist |
| `spans` | array | sorted, non-overlapping; `[]` when the file does not exist |
| `spans[].start`, `.end` | int | code point offsets in `text`, end exclusive |
| `spans[].type` | string | `PERSONA`, `ORG`, `LUOGO`, `INDIRIZZO`, `CF`, `PIVA`, `IBAN`, `EMAIL`, `TELEFONO`, `TARGA`, `DOCUMENTO`, `DATA_NASCITA` (values from `deny.txt` are `PERSONA`) |
| `spans[].token` | string | what replaces the span, `[TYPE_N]`; for a value ingest has not learned yet, the token it would get now |

When `stale` is false and neither list changed since the document was indexed, replacing each
span with its token gives exactly the text whose chunks `show` prints.

`review -` is the same as a long-lived worker: it loads the models once (about 7 seconds), then
reads one `doc_id` per line on stdin and answers each with one line, a `review` or an `error`
event (`no_index`, `no_document`, `failed`), in order, in well under a second. Errors do not end
it; it exits 0 when stdin closes. Send the next id after reading the previous answer, or keep
the ids in order: answers carry no request id.

### deny / allow `add|remove <value>`

`deny`: mask this value everywhere, always. `allow`: never mask it. The value is normalized as
ingest reads the lists (Unicode NFKC, whitespace collapsed, lowercase) and stored in
`private/deny.txt` or `private/allow.txt`, mode 0600. Adding a value to one list removes it
from the other: the latest decision wins.

```json
{"event": "deny", "action": "add", "changed": true}
```

`changed` is false when the value was already there (`add`) or was not (`remove`). Nothing is
re-indexed: follow with `ingest`.

- After `deny add`: `ingest` of any path (even an unchanged file) masks the value in every
  published document.
- After `allow add`: `ingest --force` of a document publishes the value in clear there; other
  documents keep it masked until they are re-indexed with `--force`.

## Categories

A document's categories are worked out on every `list`, from three sources:

1. The name of the folder that holds its file (automatic).
2. Every keyword rule (`rules`) with a term in what the agent sees of it (automatic).
3. `cat` decisions: `add` gives it a category; `remove` takes one away for good, automatic ones
   too (a rule or a folder that gives it again does not bring it back; `add` does).

Categories, rules and decisions stay in `private/`: nothing of them is published or reaches the
agent. Editing a rule needs no re-index.

### cat `add|remove <doc_id>... <name>`

One line for each document, in order. An unknown `doc_id` is an error and changes none.

```json
{"event": "cat", "action": "add", "changed": true}
```

`name` is trimmed, inner whitespace collapsed; case is kept. `changed` tells whether the
document's categories changed: false for `add` of one it already had (now kept by hand) and for
`remove` of one it did not have (nothing is remembered then).

### forget `<doc_id>...`

Takes documents out of the index, for good: their chunks, record and categories go; the files
stay where they are (`ingest` brings one back, under a new `doc_id`). Values keep their tokens in
the documents that remain. One `removed` line for each document taken out, as `ingest` sends for
a file that is gone; an id that is not indexed is skipped.

```json
{"event": "doc", "doc_id": "a1b2c3d4e5f60718", "path": "/Users/me/docs/old.pdf", "chunks": 0, "status": "removed", "error": null}
```

### rules `[--set <name> [<term>...]]`

The keyword rules, one line each, in the order of `private/categories.txt`
(`Name: term, term*, two words` per line, `#` comments; mode 0600). Nothing when there are none.
`--set` first replaces the rule's terms, or removes the rule if none are given; other lines of
the file are kept. A term is a word, a prefix (`fattur*`) or a phrase (`nota di credito`),
matched case- and accent-insensitively; commas in an argument split terms.

```json
{"event": "rule", "name": "Fatture", "terms": ["fattur*", "imponibile", "nota di credito"]}
```

### tokens `[--type <TYPE>]...`

Every token published in two documents or more, in the most documents first (ties by token),
with its value in clear: what links documents in the graph. `--type` keeps only those types.
Reads every chunk.

```json
{"event": "token", "token": "[PERSONA_1]", "type": "PERSONA", "value": "mario rossi", "doc_ids": ["3f9c0a1b2c3d4e5f", "a1b2c3d4e5f60718"]}
```

| Field | Type | |
| --- | --- | --- |
| `type` | string | as in `review` |
| `value` | string or null | normalized as ingest stores it (lowercase); null for a token-shaped text no ingest wrote |
| `doc_ids` | array of string | sorted |

### export `<dir>`

Writes what the agent sees to a folder an agent works in (Claude Cowork):
`<dir>/pseudonymized/<doc_id>.md`, one file per document (its pseudonymized file name, then its
chunks in order, blank line between), and `<dir>/PSEUDONYMIZED.md` telling the agent what the tokens are. It reads `public.db` only, inside the `serve` sandbox plus write
access to `<dir>`; it never opens `private/`. `<dir>` is created if its parent exists.

`pseudonymized/` is made to match the index: a file named like an export (16 hex digits, `.md`
or `.md.tmp`) that is not an indexed document's (removed or scrubbed since the last export, or
left by a killed run) is deleted. Any other file, there or in `<dir>`, is left alone: `<dir>` may be
a folder with files of its own, and the agent can write its own there. Run it again after every
`ingest`. One line.

```json
{"event": "export", "dir": "/Users/me/Cowork", "docs": 12, "removed": 1}
```

| Field | Type | |
| --- | --- | --- |
| `dir` | string | the folder, absolute |
| `docs` | int | files written (documents with no chunk, an empty file, get none) |
| `removed` | int | files deleted from `pseudonymized/` |

### desktop-config `[--install | --uninstall]`

Shows Claude Desktop's configuration for this server; `--install` adds it, `--uninstall`
removes it. Only `mcpServers["mcp-secret"]` changes; the previous file is kept next to it as
`claude_desktop_config.json.bak`. Claude Desktop reads the file at startup: restart it.

| | macOS | Windows |
| --- | --- | --- |
| config | `~/Library/Application Support/Claude/claude_desktop_config.json` | `%APPDATA%\Claude\claude_desktop_config.json` |
| log | `~/Library/Logs/Claude/mcp-server-mcp-secret.log` | `%APPDATA%\Claude\logs\mcp-server-mcp-secret.log` |

The entry: `{"command": "<absolute path of this mcp-secret>", "args": ["serve"]}`, plus
`"env": {"MCP_SECRET_HOME": "<root>"}` when the data root is not the default.

```json
{"event": "desktop", "config": "/Users/me/Library/Application Support/Claude/claude_desktop_config.json", "configured": true, "command": "/Users/me/mcp_secret/.venv/bin/mcp-secret", "command_ok": true, "last_log": "2026-10-13T09:41:00Z"}
```

| Field | Type | |
| --- | --- | --- |
| `config` | string | the config file |
| `configured` | bool | `mcpServers["mcp-secret"]` is there |
| `command` | string or null | the executable it points to |
| `command_ok` | bool | that executable exists |
| `last_log` | string or null | last change of Claude Desktop's log for this server: when it last started or used it |

### drive `[status | login [<client_secret.json>] | logout | add <folder> | remove <folder> | sync]`

Google Drive folders, mirrored into `private/drive/` and indexed from there. `<folder>` is a
folder link (`https://drive.google.com/drive/folders/<id>`, a shared drive's root too) or its
id. Settings live in `private/drive.json` (mode 0600): the OAuth client, the refresh token,
the folders. Every action but `sync` ends with one line:

```json
{"event": "drive", "client": true, "connected": true, "folders": [{"id": "1AbCdEfGhIjK", "name": "Clienti"}]}
```

| Field | Type | |
| --- | --- | --- |
| `client` | bool | an OAuth client file was given (`login <file>`; kept for the next logins) |
| `connected` | bool | a refresh token is stored; not checked with Google |
| `folders` | array | the folders to mirror, in the order added; `name` as on Drive |

- `status` (or no action): reads `drive.json`, no network.
- `login`: opens the browser for consent (read-only scope) and waits up to 10 minutes after
  the last request to its loopback port; SIGTERM cancels it. The first time, pass the JSON of a
  Google Cloud OAuth client of type Desktop app.
- `logout`: revokes the token with Google (best effort) and forgets it. Folders, the local
  copy and the index stay.
- `add`: checks with Google that the folder exists. Nothing is downloaded until `sync`.
- `remove`: forgets the folder; the next `sync` deletes its copy and its documents.
- `sync`: lists every folder, then mirrors: files ingest reads are downloaded when new or
  changed (Google Docs, Sheets, Slides exported to DOCX, XLSX, PPTX), everything else under
  `private/drive/` is deleted. Then it becomes `ingest private/drive/` on the same stdout:

```json
{"event": "start", "files": 2}
{"event": "file", "path": "/Users/me/Library/Application Support/mcp-secret/private/drive/Clienti/contratto.docx", "status": "downloaded", "error": null}
{"event": "file", "path": "/Users/me/Library/Application Support/mcp-secret/private/drive/Clienti/note.pdf", "status": "error", "error": "HTTPError"}
{"event": "start", "files": 2}
{"event": "doc", "doc_id": "3f9c0a1b2c3d4e5f", "path": "/Users/me/Library/Application Support/mcp-secret/private/drive/Clienti/contratto.docx", "chunks": 4, "status": "indexed", "error": null}
...
{"event": "done"}
```

`file.status` is `downloaded`, `unchanged` or `error` (`error`: the exception class; the old
copy, if any, stays). A failed listing ends the run with an `error` event before any file is
touched. The exit code is the ingest's: a `file` error alone does not make it 1. Documents
whose file was deleted come as ingest's `removed` events.

## Cancelling

- `ingest`: send SIGTERM. The document in progress rolls back, the run exits 143, documents
  already reported stay indexed.
- A hard kill (SIGKILL, `TerminateProcess` on Windows, a crash) is safe too, but leaves a
  rollback journal behind: `list`, `show`, `review` and `serve` then fail (`failed`,
  `OperationalError`) until a read-write open recovers it. `status` does, as does `ingest`.
- The other commands can be killed at any time: the only files they write are replaced
  atomically.

## Concurrency

Run one writing command at a time (`ingest`, `deny`, `allow`, `cat`, `rules --set`,
`desktop-config`, `drive` but `status`). A second `ingest` waits up to 5 seconds for each lock the first one holds,
then fails: the whole run (`failed`) or one file (`OperationalError`). Reading commands can run
during an ingest.

## Privacy

`ingest`, `list`, `review`, `tokens` and `drive sync` print paths, document text and values in clear. A UI keeps them in
memory only: no logs, caches, temporary files or saved window state. Do not run these commands
through an agent whose output leaves the machine. `show` prints only what the agent sees, and
`export` writes only that: its folder is as public as `public.db`.
