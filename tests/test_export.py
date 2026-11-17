"""export in the original format (redact.py): the file itself with tokens for values, none of
them left anywhere in it (body, headers, comments, metadata, links, cells, formulas, sheet names,
notes); a scan goes out as Markdown; an export with nothing changed detects nothing."""

import io
import re
import sqlite3
import zipfile
from pathlib import Path

import docx
import openpyxl
import pptx
import pymupdf
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn
from PIL import Image, ImageDraw
from test_leak import _pdf, everything

from mcp_secret import cli, ingest, pii

CF, IBAN, EMAIL = "RSSMRA80A01H501U", "IT60X0542811101000000123456", "zq.canary@example.org"
NAME = "Zebulón Quackenbüsh"  # deny.txt
PHONE, PIVA = "3331234567", "01234567897"  # stored as numbers: the P.IVA's 0 is a cell format
CLEAR = [CF, IBAN, EMAIL, *NAME.split(), PHONE, PIVA, PIVA.lstrip("0")]


def _docx(p):
    d = docx.Document()
    d.core_properties.author = NAME
    d.sections[0].header.paragraphs[0].text = f"Riservato {EMAIL}"
    para = d.add_paragraph("Codice fiscale del titolare: RSSMRA")
    para.add_run("80A01").bold = True  # one value over three runs
    para.add_run(f"H501U. Cliente {NAME}, scrivere a ")
    rid = d.part.relate_to(f"mailto:{EMAIL}", RT.HYPERLINK, is_external=True)
    link = para._p.makeelement(qn("w:hyperlink"), {qn("r:id"): rid})
    link.append(para.add_run("qui")._r)
    para._p.append(link)
    d.add_comment(para.runs[0], text=f"verificare {CF}", author=NAME)
    d.save(p)


def _xlsx(p):
    """Written as Excel does, which openpyxl does not: shared strings, a formula, a calcChain."""
    sheet = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s">'
             '<v>2</v></c><c r="D1" t="s"><v>3</v></c></row><row r="2"><c r="A2" t="s"><v>4</v></c>'
             f'<c r="B2"><v>{PHONE}</v></c><c r="C2" s="1"><v>{int(PIVA)}</v></c><c r="D2" t="str">'
             f'<f>"zq.canary"&amp;"@example.org"</f><v>{EMAIL}</v></c></row>')
    strings = ["Nome", "Telefono", "P.IVA", "Email", NAME]
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ct = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{ct}.sheet.main+xml"/>'
        f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{ct}.worksheet+xml"/>'
        f'<Override PartName="/xl/sharedStrings.xml" ContentType="{ct}.sharedStrings+xml"/>'
        f'<Override PartName="/xl/styles.xml" ContentType="{ct}.styles+xml"/>'
        f'<Override PartName="/xl/calcChain.xml" ContentType="{ct}.calcChain+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '</Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/'
        'metadata/core-properties" Target="docProps/core.xml"/></Relationships>',
        "docProps/core.xml": '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/'
        f'2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>{NAME}'
        '</dc:creator></cp:coreProperties>',
        "xl/workbook.xml": f'<workbook {ns} xmlns:r="{rel}"><sheets><sheet name="{NAME}" sheetId="1" '
        f'r:id="rId1"/></sheets><definedNames><definedName name="x">\'{NAME}\'!$A$1</definedName>'
        '</definedNames></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
        f'2006/relationships"><Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{rel}/sharedStrings" Target="sharedStrings.xml"/>'
        f'<Relationship Id="rId3" Type="{rel}/styles" Target="styles.xml"/>'
        f'<Relationship Id="rId4" Type="{rel}/calcChain" Target="calcChain.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f"<worksheet {ns}><sheetData>{sheet}</sheetData></worksheet>",
        "xl/sharedStrings.xml": f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>",
        "xl/styles.xml": f'<styleSheet {ns}><numFmts count="1"><numFmt numFmtId="164" formatCode="00000000000"/>'
        '</numFmts><fonts count="1"><font><sz val="11"/></font></fonts><fills count="1"><fill>'
        '<patternFill/></fill></fills><borders count="1"><border><left/><right/><top/><bottom/>'
        '</border></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0"/>'
        '<xf numFmtId="164" applyNumberFormat="1"/></cellXfs></styleSheet>',
        "xl/calcChain.xml": f'<calcChain {ns}><c r="D2" i="1"/></calcChain>',
    }
    with zipfile.ZipFile(p, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)


