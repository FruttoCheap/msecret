"""Ingest: file -> NFC text -> pseudonymized chunks in public.db, entities in private.db."""

import email
import email.policy
import functools
import hashlib
import io
import re
import secrets
import sqlite3
import sys
import unicodedata
from pathlib import Path

from mcp_secret import pii

SUFFIXES = {".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".md", ".png", ".jpg", ".jpeg",
            ".tif", ".tiff", ".bmp", ".eml", ".txt"}

# Rollback journals (not WAL) make a transaction over ATTACHed files atomic (super-journal).
SCHEMA = """
PRAGMA main.journal_mode = DELETE;
PRAGMA pub.journal_mode = DELETE;
CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities(type TEXT NOT NULL, value TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS pub.chunks
    USING fts5(doc_id UNINDEXED, seq UNINDEXED, text, tokenize="unicode61 tokenchars '_'");
INSERT INTO pub.chunks(chunks, rank) VALUES ('secure-delete', 1);
"""
_TOKEN = r"(?-i:\[[A-Z]+(?:_[A-Z]+)*_\d+\])"
# What the model tags but is no entity: a type label ("IBAN", "Telefono"), a bare short number.
# As an entity it would be masked in every document.
_NOISE = re.compile(r"(?i)\d{1,3}|persona|org|luogo|indirizzo|cf|piva|iban|e-?mail|telefono|targa"
                    r"|documento|data di nascita")


@functools.cache
def _converter():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import OcrMacOptions, PdfPipelineOptions
    from docling.datamodel.settings import settings
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

    opts = PdfPipelineOptions(artifacts_path=settings.cache_dir / "models",  # `mcp-secret models`
                              ocr_options=OcrMacOptions(lang=["it-IT", "en-US"]))
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts),
                                             InputFormat.IMAGE: ImageFormatOption(pipeline_options=opts)})


def _markdown(source) -> str:
    from docling_core.transforms.serializer.markdown import MarkdownDocSerializer, MarkdownParams

    doc = _converter().convert(source).document
    # No **emphasis** markers: a bold run inside a CF would split it. No link targets: hidden
    # metadata (wa.me/<phone>, mailto:) like the title and author.
    params = MarkdownParams(escape_html=False, escape_underscores=False, include_formatting=False,
                            include_hyperlinks=False)
    return MarkdownDocSerializer(doc=doc, params=params).serialize().text


def _xlsx(path: Path):
    """Docling drops number formats: a zero-padded code (P.IVA, phone) would lose its 0s."""
    import openpyxl
    from docling.datamodel.base_models import DocumentStream

    wb = openpyxl.load_workbook(path, data_only=True)  # cached values, as Docling reads them
    cells = [c for ws in wb.worksheets for row in ws.iter_rows() for c in row
             if type(c.value) is int and re.fullmatch("0+", c.number_format)]
    for c in cells:
        c.value = str(c.value).zfill(len(c.number_format))
    if not cells:
        return path
    wb.save(buf := io.BytesIO())
    return DocumentStream(name="book.xlsx", stream=io.BytesIO(buf.getvalue()))


def parse(path: Path) -> str:
    """Document text. Only the content: no file name, no metadata (title, author, dates)."""
    kind = path.suffix.lower()
    if kind == ".txt":
        return path.read_text(errors="replace")
    if kind != ".eml":
        return _markdown(_xlsx(path) if kind == ".xlsx" else path)
    # ponytail: attachments are skipped, save and ingest them as files
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    head = "\n".join(f"{h}: {msg[h]}" for h in ("From", "To", "Cc", "Subject") if msg[h])
    part = msg.get_body(("plain", "html"))
    body = part.get_content() if part else ""
    if part and part.get_content_type() == "text/html":
        from docling.datamodel.base_models import DocumentStream

        body = _markdown(DocumentStream(name="body.html", stream=io.BytesIO(body.encode())))
    return f"{head}\n\n{body}"


def clean(text: str) -> str:
    """The text that is detected and published: invisible format characters (zero-width, soft
    hyphen) dropped, compatibility forms (fullwidth, NBSP) folded. NFKC output is NFC too."""
    # ponytail: homoglyphs (a Cyrillic letter in a CF) stay; add a confusables fold if needed
    return unicodedata.normalize("NFKC", "".join(c for c in text if unicodedata.category(c) != "Cf"))


