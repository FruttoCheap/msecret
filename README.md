# mcp-secret

Indexes your documents locally and lets an AI agent search them over MCP, **pseudonymized
only**: names, tax codes, IBANs, emails, phones become tokens like `[PERSONA_3]`, the same
token for the same value in every document. The agent sees no folders and no file metadata
(title, author, dates). The file name and an email's From/To/Cc/Subject lines are text, masked
with the body, so whatever detection misses there is published like a miss in the body.

- `ingest` (no network) parses, detects PII, writes the token map to `private/private.db` and
  the pseudonymized chunks to `public/public.db`.
- `serve` (MCP over stdio) opens only `public/public.db`, read-only, inside `sandbox-exec`: no
  network, no programs but Python itself, no system services (mach-lookup), no reads of your
  home folder, `/Users`, `/Volumes` or the temp folders except `public/` and the Python runtime,
  no file writes anywhere, no peeking at or signalling other processes. It refuses to start if
  the sandbox is not active.

> **Not a guarantee of anonymity.** Detection recall is below 1: a value the detector misses
> reaches the agent in clear. Read [Residual risks](#residual-risks) before trusting it with
> sensitive files.

## Install

macOS or Windows, [uv](https://docs.astral.sh/uv/). Prefer an app? See [Try the app](#try-the-app).

```sh
git clone https://github.com/FruttoCheap/msecret.git && cd msecret
uv sync
uv run mcp-secret models     # one-time model download; with `drive`, the only network use
```

## Use

```sh
uv run mcp-secret ingest ~/Documents/clienti    # files or folders; re-run any time
# 3f9c0a1b2c3d4e5f	4 chunks	/Users/you/Documents/clienti/contratto.pdf
uv run mcp-secret show 3f9c0a1b2c3d4e5f         # exactly what the agent sees
uv run mcp-secret list                          # indexed files: changed or missing ones flagged
uv run mcp-secret review 3f9c0a1b2c3d4e5f       # the text in clear, what is masked, which token
uv run mcp-secret deny add "Mario Rossi"        # always mask it (`allow add`: never), then ingest
uv run mcp-secret rules --set Fatture "fattur*" imponibile   # an automatic category
uv run mcp-secret cat add 3f9c0a1b2c3d4e5f Urgente           # one by hand (`remove`: for good)
uv run mcp-secret forget 3f9c0a1b2c3d4e5f       # out of the index (the file stays); several ids work too
uv run mcp-secret tokens --type PERSONA         # the tokens in the most documents, values in clear
```

Categories are for you, not the agent: a document gets its folder's name, every rule with a
term in its published text, and what you add by hand; nothing of them reaches `public.db`.

`ingest` and `list` print each file's full path, and `review` the document's text, in clear, to
your terminal: run them yourself, not through an agent whose output leaves the machine (a cloud
agent would send the paths and the text along).

Formats: PDF, DOCX, XLSX, PPTX, HTML, MD, images (OCR), EML, TXT. Unchanged files are skipped;
changed files replace their chunks; files deleted or moved out of an ingested folder lose theirs.

The data root is `~/Library/Application Support/mcp-secret` (override with `MCP_SECRET_HOME`):

| File | Holds |
| --- | --- |
| `private/private.db` | file paths, the value → token map, where the detector found values (offsets, no text), categories added or removed by hand; 0600, never opened by `serve` |
| `private/deny.txt` | values to always mask, one per line (masked as `PERSONA`) |
| `private/allow.txt` | values never to mask, one per line (applies to documents indexed afterwards: `ingest --force`) |
| `src/mcp_secret/public.txt` (built in) | well-known public names left in clear when detected as `ORG` or `LUOGO`: banks, public bodies, large companies, countries, regions, cities of ~90,000+ people; `deny.txt` overrides it, `ingest --force` applies a change |
| `private/categories.txt` | keyword rules of the automatic categories, `Name: term, prefix*, a phrase` per line |
| `private/drive.json` | Google Drive: OAuth client, refresh token (read-only access to the whole Drive), folders; 0600 |
| `private/drive/` | the Drive folders' files, in clear, as last synced |
| `public/public.db` | pseudonymized chunks + full-text index, nothing else |

`show` reveals a missed value? `deny add` it (or add a line to `deny.txt`) and run `ingest`
again: it is masked in new documents and scrubbed from those already indexed.

Every command takes `--json` (before the command) and then prints one JSON object per line:
the contract a UI builds on, in [docs/cli-json.md](docs/cli-json.md).

## Google Drive

Folders you pick are copied into `private/drive/` and indexed from there like local files;
Google Docs, Sheets and Slides come as DOCX, XLSX and PPTX, each folder's name becomes a
category. Only `drive login`, `add` and `sync` use the network, and they never parse a
document: the ingest that follows does, offline and sandboxed. `serve` cannot read
`private/`, so neither the copies nor the token.

Once, an OAuth client of your own (no Google review needed for an Internal app):

1. [Google Cloud console](https://console.cloud.google.com): a project of your Workspace
   organization, with the Google Drive API enabled.
2. Google Auth Platform: audience **Internal** (an External app in testing must log in again
   every 7 days); data access: `.../auth/drive.readonly`.
3. Clients → Create client → **Desktop app** → download the JSON.

```sh
uv run mcp-secret drive login ~/Downloads/client_secret_….json    # consent in the browser
uv run mcp-secret drive add "https://drive.google.com/drive/folders/1AbC…"
uv run mcp-secret drive sync         # download what changed, then ingest it; re-run any time
uv run mcp-secret drive remove 1AbC… # its copy and its documents go at the next sync
```

Not mirrored: shortcuts, Forms, Drawings, files ingest cannot read; Docs over 10 MB once
exported fail (a `file` error).

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
- `export` rewrites the originals, and what the detector does not read leaves them as it is:
  images (a photo, a signature, an ID pasted in a page), text drawn as shapes or in a font with
  no Unicode map, embedded files, PDF text outside the page. A token takes the box of the value
  it replaces, tiny where the value was short. A value Docling reads another way than the file
  holds it (a CF in three formatted runs reads `RSSMRA 80A01 H501U`) gets another token there.
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

## Claude Cowork

`serve` is for searching. To let Claude work on whole documents (read, edit, summarize, write
new files, run scripts over all of them), write them to a folder and connect that folder to a
Cowork session:

```sh
uv run mcp-secret export ~/Cowork/clienti    # pseudonymized/<doc_id>.<ext>, one per document, and a guide
uv run mcp-secret export ~/Desktop --doc <doc_id>    # one document, straight in that folder
```

A PDF, Word, PowerPoint or Excel document whose file is still there comes out in its own format:
the file itself, every value ingest masks replaced by its token wherever the file keeps text
(body, headers, footers, notes, comments, alt text, cells, sheet names, form fields), metadata,
thumbnail, link targets and comment authors removed, a workbook's formulas replaced by their
values. Layout and images stay. Any other document comes out as what `show` prints, its
pseudonymized name on the first line, as `<doc_id>.md`: also a scan (a page without text, or
mostly one image), a workbook with pivot tables or links to other files, a file with OLE objects.
Files are named by random id; the guide lists their pseudonymized names.

To rewrite the originals `export` reads them and `private/`: it runs boxed like `ingest` (no
network), not like `serve`, and it masks like ingest, learning a value found only in the file (a
name in a header Docling skips): from then on it is masked in `public.db` too. It makes
`pseudonymized/` match the index, so a document removed or scrubbed by a later `ingest` disappears at the
next `export`: run it after every `ingest` (the app does, once you chose a folder). The folder is
a second copy of what the agent can read: anything the detector missed is readable there too.
Only files named like its own are ever replaced or deleted there, so the folder can be one with
files of yours; Claude's own belong outside `pseudonymized/`. What Claude writes carries
tokens, not values.

## UI

A macOS app (SwiftUI, macOS 15+). From this repo it runs unbundled, as below; the signed,
notarized build is on the Releases page ([Try the app](#try-the-app)). It
only runs the CLI with `--json` ([docs/cli-json.md](docs/cli-json.md)); it never opens the
databases or the deny/allow lists, never starts `serve`, never loads a model.

```sh
swift run --package-path apps/macos MCPSecret
```

Windows and Linux: `uv run mcp-secret-gui` (on Windows also `.venv\Scripts\mcp-secret-gui.exe`,
no console; the installer in [Try the app](#try-the-app) adds it to the Start menu). A smaller
tkinter UI, Windows 11 style (sv-ttk), in English, on the same contract: add files or folders, the original with
its masked values tinted by type and tagged with their token next to what the agent sees (click
a value to find its token there), Open File for the original in its own app, Always/Never Mask a
selection, Re-index, Export… (the open document, `export --doc`) and Export All for Cowork,
Claude Desktop install, the model download on first launch. When the
original cannot be read, its pane says why, with the last lines the review process wrote.

- The window opens on the simple view: drop one file (or ⌘O) and it is indexed like any other,
  the original on the left, what the agent sees on the right; drag it out (or Save…): the
  original with tokens for values, or `.md` (`export --doc`).
  Select a value, or text, and choose whether to mask it in this document only (Mask Here, Don't
  Mask Here) or everywhere (Always Mask, Never Mask). The button at the top right, or ⇧⌘A,
  switches to the advanced view described below.
- Advanced view: drop files and folders on the sidebar (or ⌘O) and watch them index; each document shows its
  state: indexed, unchanged, error, file missing, changed since indexing.
- A document opens in two columns: the original in clear, each masked value tinted by type and
  tagged with its token (legend on top), and what the agent sees. Select a value to light up
  its token on the right; ⌘[ and ⌘] step through them. One core process keeps the models loaded
  for the session (about 2.5 GB of memory): it starts with the app, then each document opens in
  well under a second.
- Select text that slipped through and choose Always Mask (⇧⌘M). Select a value that should not
  be masked and choose Don't Mask (⇧⌘U); other documents keep it masked until Re-index All
  (⇧⌘R).
- Categories: the sidebar filters by one; right-click a document to add or remove one by hand.
  Each document also gets its folder's name and the category of every keyword rule (Settings)
  with a term in what the agent sees.
- Graph (toolbar): the documents, colored by category, linked by the tokens and categories
  they share; it follows the sidebar's category. Click a document for what links it (switch a
  link off when it is everywhere, like your own name), double-click to open it; filter by type
  and by links in common, pinch or zoom, drag to pan.
- Export for Cowork (toolbar): choose a folder, get the pseudonymized documents in it (`export`); it is
  refreshed after every ingest for the rest of the session.
- Settings (⌘,): CLI path and data root for the session (`-cliPath`, `-dataRoot` launch
  arguments preset them), model download, keyword rules, Google Drive (connect with the OAuth
  client file, add or remove folders, sync), Claude Desktop install and removal.
- Sync Google Drive (toolbar, once a folder is added): download what changed, then index it.

Document text, values and paths stay in the app's memory: no caches, logs, temporary files or
restored windows. Italian or English, following the system (`-AppleLanguages "(en)"` forces
English). Tests: `swift test --package-path apps/macos` (one of them drives the real CLI);
`SCREENSHOTS=<dir>` in front of it, with `--filter screenshots`, draws the main screens in light
and dark.

## Try the app

Both from the [latest release](https://github.com/FruttoCheap/msecret/releases/latest); about
4 GB of disk, network for the install and the models, no repo or developer tools.

**Mac** (Apple Silicon or Intel, macOS 15+). On Intel the core installs older pinned versions
(torch 2.2.2, docling 2.73, gliner 0.2.22: nothing newer publishes an Intel wheel), see
`[tool.uv]` in `pyproject.toml`. Docling 2.73's layout model takes more lines for page headers
(a lone line at the top of a page) and leaves them out: missing from what the agent sees, not
published in clear.

1. Download `mcp-secret-<version>.zip`, unzip it, drag `mcp-secret.app` to Applications and
   open it.
2. The first launch says "Install the core": click Install. It installs
   [uv](https://docs.astral.sh/uv/) from astral.sh if you have none, then the core (and its
   Python dependencies, from PyPI) into `~/.local/share/uv/tools/mcp-secret`: a few minutes.
   "Or in Terminal" shows the same command, to paste yourself.
3. The Setup Guide takes over: download the models (once, about 2 GB, from Hugging Face), add
   documents, connect Claude Desktop (then restart Claude). After that the only network use is
   `drive`, if you connect Google Drive.

Update: unzip the new version over the old one and open it: its core has another version, so
step 2 comes back. Remove everything: delete the app, run `uv tool uninstall mcp-secret`,
and delete `~/Library/Application Support/mcp-secret` (the index and the token map).

**Windows** (10 or 11, x64). Run `mcp-secret-setup-<version>.exe`. It is not code-signed:
Windows says "Windows protected your PC"; click More info, then Run anyway. It installs for your
account only (no administrator), carries uv, and installs the core with it (a few minutes; its
log is `%LOCALAPPDATA%\Programs\mcp-secret\install.log`). Then open mcp-secret from the Start
menu: it offers the model download, then add documents and Connect Claude Desktop (restart
Claude). Update: run the new installer. Remove: Settings → Apps → mcp-secret (the core and uv's
download cache go; the index and the token map stay in `%LOCALAPPDATA%\mcp-secret`: delete it
too to remove everything).

## Develop

```sh
uv run ruff check && uv run pytest    # needs `mcp-secret models` once
```

Release: once, `xcrun notarytool store-credentials mcp-secret` (Apple ID, app-specific
password); then `./scripts/release.sh` writes `dist/mcp-secret-<version>.zip`, the version
being `pyproject.toml`'s. The app carries the core's wheel and pinned dependencies
(`uv export`), and asks for the install again whenever the core's `status` version differs. `SIGN_ID=- NOTARIZE=0 ./scripts/release.sh`
builds an ad-hoc signed app to check the bundle on this Mac. The Windows installer
(`scripts/installer.iss`) is built by CI (`.github/workflows/ci.yml`, the `installer` job's
artifact), which also installs it, indexes a document with it and uninstalls it.

`tests/test_leak.py` hides canaries in every corner of a DOCX, PDF, PNG, XLSX, EML and HTML,
ingests them, drives `serve` through a real MCP client and checks that no canary reaches
`public.db` or tool output; it also probes the sandbox and kills an ingest mid-transaction.

## License

[MIT](LICENSE). Models and dependencies keep their own licenses: [THIRD_PARTY.md](THIRD_PARTY.md).
Security reports: [SECURITY.md](SECURITY.md).
