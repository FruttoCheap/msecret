"""Export in the original format: the file itself with every value ingest masks replaced by its
token. Ingest's masking (masks()) runs on all the text the file holds: body, headers, footers,
notes, comments, alt text, cells, sheet names, form fields. Metadata, link targets and comment
authors go. Images, layout and text drawn as shapes stay as they are (a user decision: what the
detector cannot read leaves in clear). PDF through PyMuPDF; DOCX, PPTX, XLSX as their XML.
redact() returns None for a file exported as Markdown instead: a scan, a part this does not read."""

import bisect
import io
import itertools
import json
import posixpath
import re
import sqlite3
import zipfile
from pathlib import Path

from lxml import etree

from mcp_secret import ingest

SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx"}
# Elements whose text is document text (WordprocessingML, DrawingML, SpreadsheetML, comments).
TEXT = {"t", "delText", "instrText", "text", "oddHeader", "oddFooter", "evenHeader", "evenFooter",
        "firstHeader", "firstFooter"}
WHO = {"author", "initials", "displayName", "userId", "providerId"}  # comment and revision authors
# Parts with copies of the data this does not rewrite (pivot caches, links to other workbooks,
# OLE objects): such a file goes out as Markdown.
OPAQUE = re.compile(r"pivotCache|pivotTable|externalLink|connections\.xml|queryTable"
                    r"|/embeddings/(?!.*\.(docx|pptx|xlsx)$)")
SPACE = "{http://www.w3.org/XML/1998/namespace}space"
LEVEL = {"tr": 0, "row": 0, "tc": 1, "c": 1, "p": 2, "si": 2, "is": 2}  # row, cell, paragraph


def redact(con: sqlite3.Connection, doc_id: str, path: Path, allow: set[str],
           public: set[str]) -> bytes | None:
    """`path` with its values masked as ingest masks them; None: export it as Markdown."""

    head = ingest.heading(path) + "\n\n"  # the name first, as ingest reads it: the model's context

    def mask(text: str) -> list[list]:
        n = len(head)
        return [[max(s - n, 0), e - n, t] for s, e, t in _cuts(con, doc_id, head + text, allow, public)
                if e > n]

    data = path.read_bytes()
    if path.suffix.lower() == ".pdf":
        return _pdf(data, mask)
    if not (pkg := _package(data)):
        return None
    pieces, finish = pkg
    return finish(_spread(pieces, mask("".join(pieces))))


def _cuts(con: sqlite3.Connection, doc_id: str, text: str, allow: set[str],
          public: set[str]) -> list[list]:
    """masks() of a file's text for its document. New values are learned for good and swept out
    of public.db, so a token means one value everywhere. The detector's spans are kept under
    "<doc_id>:file": an export with nothing changed detects nothing."""
    key = f"{doc_id}:file"  # ponytail: the detected table, keyed apart from the parsed text's
    spans = ingest._detect(con, key, text)
    new: dict[str, str] = {}
    with con:
        con.execute("BEGIN IMMEDIATE")
        cuts = ingest.masks(con, text, spans, allow, public, new, doc_id)
        ingest.sweep(con, new)
        con.execute("INSERT OR REPLACE INTO detected VALUES (?, ?, ?)",
                    (key, ingest._sha(text), json.dumps([[s.start, s.end, s.type] for s in spans])))
    return cuts


def _spread(pieces: list[str], cuts: list[list]) -> list[str]:
    """`pieces`, consecutive parts of one text, with `cuts` on that text applied: the token goes
    in the piece its value starts in, the value's characters out of every piece it covers."""
    starts = list(itertools.accumulate(map(len, pieces), initial=0))
    each: list[list] = [[] for _ in pieces]
    for s, e, token in cuts:
        i = bisect.bisect_right(starts, s) - 1
        while i < len(pieces) and starts[i] < e:
            each[i].append((max(s, starts[i]) - starts[i], min(e, starts[i + 1]) - starts[i], token))
            token, i = "", i + 1
    return [ingest._apply(p, c) if c else p for p, c in zip(pieces, each)]


