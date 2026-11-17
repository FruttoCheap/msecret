"""Ingest: file -> NFC text -> pseudonymized chunks in public.db, entities in private.db."""

import codecs
import email
import email.policy
import functools
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import traceback
import unicodedata
from pathlib import Path

from mcp_secret import pii

SUFFIXES = {".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".md", ".png", ".jpg", ".jpeg",
            ".tif", ".tiff", ".bmp", ".eml", ".txt"}

# What the detector found in each document, for review: offsets and types, no text.
DETECTED = ("CREATE TABLE IF NOT EXISTS detected(doc_id TEXT PRIMARY KEY, text_sha TEXT NOT NULL, "
            "spans TEXT NOT NULL)")
# Categories decided by hand (categories.py): removed = 0 adds one, 1 takes it away for good (a
# rule or the folder that gives it again does not bring it back).
CATS = ("CREATE TABLE IF NOT EXISTS cats(doc_id TEXT NOT NULL, name TEXT NOT NULL, "
        "removed INTEGER NOT NULL, PRIMARY KEY(doc_id, name))")
# Decisions for one document (`deny|allow add --doc`): mask = 1 masks the value there, 0 leaves it
# in clear there. The latest decision wins.
OVERRIDES = ("CREATE TABLE IF NOT EXISTS overrides(doc_id TEXT NOT NULL, value TEXT NOT NULL, "
             "mask INTEGER NOT NULL, PRIMARY KEY(doc_id, value))")
