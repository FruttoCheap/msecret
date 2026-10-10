"""Round-2 regressions: the token contract, the serve sandbox scope, rule gaps, lone names."""

import re
import sqlite3

import docx
from test_attacks import CF, IBAN, leaks, published
from test_pii import generate

from mcp_secret import pii, server

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


def test_lone_first_name_after_full_name(tmp_path):
    mail = """Ciao Stefania,

ti inoltro i dati di Mastroianni Fortuna per l'apertura della pratica.

Ho sentito Fortuna stamattina: preferisce essere contattata via mail. Lo stesso per
Paoletta Valle, che mi ha scritto ieri. Paoletta arriva lunedì.
"""
    assert leaks(published(tmp_path, {"mail.txt": mail}), "sentito Fortuna", "Paoletta arriva") == []


def test_names_the_seed5_e2e_run_leaked():
    """Lone parts of a full name, a name after "Referenze:", an ALL-CAPS table row."""
    docs = {d["name"]: d for d in generate(5, 30)}
    leaked = {"02_mail": "Paoletta", "09_mail": "Fortuna", "16_mail": "Bianca", "10_cv": "Bianca",
              "19_verbale": "Chittolini", "23_mail": "Célina", "24_cv": "Ambra Orengo",
              "25_busta": "PACE TELEMACO"}
    missed = []
    for name, value in leaked.items():
        text = docs[name]["text"]
        covered = bytearray(len(text))
        for s in pii.detect(text):
            covered[s.start : s.end] = b"\1" * (s.end - s.start)
        missed += [(name, value, g["start"]) for g in docs[name]["spans"] if g["text"] == value
                   and not all(covered[i] for i in range(g["start"], g["end"]) if text[i].isalnum())]
    assert missed == []


def test_lone_name_parts_stay_in_their_document(tmp_path):
    a = "Il contratto è firmato dal sig. Marco Costa, nato a Genova. Costa riceverà le chiavi.\n"
    b = "Il pacchetto costa poco e la spedizione costa meno del previsto.\n"
    text = published(tmp_path, {"a.txt": a, "b.txt": b})
    assert "Costa riceverà" not in text and "costa poco" in text and "costa meno" in text, text


# --- round 2, second pass: what the verifier still found -----------------------------------


def found(text: str) -> list[tuple[str, str]]:
    """What the rules alone mask: precision checks without the model's noise."""
    return [(s.type, text[s.start : s.end]) for s in pii._rules(text)]


def test_email_split_inside_the_local_part(tmp_path):
    html = "<p>Scrivere a <b>ufficio77</b>.acquisti@<i>dittabeta</i>.com per informazioni.</p>"
    text = published(tmp_path, {"mail.html": html})  # "ufficio77 .acquisti@ dittabeta .com"
    assert leaks(text, "ufficio77", "acquisti") == [] and "[EMAIL_1]" in text, text
    assert found("Scrivi a info@ditta.it .Grazie") == [("EMAIL", "info@ditta.it")]


def test_birth_dates_in_capitals_and_with_spelled_days(tmp_path):
    note = """NATA IL 12 MARZO DEL 1958 A BARI.
Nato il 9 Luglio Del 1977.
NATO A ROMA IL 3 APRILE 1962.
Reference: born on the first of May, 1980. Her brother, born on the twenty-second of June, 1983.
"""
    text = published(tmp_path, {"note.txt": note})
    assert leaks(text, "12 MARZO DEL 1958", "9 Luglio Del 1977", "3 APRILE 1962",
                 "first of May, 1980", "twenty-second of June, 1983") == [], text