def _pdf(data: bytes, mask) -> bytes | None:
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    if doc.is_encrypted:
        return None
    doc.bake()  # form fields and annotations drawn into the page: masked with the rest
    text, where = [], []  # the text, cleaned; for each of its characters (page, box), None between lines
    for page in doc:
        # ponytail: a page without text or mostly one image is a scan, its text in the pixels
        if not page.get_text().strip() or any(abs(pymupdf.Rect(i["bbox"]) & page.rect) > abs(page.rect) / 2
                                              for i in page.get_image_info()):
            return None
        for block in page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)["blocks"]:
            for line in block.get("lines", ()):
                for ch in (c for span in line["spans"] for c in span["chars"]):
                    c = ingest.clean(ch["c"])
                    text.append(c)
                    where += [(page.number, pymupdf.Rect(ch["bbox"]))] * len(c)
                text.append("\n")
                where.append(None)
            text.append("\n")  # a blank line between blocks, as in Docling's Markdown
            where.append(None)
    for s, e, token in mask("".join(text)):
        boxes: list[list] = []  # one box per line the value is on
        for pno, r in filter(None, where[s:e]):
            if boxes and boxes[-1][0] == pno and r.y0 < boxes[-1][1].y1 and r.y1 > boxes[-1][1].y0:
                boxes[-1][1] |= r
            else:
                boxes.append([pno, pymupdf.Rect(r)])
        for pno, r in boxes:  # the token in the first box, shrunk to fit it: no text, no token
            size = min(r.height * .8, r.width / pymupdf.get_text_length(token, "helv", 1)) if token else 1
            doc[pno].add_redact_annot(r, text=token or None, fontsize=size, fill=(1, 1, 1),
                                      cross_out=False)
            token = ""
    for page in doc:
        page.apply_redactions()  # the text under each box goes, and the pixels of an image there
    doc.set_toc([])
    doc.scrub()  # metadata, XMP, attachments, links, JavaScript, hidden text, thumbnails
    return doc.tobytes(garbage=4, deflate=True, clean=True)  # no earlier revision kept


def _package(data: bytes):
    """An OOXML file's text as pieces, with separators between them as Docling would write them
    (pii.detect is tuned on its Markdown), and the function that writes the masked pieces back
    into a new file. None if it has parts this does not read (OPAQUE). Embedded DOCX, PPTX, XLSX
    (a chart's data) are read the same way."""
    zin = zipfile.ZipFile(io.BytesIO(data))
    names = zin.namelist()
    if any(OPAQUE.search(n) for n in names):
        return None
    drop = {n for n in names if n.startswith("docProps/") or n == "xl/calcChain.xml"}  # metadata, thumbnail
    pieces: list[str] = []
    puts: list = []  # per piece: what writes its new text, None for a separator
    last = [(None, None, None)]  # where the last piece is: (row, cell, paragraph)
    trees, nested = {}, {}

    def add(text: str, put, where: tuple = (None, None, None)) -> None:
        if sep := _sep(last[0], where):
            pieces.append(sep)
            puts.append(None)
        pieces.append(ingest.clean(text))
        puts.append(put)
        last[0] = where

    def tree(name: str):
        trees[name] = etree.fromstring(zin.read(name), etree.XMLParser(huge_tree=True))
        return trees[name]

    zeros = {}  # xlsx: style index -> digits of a "000…" format: a zero-padded code
    if "xl/styles.xml" in names:
        styles = tree("xl/styles.xml")
        fmts = {f.get("numFmtId"): f.get("formatCode") for f in styles.iter("{*}numFmt")}
        xfs = next(styles.iter("{*}cellXfs"), [])
        zeros = {i: len(c) for i, xf in enumerate(xfs)
                 if (c := fmts.get(xf.get("numFmtId"))) and re.fullmatch("0+", c)}
    sst = list(tree("xl/sharedStrings.xml")) if "xl/sharedStrings.xml" in names else []
    used: set[int] = set()  # shared strings read in their cells

    for name in names:
        if name in drop or name in ("xl/styles.xml", "xl/sharedStrings.xml"):
            continue
        if "/embeddings/" in name:
            if not (sub := _package(zin.read(name))):
                return None
            nested[name] = (len(pieces) + 1, len(pieces) + 1 + len(sub[0]), sub[1])
            pieces += ["\n\n", *sub[0]]
            puts += [None] * (1 + len(sub[0]))
            last[0] = (None, None, None)
            continue
        if not name.endswith((".xml", ".rels")):
            continue
        root = tree(name)
        if name == "[Content_Types].xml":
            for o in [o for o in root if o.get("PartName", "")[1:] in drop]:
                root.remove(o)
        elif name.endswith(".rels"):
            base = posixpath.dirname(posixpath.dirname(name))  # word/_rels/x.rels -> word
            for rel in list(root):
                target = rel.get("Target", "")
                if rel.get("TargetMode") == "External":  # link targets: hidden data, as in ingest
                    rel.set("Target", "about:blank")
                elif (target[1:] if target.startswith("/")
                      else posixpath.normpath(posixpath.join(base, target))) in drop:
                    root.remove(rel)
        else:
            _part(name, root, add, zeros, sst, used)
    for i, si in enumerate(sst):  # strings no cell shows any more: still in the file
        if i not in used:
            for t in si.iter("{*}t"):
                add(t.text or "", lambda v, t=t: _set(t, v), (None, None, si))

    def finish(masked: list[str]) -> bytes:
        for old, new, put in zip(pieces, masked, puts):
            if put and new != old:  # ponytail: a shared string masked two ways keeps the last
                put(new)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for name in names:
                if name in drop:
                    continue
                if name in nested:
                    a, b, sub = nested[name]
                    z.writestr(name, sub(masked[a:b]))
                elif name in trees:
                    z.writestr(name, etree.tostring(trees[name], xml_declaration=True,
                                                    encoding="UTF-8", standalone=True))
                else:
                    z.writestr(name, zin.read(name))
        return out.getvalue()

    return pieces, finish