# Entities learned for one document only: no sweep carries them to the others, until a detector or
# deny.txt learns the value for all (_token).
LOCAL = "CREATE TABLE IF NOT EXISTS local(value TEXT PRIMARY KEY)"
# Rollback journals (not WAL) make a transaction over ATTACHed files atomic (super-journal).
SCHEMA = f"""
PRAGMA main.journal_mode = DELETE;
PRAGMA pub.journal_mode = DELETE;
CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities(type TEXT NOT NULL, value TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL);
{DETECTED};
{CATS};
{OVERRIDES};
{LOCAL};
CREATE VIRTUAL TABLE IF NOT EXISTS pub.chunks
    USING fts5(doc_id UNINDEXED, seq UNINDEXED, text, tokenize="unicode61 tokenchars '_'");
CREATE TABLE IF NOT EXISTS pub.names(doc_id TEXT PRIMARY KEY, name TEXT NOT NULL);
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
    from docling.datamodel.pipeline_options import (
        OcrMacOptions,
        PdfPipelineOptions,
        RapidOcrOptions,
    )
    from docling.datamodel.settings import settings
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

    # OcrMac is macOS only. ponytail: RapidOCR runs one language, Italian; its recognizer also
    # reads English words. Check on Italian scans before shipping Windows.
    ocr = (OcrMacOptions(lang=["it-IT", "en-US"]) if sys.platform == "darwin"
           else RapidOcrOptions(lang=["iso:it"]))
    opts = PdfPipelineOptions(artifacts_path=settings.cache_dir / "models",  # `mcp-secret models`
                              ocr_options=ocr)
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


def _html(data: bytes):
    """bs4 guesses an undeclared charset (a UTF-8 "─" read as cp775 turns the phone around it
    into "333ŌöĆ9876543"), and an e-mail body's <meta> names its old charset: bytes that are
    UTF-8 get a BOM, which bs4 trusts first."""
    from docling.datamodel.base_models import DocumentStream

    try:
        data.decode()
        data = codecs.BOM_UTF8 + data
    except UnicodeDecodeError:
        pass
    return DocumentStream(name="page.html", stream=io.BytesIO(data))


def parse(path: Path) -> str:
    """Document text. Only the content: no file name, no metadata (title, author, dates)."""
    kind = path.suffix.lower()
    if kind == ".txt":
        return path.read_text(encoding="utf-8-sig", errors="replace")
    if kind in (".html", ".htm"):
        return _markdown(_html(path.read_bytes()))
    if kind != ".eml":
        return _markdown(_xlsx(path) if kind == ".xlsx" else path)
    # ponytail: attachments are skipped, save and ingest them as files
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    head = "\n".join(f"{h}: {msg[h]}" for h in ("From", "To", "Cc", "Subject") if msg[h])
    part = msg.get_body(("plain", "html"))
    body = part.get_content() if part else ""
    if part and part.get_content_type() == "text/html":
        body = _markdown(_html(body.encode()))
    return f"{head}\n\n{body}"


def clean(text: str) -> str:
    """The text that is detected and published: invisible format characters (zero-width, soft
    hyphen) dropped, compatibility forms (fullwidth, NBSP) folded. NFKC output is NFC too."""
    # ponytail: homoglyphs (a Cyrillic letter in a CF) stay; add a confusables fold if needed
    return unicodedata.normalize("NFKC", "".join(c for c in text if unicodedata.category(c) != "Cf"))


def heading(path: Path) -> str:
    """The file name as ingest reads it: no folders; "_" and "-" as spaces, so the model and
    known values see its words."""
    return clean(" ".join(re.sub(r"[_-]", " ", path.name).split()))


def source(path: Path) -> str:
    """What ingest detects and masks: heading() on the first line, a blank line, the text.
    Detected with the text, the name gets its context: "CV_Rossi.pdf" next to "Mario Rossi"."""
    return clean(heading(path) + "\n\n" + parse(path))


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
    """The token of the value `m` matched. re.IGNORECASE also folds letters lower() keeps apart
    ("yılmaz" matches "yilmaz"): then the value whose own pattern matches, never a bare tag."""
    return table.get(norm(m[0])) or next(t for v, t in table.items() if _pattern([v]).fullmatch(m[0]))


def _token(con: sqlite3.Connection, type_: str, value: str, new: dict, local: bool = False) -> str:
    """Same normalized value -> same token, forever. New values are added to `new`, so the sweeps
    mask them in every document. A `local` one (masked in a single document) is not, and stays
    out of the sweeps until a later non-local call learns it: then it joins `new`."""
    v = norm(value)
    row = con.execute("SELECT token FROM entities WHERE value = ?", (v,)).fetchone()
    if row:
        if not local and con.execute("DELETE FROM local WHERE value = ?", (v,)).rowcount:
            new[v] = row[0]
        return row[0]
    n = con.execute("SELECT count(*) FROM entities WHERE type = ?", (type_,)).fetchone()[0]
    token = f"[{type_}_{n + 1}]"
    con.execute("INSERT INTO entities VALUES (?, ?, ?)", (type_, v, token))
    if local:
        con.execute("INSERT INTO local VALUES (?)", (v,))
    else:
        new[v] = token
    return token


def _merge(cands) -> list[list]:
    """Sorted, non-overlapping [start, end, token]: overlapping ranges merge under the first
    token, so nothing in between leaks."""
    out: list[list] = []
    for s, e, token in sorted(cands):
        if out and s < out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e, token])
    return out


def _apply(text: str, cands: list[tuple[int, int, str]]) -> str:
    """Replace each (start, end, token); overlapping ranges merge (_merge)."""
    out, end = [], 0
    for s, e, token in _merge(cands):
        out += [text[end:s], token]
        end = e
    return "".join(out) + text[end:]


def masks(con: sqlite3.Connection, text: str, spans: list, allow: set[str], public: set[str],
          new: dict, doc_id: str | None = None) -> list[list]:
    """What becomes a [TYPE_N] token in `text`, as _merge()d [start, end, token]: detected spans
    plus every known value (sweep). A one-word name that is a word of a full name in this text
    ("Rossi" after "Mario Rossi") takes that name's token and is no entity: swept through every
    document, the surname Costa would mask coasts. A public ORG or LUOGO stays in clear, unless
    it is such a word ("Mario Messina", then "Messina"). The decisions for this one document
    (`overrides`) come last: a value left in clear there, a value masked there only."""
    here = dict(con.execute("SELECT value, mask FROM overrides WHERE doc_id = ?", (doc_id,)))
    allow = allow | {v for v, mask in here.items() if not mask}
    keep = [(s, v) for s in spans
            if (nv := norm(v := text[s.start:s.end].strip(" _-\t\n[]"))) not in allow and not _NOISE.fullmatch(v)
            and nv not in pii.ROLES
            and pii._clean_val(nv) not in pii.ROLES]
    full = {}  # ponytail: two full names sharing a word, the lone word takes the last one's token
    for s, v in keep:
        if s.type == "PERSONA" and len(words := norm(v).split()) > 1:
            valid_words = [w for w in words if w not in pii.ROLES and len(w) >= 3]
            if valid_words:
                full.update(dict.fromkeys(valid_words, _token(con, s.type, v, new)))
    keep = [(s, v) for s, v in keep if not _public(s.type, norm(v), public, full)]
    cands = [(s.start, s.end, (s.type == "PERSONA" and full.get(norm(v))) or _token(con, s.type, v, new))
             for s, v in keep]
    known = {v: t for ty, v, t in con.execute("SELECT type, value, token FROM entities "
                                              "WHERE value NOT IN (SELECT value FROM local)")
             if v not in allow and not _public(ty, v, public, full) and v not in pii.ROLES
             and pii._clean_val(v) not in pii.ROLES}
    if known:
        cands += [(m.start(), m.end(), _lookup(known, m)) for m in _hits(_pattern(known), text)]
    if mine := {v: _token(con, "PERSONA", v, new, local=True) for v, mask in here.items() if mask}:
        cands += [(m.start(), m.end(), _lookup(mine, m)) for m in _hits(_pattern(mine), text)]
    return _merge(cands)


def sweep(con: sqlite3.Connection, new: dict) -> None:
    """Mask newly learned values in chunks already published, also where a chunk boundary cuts
    one in two. FTS on any of their words finds the candidate documents."""
    # ponytail: a value glued to "_" in an old chunk (ci_<value>) is one FTS term, not found here
    if not new:
        return
    pat = _pattern(new)
    clear: dict[str, set[str]] = {}  # values a document leaves in clear (overrides)
    for d, v in con.execute("SELECT doc_id, value FROM overrides WHERE NOT mask"):
        clear.setdefault(d, set()).add(v)

    def hits(doc_id: str, t: str) -> list[re.Match]:
        return [m for m in _hits(pat, t) if norm(m[0]) not in clear.get(doc_id, ())]

    # ponytail: every name, every sweep; an FTS prefilter like the chunks' if ingest slows on it
    for doc_id, name in con.execute("SELECT doc_id, name FROM pub.names").fetchall():
        if c := [(m.start(), m.end(), _lookup(new, m)) for m in hits(doc_id, name)]:
            con.execute("UPDATE pub.names SET name = ? WHERE doc_id = ?", (_apply(name, c), doc_id))
    query = " OR ".join('"' + w.replace('"', '""') + '"' for v in new for w in v.split())
    for (doc_id,) in con.execute("SELECT DISTINCT doc_id FROM pub.chunks WHERE chunks MATCH ?",
                                 (query,)).fetchall():
        rows = con.execute("SELECT rowid, text FROM pub.chunks WHERE doc_id = ? ORDER BY seq",
                           (doc_id,)).fetchall()
        cuts = [[(m.start(), m.end(), _lookup(new, m)) for m in hits(doc_id, t)] for _, t in rows]
        for i in range(len(rows) - 1):
            a, b = rows[i][1], rows[i + 1][1]
            h = len(b.split("\n\n", 1)[0]) + 2 if b.startswith("#") and "\n\n" in b else 0
            for m in hits(doc_id, f"{a}\n{b[h:]}"):  # across the boundary, past b's heading prefix
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
    lines = (clean(s).strip() for s in path.read_text(encoding="utf-8-sig").splitlines())
    return [s for s in lines if s]


def files(paths: list[Path]):
    for p in map(Path.resolve, paths):
        if p.is_dir():
            yield from sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in SUFFIXES)
        else:
            yield p


# "Comune di Milano", "Tribunale di Roma": the body of a public place is public too. No legal
# suffix is stripped: "Ferrara S.r.l." is a firm named after a surname, not the city.
_BODY = re.compile(
    r"^(?:comune|provincia|città metropolitana|regione|tribunale|prefettura|questura|procura"
    r"(?: della repubblica)?(?: presso il tribunale)?|corte d'appello|camera di commercio"
    r"|università(?: degli studi)?|politecnico|asl|ats|asst|ausl)(?: d(?:i|el|ella|elle|egli))? ")


def _canon(value: str) -> str:
    """A norm()ed value spelled as in public.txt: "'" for "’", "S.p.A"/", SpA" as " s.p.a."."""
    return re.sub(r",? s\.?p\.?a\.?$", " s.p.a.", value.replace("’", "'"))


PUBLIC = frozenset(_canon(norm(s)) for s in _lines(Path(__file__).with_name("public.txt"))
                   if not s.startswith("#"))


def _public(type_: str, value: str, public: set[str], full: dict) -> bool:
    """`value` (norm()ed) is a public name of public.txt, typed ORG or LUOGO, and no word of a
    full name in this text."""
    v = _canon(value)
    return type_ in ("ORG", "LUOGO") and value not in full and (v in public or _BODY.sub("", v) in public)


def lists(con: sqlite3.Connection, priv: Path) -> tuple[set[str], set[str], dict[str, str]]:
    """allow.txt, public.txt and deny.txt as ingest applies them: the values never to mask, the
    public names left in clear, and every deny value learned as an entity (value -> token). A
    value in deny is masked; so is a public name inside one ("Comune di Milano" masks Milano)."""
    # ponytail: deny.txt values are typed PERSONA, add a "TYPE:" prefix if others need it
    deny = {norm(v): _token(con, "PERSONA", v, {}) for v in _lines(priv / "deny.txt")}
    denied = {_canon(d) for d in deny}
    public = PUBLIC - denied - {_BODY.sub("", d) for d in denied}
    return {norm(v) for v in _lines(priv / "allow.txt")} - deny.keys(), public, deny


def digest(path: Path) -> str | None:
    """The file's sha256, None if it cannot be read (deleted, moved, no permission)."""
    try:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except OSError:
        return None