def norm(value: str) -> str:
    # "İ".lower() is "i" + U+0307, which an IGNORECASE pattern built from it never matches
    return " ".join(value.split()).lower().replace("i\u0307", "i")


def _pattern(values) -> re.Pattern:
    """Whole-word ("_" separates: CI_<value>.pdf), case-insensitive, whitespace-tolerant match
    of any value, longest first. A [TYPE_N] token matches first and whole, so no value match
    starts inside one: use _hits()."""
    # ponytail: one big alternation, O(text x values); Aho-Corasick if entities reach ~1e5
    alts = (r"\s+".join(map(re.escape, v.split())) for v in sorted(values, key=len, reverse=True))
    return re.compile(rf"{_TOKEN}|(?<![^\W_])(?P<v>{'|'.join(alts)})(?![^\W_])", re.IGNORECASE)


def _hits(pat: re.Pattern, text: str) -> list[re.Match]:
    """Value matches of a _pattern(), never inside a published token ([TELEFONO_1] stays)."""
    return [m for m in pat.finditer(text) if m["v"]]


def _lookup(table: dict, m: re.Match) -> str:
    # ponytail: re.IGNORECASE folds a few chars lower() does not (U+017F long s); those get a
    # generic token instead of a crash whose traceback would print the value
    return table.get(norm(m[0]), "[REDACTED]")


def _token(con: sqlite3.Connection, type_: str, value: str, new: dict) -> str:
    """Same normalized value -> same token, forever. New values are added to `new`."""
    v = norm(value)
    row = con.execute("SELECT token FROM entities WHERE value = ?", (v,)).fetchone()
    if row:
        return row[0]
    n = con.execute("SELECT count(*) FROM entities WHERE type = ?", (type_,)).fetchone()[0]
    new[v] = token = f"[{type_}_{n + 1}]"
    con.execute("INSERT INTO entities VALUES (?, ?, ?)", (type_, v, token))
    return token


def _apply(text: str, cands: list[tuple[int, int, str]]) -> str:
    """Replace each (start, end, token); overlapping ranges merge, so nothing in between leaks."""
    out, end = [], 0
    for s, e, token in sorted(cands):
        if s < end:
            end = max(end, e)
            continue
        out += [text[end:s], token]
        end = e
    return "".join(out) + text[end:]


def mask(con: sqlite3.Connection, text: str, spans: list, allow: set[str], new: dict) -> str:
    """Detected spans plus every known value (sweep) become [TYPE_N] tokens."""
    cands = [(s.start, s.end, _token(con, s.type, v, new)) for s in spans
             if norm(v := text[s.start:s.end]) not in allow and not _NOISE.fullmatch(v)]
    known = {v: t for v, t in con.execute("SELECT value, token FROM entities") if v not in allow}
    if known:
        cands += [(m.start(), m.end(), _lookup(known, m)) for m in _hits(_pattern(known), text)]
    return _apply(text, cands)


def sweep(con: sqlite3.Connection, new: dict) -> None:
    """Mask newly learned values in chunks already published, also where a chunk boundary cuts
    one in two. FTS on any of their words finds the candidate documents."""
    # ponytail: a value glued to "_" in an old chunk (ci_<value>) is one FTS term, not found here
    if not new:
        return
    pat = _pattern(new)
    query = " OR ".join('"' + w.replace('"', '""') + '"' for v in new for w in v.split())
    for (doc_id,) in con.execute("SELECT DISTINCT doc_id FROM pub.chunks WHERE chunks MATCH ?",
                                 (query,)).fetchall():
        rows = con.execute("SELECT rowid, text FROM pub.chunks WHERE doc_id = ? ORDER BY seq",
                           (doc_id,)).fetchall()
        cuts = [[(m.start(), m.end(), _lookup(new, m)) for m in _hits(pat, t)] for _, t in rows]
        for i in range(len(rows) - 1):
            a, b = rows[i][1], rows[i + 1][1]
            h = len(b.split("\n\n", 1)[0]) + 2 if b.startswith("#") and "\n\n" in b else 0
            for m in _hits(pat, f"{a}\n{b[h:]}"):  # across the boundary, past b's heading prefix
                if m.start() < len(a) < m.end():
                    cuts[i].append((m.start(), len(a), _lookup(new, m)))
                    cuts[i + 1].append((h, h + m.end() - len(a) - 1, _lookup(new, m)))
        for (rowid, text), c in zip(rows, cuts):
            if c:
                con.execute("UPDATE pub.chunks SET text = ? WHERE rowid = ?", (_apply(text, c), rowid))