def _pptx(p):
    deck = pptx.Presentation()
    deck.core_properties.author = NAME
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = f"Pratica di {NAME}"
    slide.placeholders[1].text = f"Codice fiscale {CF}, IBAN {IBAN}"
    slide.notes_slide.notes_text_frame.text = f"Richiamare {NAME} al {PHONE}"
    deck.save(p)


def _scan(p):
    img = Image.new("RGB", (1200, 300), "white")
    ImageDraw.Draw(img).text((40, 40), f"CF {CF}", fill="black", font_size=48)
    png = io.BytesIO()
    img.save(png, "PNG")
    doc = pymupdf.open()
    doc.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), stream=png.getvalue())
    doc.save(p)


def test_export_originals(tmp_path, monkeypatch):
    root, d, work = tmp_path / "data", tmp_path / "docs", tmp_path / "work"
    (root / "private").mkdir(parents=True)
    (root / "private" / "deny.txt").write_text(NAME + "\n")
    d.mkdir()
    _docx(d / "contratto.docx")
    _xlsx(d / "clienti.xlsx")
    _pptx(d / "pratica.pptx")
    _pdf(d / "fattura.pdf", f"Fattura intestata a CF {CF} IBAN {IBAN}", CF, NAME)
    _scan(d / "scansione.pdf")
    assert ingest.run(root, [d]) == 0
    con = sqlite3.connect(root / "private" / "private.db")
    ids = {Path(p).name: i for i, p in con.execute("SELECT id, path FROM docs")}

    cli.export(root, work, [])
    out = {p.suffix: p for p in (work / "pseudonymized").iterdir()}
    assert sorted(out) == [".docx", ".md", ".pdf", ".pptx", ".xlsx"]
    assert out[".md"].stem == ids["scansione.pdf"]  # its text is pixels: what the agent sees
    for p in out.values():
        blob = everything(p)
        assert not [v for v in CLEAR if v.casefold() in blob.casefold()], p
        assert re.search(r"\[(CF|PERSONA|EMAIL)_\d+\]", blob), p

    published = "".join(t for (t,) in sqlite3.connect(root / "public" / "public.db").execute(
        "SELECT text FROM chunks WHERE doc_id = ?", (ids["contratto.docx"],)))
    who = re.search(r"\[PERSONA_\d+\]", published)[0]  # the same token in the file and in public.db
    para = docx.Document(out[".docx"]).paragraphs[0].text  # (Docling reads the CF's runs spaced:
    assert re.fullmatch(rf"Codice fiscale del titolare: \[CF_\d\]\. Cliente {re.escape(who)}, "
                        "scrivere a qui", para), para  # another value to norm(), another token)
    sheet = openpyxl.load_workbook(out[".xlsx"]).worksheets[0]
    assert NAME not in sheet.title and [c.value for c in sheet[1]] == ["Nome", "Telefono", "P.IVA", "Email"]
    assert all(re.fullmatch(r"\[[A-Z]+_\d+\]", c.value) for c in sheet[2]), [c.value for c in sheet[2]]
    deck = pptx.Presentation(out[".pptx"])
    assert "[CF_" in deck.slides[0].placeholders[1].text and not deck.core_properties.author
    pdf = pymupdf.open(out[".pdf"])
    assert "[CF_" in pdf[0].get_text() and not pdf.metadata["author"]

    def no_detector(text):  # nothing changed: the spans stored by the first export
        raise AssertionError("detected again")

    monkeypatch.setattr(pii, "detect", no_detector)
    cli.export(root, work, [ids["contratto.docx"]])
    assert (work / f"{ids['contratto.docx']}.docx").read_bytes()