def _doc(doc_id: str | None, path, chunks: int | None, status: str,
         error: str | None = None) -> dict:
    return {"event": "doc", "doc_id": doc_id, "path": str(path), "chunks": chunks, "status": status,
            "error": error}


def _print(ev: dict) -> None:
    """Human output of run(): doc_id, chunk count or status, path; failures on stderr, by type."""
    if ev["event"] != "doc":
        return
    if ev["status"] == "error":
        print(f"error\t{ev['error']}\t{ev['path']}", file=sys.stderr, flush=True)
    else:
        what = f"{ev['chunks']} chunks" if ev["status"] == "indexed" else ev["status"]
        print(f"{ev['doc_id']}\t{what}\t{ev['path']}", flush=True)


def forget(con: sqlite3.Connection, doc_id: str) -> None:
    """Take a document out of both databases: its chunks, its record, what was detected in it,
    its categories and decisions. The entities stay: a value keeps its token in the documents
    that remain."""
    with con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM overrides WHERE doc_id = ?", (doc_id,))
        con.execute("DELETE FROM pub.chunks WHERE doc_id = ?", (doc_id,))
        con.execute("DELETE FROM pub.names WHERE doc_id = ?", (doc_id,))
        con.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
        con.execute("DELETE FROM detected WHERE doc_id IN (?, ?)", (doc_id, f"{doc_id}:file"))  # redact.py
        con.execute("DELETE FROM cats WHERE doc_id = ?", (doc_id,))


