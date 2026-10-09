"""Round-2 regressions: the token contract, the serve sandbox scope, rule gaps, lone names."""

import re
import sqlite3

import docx
from test_attacks import CF, IBAN, leaks, published

from mcp_secret import server

TOKEN = re.compile(r"\[[A-Z]+(?:_[A-Z]+)*_\d+\]")


def test_published_tokens_are_never_rewritten(tmp_path):
    docs = {"a.txt": f"Accredito su IBAN {IBAN}, per conferme telefono 3331234567.\n",
            "b.txt": "Rata 34 di 120, telefono fisso.\n"}
    published(tmp_path, docs)
    con = sqlite3.connect(tmp_path / "data" / "private" / "private.db")
    learned = {v for (v,) in con.execute("SELECT value FROM entities")}
    assert not learned & {"iban", "telefono", "34", "120"}  # labels and bare numbers: not entities
    # the words become entities late: the sweep must not touch [IBAN_1] / [TELEFONO_1]
    text = published(tmp_path, {"c.txt": "Allegato: contratto_telefono.pdf\n"}, deny="telefono\nIBAN\n")
    assert [t for t in re.findall(r"\[[\w\[\]]*\]", text) if not TOKEN.fullmatch(t)] == []
    assert "telefono" not in TOKEN.sub("", text).lower()  # still masked when glued to "_"
    server.con = server.connect(tmp_path / "data" / "public" / "public.db")
    assert server.search("IBAN_1") and server.search("[TELEFONO_1]")


def test_unicode_dashes_and_invisible_marks_inside_identifiers(tmp_path):
    def card(p):
        d = docx.Document()
        d.add_paragraph("Cellulare 333\u20117654321, ufficio 02\u20101234567.")
        d.save(p)

    note = ("Tel. 333\u2011123\u20114567, IBAN IT60\u2013X054\u20132811\u20131010\u20130000\u20130123"
            "\u2013456, CF RSS\u2011MRA\u201180A01\u2011H501U, cell. 347\ufe0f9876543.\n")
    text = published(tmp_path, {"card.docx": card, "nota.txt": note})
    assert leaks(text, "3337654321", "021234567", "3331234567", IBAN, CF, "3479876543") == []


def test_email_split_by_formatting_before_the_tld(tmp_path):
    def card(p):
        d = docx.Document()
        para = d.add_paragraph("Per appuntamenti scrivere a info.studio77@")
        para.add_run("legalebeta").bold = True  # Docling: "info.studio77@ legalebeta .it"
        para.add_run(".it entro venerdì.")
        d.save(p)

    text = published(tmp_path, {"card.docx": card})
    assert leaks(text, "info.studio77", "legalebeta") == [] and "[EMAIL_1]" in text, text


def test_phone_after_a_numeric_csv_column(tmp_path):
    rows = "nome,eta,telefono\nRosa,34,3471234567\nSole,1,3409876543\nnome,cap,telefono\nRosa,10121,3471234568\n"
    text = published(tmp_path, {"rubrica.txt": rows})
    assert leaks(text, "3471234567", "3409876543", "3471234568") == []


def test_more_birth_date_layouts(tmp_path):
    def form(p):
        d = docx.Document()
        d.add_paragraph("DATA DI NASCITA")  # the boxes are a one-row table below the label
        for c, v in zip(d.add_table(rows=1, cols=10).rows[0].cells, "21/04/1961"):
            c.text = v
        d.save(p)

    note = """Mia madre è nata il 12 marzo del 1958 a Bari.
Reference: Ann Lee, born on the 1st of May, 1980.
DATA DI NASCITA  1  5  /  0  3  /  1  9  7  2
Nato il |0|2|0|5|1|9|6|3|
"""
    text = published(tmp_path, {"modulo.docx": form, "note.txt": note})
    assert leaks(text, "21041961", "12 marzo del 1958", "1st of May, 1980", "15031972",
                 "02051963") == []


def test_more_identifiers_and_handles(tmp_path):
    note = """Payroll: SSN 123 45 6789.
Scrivimi su wa.me/393337654321 oppure apri contatto_3331234567.vcf
Coordinate: ABI 03069 CAB 09606 c/c n. 100000012345
Canali: youtube.com/@mariorossi88, medium.com/@giulia.verdi, IG: @sole_luna_77
Instagram: rosa.leone@gmail.com
"""
    text = published(tmp_path, {"note.txt": note})
    assert leaks(text, "123 45 6789", "393337654321", "3331234567", "100000012345",
                 "mariorossi88", "giulia.verdi", "sole_luna_77", "rosa.leone") == []
    assert not re.search(r"\[PERSONA_\d+\]:? *\[EMAIL_", text), text  # an email is masked once