def chunk(text: str, size: int = 1500) -> list[str]:
    """Split on blank lines into ~size chunks, each prefixed by the last markdown heading."""
    out, cur, head = [], [], ""
    for block in re.split(r"\n\s*\n", text):
        for p in block.splitlines() if len(block) > size else [block]:  # e.g. a long table
            if not p.strip():
                continue
            if p.startswith("#"):
                head = p
            if cur != [head] and cur and sum(map(len, cur)) + len(p) > size:
                out.append("\n\n".join(cur))
                cur = [head] if head and head != p else []
            cur.append(p)
    if cur:
        out.append("\n\n".join(cur))
    return out


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = (clean(s).strip() for s in path.read_text().splitlines())
    return [s for s in lines if s]


def files(paths: list[Path]):
    for p in map(Path.resolve, paths):
        if p.is_dir():
            yield from sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in SUFFIXES)
        else:
            yield p


def prune(con: sqlite3.Connection, folders: list[Path]) -> None:
    """Forget documents whose file is gone from an ingested folder (deleted or moved)."""
    for doc_id, path in con.execute("SELECT id, path FROM docs").fetchall():
        p = Path(path)
        if not p.exists() and any(d in p.parents for d in folders):
            with con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("DELETE FROM pub.chunks WHERE doc_id = ?", (doc_id,))
                con.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
            print(f"{doc_id}\tremoved\t{path}", flush=True)


def ingest_file(con: sqlite3.Connection, path: Path, allow: set[str]) -> str:
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    row = con.execute("SELECT id, sha256 FROM docs WHERE path = ?", (str(path),)).fetchone()
    if row and row[1] == sha:
        return f"{row[0]}\tunchanged"
    text = clean(parse(path))
    spans = pii.detect(text)  # slow part, outside the write lock
    doc_id = row[0] if row else secrets.token_hex(8)
    new: dict[str, str] = {}
    with con:  # one transaction over private.db + public.db
        con.execute("BEGIN IMMEDIATE")
        chunks = chunk(mask(con, text, spans, allow, new))
        con.execute("DELETE FROM pub.chunks WHERE doc_id = ?", (doc_id,))
        sweep(con, new)
        con.executemany("INSERT INTO pub.chunks(doc_id, seq, text) VALUES (?, ?, ?)",
                        [(doc_id, i, c) for i, c in enumerate(chunks)])
        con.execute("INSERT OR REPLACE INTO docs VALUES (?, ?, ?)", (doc_id, str(path), sha))
    return f"{doc_id}\t{len(chunks)} chunks"


def run(root: Path, paths: list[Path]) -> int:
    priv = root / "private"
    (root / "public").mkdir(exist_ok=True)
    con = sqlite3.connect(priv / "private.db", isolation_level=None)
    con.execute("ATTACH ? AS pub", (str(root / "public" / "public.db"),))
    con.execute("PRAGMA secure_delete = ON")  # no schema: applies to both files
    con.executescript(SCHEMA)
    allow = {norm(v) for v in _lines(priv / "allow.txt")}
    new: dict[str, str] = {}
    with con:  # deny.txt: learn new values, scrub them from chunks already published
        con.execute("BEGIN IMMEDIATE")
        for v in _lines(priv / "deny.txt"):
            # ponytail: deny.txt values are typed PERSONA, add a "TYPE:" prefix if others need it
            _token(con, "PERSONA", v, new)
        sweep(con, new)
    prune(con, [p.resolve() for p in paths if p.is_dir()])
    failed = 0
    for path in files(paths):
        try:
            print(f"{ingest_file(con, path, allow)}\t{path}", flush=True)
        except Exception as e:  # noqa: BLE001 - one bad file never stops the others
            failed += 1
            print(f"error\t{type(e).__name__}\t{path}", file=sys.stderr, flush=True)
    return 1 if failed else 0