def prune(con: sqlite3.Connection, folders: list[Path], emit) -> None:
    """Forget documents whose file is gone from an ingested folder (deleted or moved)."""
    for doc_id, path in con.execute("SELECT id, path FROM docs").fetchall():
        p = Path(path)
        if not p.exists() and any(d in p.parents for d in folders):
            forget(con, doc_id)
            emit(_doc(doc_id, path, 0, "removed"))


def ingest_file(con: sqlite3.Connection, path: Path, allow: set[str], public: set[str],
                force: bool) -> tuple[str, int | None]:
    """(doc_id, chunk count), the count None if the file is unchanged and not `force`d."""
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    # no name yet: indexed before names were published, read once more
    row = con.execute("SELECT id, sha256, (SELECT 1 FROM pub.names WHERE doc_id = id) FROM docs "
                      "WHERE path = ?", (str(path),)).fetchone()
    if row and row[1] == sha and row[2] and not force:
        return row[0], None
    text = source(path)
    spans = _detect(con, row and row[0], text)  # outside the write lock
    doc_id = row[0] if row else secrets.token_hex(8)
    new: dict[str, str] = {}
    with con:  # one transaction over private.db + public.db
        con.execute("BEGIN IMMEDIATE")
        cuts = masks(con, text, spans, allow, public, new, doc_id)
        n = text.index("\n\n")  # the name's line ends: a token across it masks both sides
        name = _apply(text[:n], [(s, min(e, n), t) for s, e, t in cuts if s < n])
        chunks = chunk(_apply(text[n:], [(max(s, n) - n, e - n, t) for s, e, t in cuts if e > n]))
        con.execute("DELETE FROM pub.chunks WHERE doc_id = ?", (doc_id,))
        sweep(con, new)
        con.executemany("INSERT INTO pub.chunks(doc_id, seq, text) VALUES (?, ?, ?)",
                        [(doc_id, i, c) for i, c in enumerate(chunks)])
        con.execute("INSERT OR REPLACE INTO pub.names VALUES (?, ?)", (doc_id, name))
        con.execute("INSERT OR REPLACE INTO docs VALUES (?, ?, ?)", (doc_id, str(path), sha))
        con.execute("INSERT OR REPLACE INTO detected VALUES (?, ?, ?)",
                    (doc_id, _sha(text), json.dumps([[x.start, x.end, x.type] for x in spans])))
    return doc_id, len(chunks)