def _sep(a: tuple, b: tuple) -> str:
    """Between pieces at `a` and `b`, (row, cell, paragraph) each: nothing in one paragraph, a
    space in one cell, " | " in one row, a line between rows, else a blank line."""
    if b[2] is not None and b[2] is a[2]:
        return ""
    if b[1] is not None and b[1] is a[1]:
        return " "
    if b[0] is not None and b[0] is a[0]:
        return " | "
    return (" |" if a[0] is not None else "") + ("\n" if a[0] is not None and b[0] is not None
                                                 else "\n\n") + ("| " if b[0] is not None else "")


def _where(el, row=None, cell=None) -> tuple:
    """The row, cell and paragraph `el` is in, None where it is in none."""
    at = {}
    for a in el.iterancestors():
        if (k := LEVEL.get(etree.QName(a).localname)) is not None:
            at.setdefault(k, a)
    return row if row is not None else at.get(0), cell if cell is not None else at.get(1), at.get(2)


def _part(name: str, root, add, zeros: dict[int, int], sst: list, used: set[int]) -> None:
    """Pieces of one XML part; author names blanked, workbook formulas dropped (their literals
    are not read: the cached values stay)."""
    sheet, custom = name.startswith("xl/worksheets/"), re.fullmatch(r"customXml/item\d+\.xml", name)
    gone = []
    for el in root.iter(etree.Element):
        tag = etree.QName(el).localname
        for a in list(el.attrib):
            an = etree.QName(a).localname
            if an in WHO or (an == "name" and tag in ("cmAuthor", "author", "person")) or (
                    tag == "docVar" and an == "val"):
                el.set(a, "")
            elif an in ("descr", "title", "instr") or (an == "name" and tag in ("cNvPr", "docPr")):
                add(el.get(a), lambda v, el=el, a=a: el.set(a, v))
            elif an == "name" and tag == "sheet":  # no [ ] : * ? / \ in a sheet name, 31 at most
                add(el.get(a), lambda v, el=el: el.set("name", re.sub(r"[][:*?/\\]", "", v)[:31] or "-"))
        if tag == "definedNames" or (sheet and tag == "f"):  # formulas name sheets and values
            gone.append(el)
            continue
        if sheet and tag == "c" and el.get("t") == "s":  # its text is in the shared strings
            if (v := el.find("{*}v")) is not None and v.text and v.text.isdigit() and int(v.text) < len(sst):
                used.add(i := int(v.text))
                for t in sst[i].iter("{*}t"):
                    add(t.text or "", lambda x, t=t: _set(t, x), (el.getparent(), el, sst[i]))
            continue
        if len(el) or not el.text:
            continue
        if custom or tag == "author":  # SharePoint properties, comment authors
            el.text = ""
        elif tag == "v" and sheet:
            cell = el.getparent()
            kind = cell.get("t", "n")
            if kind in ("s", "b", "e"):  # a shared string's index, a boolean, an error
                continue
            text = el.text
            if kind == "n" and text.isdigit() and (n := zeros.get(int(cell.get("s", 0)))):
                text = text.zfill(n)
            add(text, lambda v, cell=cell, el=el: _inline(cell, el, v), _where(el))
        elif tag in TEXT or tag in ("v", "f") and not any(
                etree.QName(a).localname in ("numCache", "numLit") for a in el.iterancestors()):
            add(el.text, lambda v, el=el: _set(el, v), _where(el))
    for el in gone:
        el.getparent().remove(el)


def _set(el, text: str) -> None:
    el.text = text
    if "wordprocessingml" in el.tag:  # else Word trims the spaces around a token
        el.set(SPACE, "preserve")


def _inline(cell, v, text: str) -> None:
    """A number cell masked: the token as an inline string."""
    cell.remove(v)
    cell.set("t", "inlineStr")
    etree.SubElement(etree.SubElement(cell, v.tag.replace("}v", "}is")), v.tag.replace("}v", "}t")).text = text