def connect(root: Path) -> sqlite3.Connection:
    """private.db with public.db attached as `pub`, the schema in place, deletes wiped."""
    con = sqlite3.connect(root / "private" / "private.db", isolation_level=None)
    con.execute("ATTACH ? AS pub", (str(root / "public" / "public.db"),))
    con.execute("PRAGMA secure_delete = ON")  # no schema: applies to both files
    con.executescript(SCHEMA)
    return con


def run(root: Path, paths: list[Path], force: bool = False, emit=_print) -> int:
    """Ingest files and folders; `emit` gets the events of docs/cli-json.md (start, doc, done)."""
    priv = root / "private"
    (root / "public").mkdir(exist_ok=True)
    con = connect(root)
    todo = list(files(paths))
    emit({"event": "start", "files": len(todo)})
    with con:  # deny.txt: learn new values, scrub every deny value from chunks already published
        con.execute("BEGIN IMMEDIATE")  # (also one that was allowed when they were published)
        allow, public, deny = lists(con, priv)
        sweep(con, deny)
    prune(con, [p.resolve() for p in paths if p.is_dir()], emit)
    counts = dict(con.execute("SELECT doc_id, count(*) FROM pub.chunks GROUP BY doc_id"))
    failed = 0
    for path in todo:
        try:
            doc_id, n = ingest_file(con, path, allow, public, force)
            if n is not None:
                counts[doc_id] = n
            emit(_doc(doc_id, path, counts.get(doc_id, 0), "unchanged" if n is None else "indexed"))
        except Exception as e:  # noqa: BLE001 - one bad file never stops the others
            failed += 1
            if os.environ.get("MCP_SECRET_DEBUG"):  # where it failed: frames and the file an OSError names, never the message
                print(*(f"{f.filename}:{f.lineno} {f.name}" for f in traceback.extract_tb(e.__traceback__)[-6:]),
                      getattr(e, "filename", None), sep="\n", file=sys.stderr)
            row = con.execute("SELECT id FROM docs WHERE path = ?", (str(path),)).fetchone()
            emit(_doc(row and row[0], path, None, "error", type(e).__name__))
    emit({"event": "done"})
    return 1 if failed else 0


# The detector: pii.py pins the model revision, the labels and the thresholds.
# ponytail: its code, not the installed gliner; touch pii.py if an upgrade changes what it finds
_DETECTOR = hashlib.sha256(Path(pii.__file__).read_bytes()).hexdigest()


def _sha(text: str) -> str:
    """What the spans stored for a document are keyed by: its text and the detector."""
    return hashlib.sha256((_DETECTOR + text).encode()).hexdigest()


def _detect(con: sqlite3.Connection, doc_id: str | None, text: str) -> list:
    """The detector's spans: those stored for the document while its text and the detector are
    the same (a re-index for allow.txt, deny.txt or public.txt), else detected now, the slow part."""
    row = con.execute("SELECT spans FROM detected WHERE doc_id = ? AND text_sha = ?",
                      (doc_id, _sha(text))).fetchone()
    return [pii.Span(*x) for x in json.loads(row[0])] if row else pii.detect(text)


def review(root: Path, doc_id: str) -> dict | None:
    """A document as ingest would publish it now, in clear: its text and the masked spans, with
    their type and token. The detector's spans are the ones stored at ingest while the parsed
    text and the detector are the same (detection is most of the time); a changed file, a parser
    that now reads it differently or a new detector, is detected again. Writes nothing: it works on an in-memory copy of
    private.db, where a value ingest would learn gets the token it would get. None if `doc_id`
    is unknown."""
    con = sqlite3.connect(":memory:")
    src = sqlite3.connect((root / "private" / "private.db").as_uri() + "?mode=ro", uri=True)
    src.backup(con)
    src.close()
    for table in (DETECTED, OVERRIDES, LOCAL):  # an index from before they existed
        con.execute(table)
    row = con.execute("SELECT path, sha256 FROM docs WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        return None
    sha = digest(Path(row[0]))
    out = {"doc_id": doc_id, "path": row[0], "exists": sha is not None,
           "stale": sha not in (None, row[1]), "text": "", "spans": []}
    if sha:
        allow, public, _ = lists(con, root / "private")
        out["text"] = text = source(Path(row[0]))
        spans = _detect(con, doc_id, text)
        out["spans"] = [{"start": s, "end": e, "type": t[1:-1].rsplit("_", 1)[0], "token": t}
                        for s, e, t in masks(con, text, spans, allow, public, {}, doc_id)]
    return out
