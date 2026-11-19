"""Round-2 regressions: the token contract, the serve sandbox scope, rule gaps, lone names."""

import ctypes
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading

import docx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from test_attacks import CF, IBAN, leaks, published
from test_leak import CLI
from test_pii import generate

from mcp_secret import ingest, pii, server
from mcp_secret.cli import _links, profile, sandbox_cmd

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


def test_boxed_birth_date_under_a_table_header(tmp_path):
    def form(p):
        d = docx.Document()
        t = d.add_table(rows=2, cols=8)
        t.rows[0].cells[0].merge(t.rows[0].cells[7]).text = "DATA DI NASCITA"
        for c, v in zip(t.rows[1].cells, "30111985"):
            c.text = v
        d.save(p)

    md = "| Data di nascita |\n|---|\n| 1 | 4 | 0 | 2 | 1 | 9 | 7 | 0 |\n"
    text = published(tmp_path, {"modulo.docx": form, "modulo.txt": md})
    assert leaks(text, "30111985", "14021970") == [], text


def test_whatsapp_link_with_plus_and_phone_after_a_dash(tmp_path):
    text = published(tmp_path, {"n.txt": "Chat: wa.me/+393287776655, poi scheda-3289990011.vcf\n"})
    assert leaks(text, "3287776655", "3289990011") == [], text
    # a decimal comma is no CSV column: "12,0345..." and "1.234,333..." are amounts
    for amount in ["Il valore è 12,03456789 circa.", "Totale: 1.234,3331234567"]:
        assert found(amount) == [], amount
    assert found("Rosa,1.75,3471234567") == [("TELEFONO", "3471234567")]


def test_account_number_on_the_line_below_its_label(tmp_path):
    text = published(tmp_path, {"n.txt": "Account no.\n12345678\n\nAccount number\n\n31926819\n"})
    assert leaks(text, "12345678", "31926819") == [], text
    assert found("Il conto finale è di 1500000 euro.") == []


def test_more_profile_hosts_and_handle_labels(tmp_path):
    text = published(tmp_path, {"n.txt": "Profili: fb.com/marco.rossi.75 e insta: @sara.bianchi.92\n"})
    assert leaks(text, "marco.rossi.75", "sara.bianchi.92") == [], text
    assert found("Threads: cotone. Discord: problemi. IG: 50") == []


def test_case_folds_the_regex_knows_but_lower_does_not(tmp_path):
    text = published(tmp_path, {"a.txt": "Studio legale: www.yılmaz-hukuk.com.tr\n"}, deny="Yilmaz\n")
    assert [t for t in re.findall(r"\[[^\]]*\]", text) if not TOKEN.fullmatch(t)] == [], text
    assert leaks(text, "yılmaz") == [], text


def test_every_unicode_dash_is_folded(tmp_path):
    text = published(tmp_path, {"n.txt": "Tel. 333⸺765֊4321, CF RSS⸺MRA〜80A01H501U\n"})
    assert leaks(text, "3337654321", CF) == [], text


# --- round 3: recall regressions of the second pass, sandbox writes and process info --------


def test_email_split_before_a_capitalized_domain(tmp_path):
    html = ("<p>Ufficio: ufficio99.resi@ <b>DittaDelta</b>.IT oppure "
            "<b>s</b>.<i>marini</i>@<b>Rinaldi-Impianti</b>.IT, grazie.</p>")
    text = published(tmp_path, {"mail.html": html})
    assert leaks(text, "ufficio99.resi", "DittaDelta", "s.marini", "Rinaldi-Impianti") == [], text
    for v in ["ufficio99.resi@ DittaDelta .IT", "s . marini @ Rinaldi-Impianti .IT",
              "x @ ditta . it", "a .b @ c . IT"]:
        assert found(f"Scrivere a {v} per info.") == [("EMAIL", v)], v
    assert found("Scrivi a info@ditta.it .Grazie") == [("EMAIL", "info@ditta.it")]


def test_account_number_after_a_word_between_label_and_value(tmp_path):
    note = """Saldo sul conto deposito n. 12345678 al 31/12.
Account ID: 87654321
Titoli in custodia sul conto titoli 000045678912.
Bonifico dal conto cointestato n. 123456789 di entrambi.
Versamento su c/c BancoPosta 001234567890.
"""
    text = published(tmp_path, {"n.txt": note})
    assert leaks(text, "12345678", "87654321", "000045678912", "123456789", "001234567890") == [], text
    assert found("Il conto finale è di 1500000 euro.") == []


def test_phone_after_a_word_and_a_number_with_a_comma(tmp_path):
    rows = "Rossi 45,3471234567\nVia Roma 12,3471234567\nBianchi 7,0612345678\n"
    text = published(tmp_path, {"rubrica.txt": rows})
    assert leaks(text, "3471234567", "0612345678") == [], text
    assert found("Rossi 45,3471234567") == [("TELEFONO", "3471234567")]


def sandbox_denies(pid: int, op: str, path: str) -> bool:
    """The kernel's verdict for `op` on `path` in process `pid`'s sandbox; tries nothing."""
    check = ctypes.CDLL(None).sandbox_check
    check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]  # the path is variadic
    return check(pid, op.encode(), 0x40000001, path.encode()) == 1  # FILTER_PATH | NO_REPORT


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS; Windows: test_winbox")
def test_serve_profile_opens_the_links_to_python(tmp_path):
    """uv links a venv's python into cpython-3.12-<platform>, itself a link to the patch release:
    exec reads both links, which realpath skips. Without them serve's box refused to start."""
    (tmp_path / "cpython-3.12.15" / "bin").mkdir(parents=True)
    (tmp_path / "cpython-3.12.15" / "bin" / "python3.12").touch()
    (tmp_path / "cpython-3.12").symlink_to(tmp_path / "cpython-3.12.15")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    exe = tmp_path / "venv" / "bin" / "python"
    exe.symlink_to(tmp_path / "cpython-3.12" / "bin" / "python3.12")
    assert _links(str(exe)) == {str(exe), str(tmp_path / "cpython-3.12")}


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS; Windows: test_winbox")
def test_serve_writes_no_file(tmp_path):
    published(tmp_path, {"a.txt": "Nota di prova per la sessione.\n"})
    env = {**os.environ, "MCP_SECRET_HOME": str(tmp_path / "data")}
    p = subprocess.Popen([CLI, "serve"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    threading.Timer(60, p.kill).start()  # a hung session fails the test, never blocks it

    def rpc(method: str, **params) -> dict:
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                      + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    try:
        assert "result" in rpc("initialize", protocolVersion=LATEST_PROTOCOL_VERSION,
                               capabilities={}, clientInfo={"name": "t", "version": "0"})
        r = rpc("tools/call", name="list_sources", arguments={})
        assert r["result"]["structuredContent"]["result"], r  # the stdio session works
        # existing files outside every denied path: a write there runs later, unsandboxed
        files = [os.path.realpath(sys.executable), "/opt/homebrew/bin/brew", "/usr/bin/true"]
        assert [f for f in files if os.path.exists(f)
                and not sandbox_denies(p.pid, "file-write-data", f)] == []
    finally:
        p.kill()
        p.wait()


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS; Windows: test_winbox")
def test_serve_starts_before_the_first_ingest(tmp_path):
    """A fresh install: Claude launches serve with no index yet. It must stay up and say so."""
    env = {**os.environ, "MCP_SECRET_HOME": str(tmp_path / "data")}
    p = subprocess.Popen([CLI, "serve"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    threading.Timer(60, p.kill).start()

    def rpc(method: str, **params) -> dict:
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    try:
        assert "result" in rpc("initialize", protocolVersion=LATEST_PROTOCOL_VERSION,
                               capabilities={}, clientInfo={"name": "t", "version": "0"})
        r = rpc("tools/call", name="list_sources", arguments={})["result"]
        assert r["isError"] and "no documents indexed" in r["content"][0]["text"]
    finally:
        p.kill()
        p.wait()


PROC_PROBE = """
import ctypes, os
ppid, buf = os.getppid(), ctypes.create_string_buffer(4096)
try:
    os.kill(ppid, 0)
    signal = True
except PermissionError:
    signal = False
print(ctypes.CDLL(None).proc_pidpath(ppid, buf, len(buf)) > 0, signal)
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS; Windows: test_winbox")
def test_serve_cannot_inspect_or_signal_its_parent(tmp_path):
    (tmp_path / "public").mkdir()
    r = subprocess.run(sandbox_cmd(profile(tmp_path), "-c", PROC_PROBE), capture_output=True,
                       text=True, check=True)
    # ponytail: KERN_PROCARGS2 (argv, env) skips the sandbox, see cli.profile
    assert r.stdout.split() == ["False", "False"], r.stdout + r.stderr


def test_dash_lookalikes_are_folded():
    for c in "⁃˗➖─━⎯⁓ー┄╌":
        assert found(f"Tel. 333{c}765{c}4321") == [("TELEFONO", f"333{c}765{c}4321")], hex(ord(c))
        assert found(f"CF RSS{c}MRA{c}80A01H501U")[0][0] == "CF", hex(ord(c))


def test_partita_iva_with_separators():
    for v in ["1234567-8903", "123 456 789 03", "12345 678 903", "IT 0712-3456-787", "023 4567 8912"]:
        assert ("PIVA", v) in found(f"P.IVA {v} del fornitore"), v
    assert found("P.IVA 123 456 789 04 del fornitore") == []  # checksum fails


def test_grouped_account_number_after_its_label():
    for v in ["Conto n. 1234 5678 9012", "Account number: 1234-5678", "conto deposito 0012 3456 7890"]:
        assert ("IBAN", re.search(r"\d.*", v)[0]) in found(v), v
    assert found("Il conto finale è di 1 500 000 euro.") == []


def test_social_label_before_a_profile_url():
    for t in ["Facebook: fb.com/mario.rossi", "YouTube: youtube.com/c/mariorossi",
              "Twitter: twitter.com/mrossi", "Telegram: t.me/mrossi",
              "Profilo: facebook.com/profile.php?id=100012345678901"]:
        assert any(s.type == "PERSONA" and s.end == len(t) for s in pii._rules(t)), t
    assert found("Threads: cotone. Discord: problemi/altro. IG: 50") == []


# --- round 3, second pass: what the verifier still found ---------------------------------


def test_email_capitalized_label_between_spaced_dots(tmp_path):
    def card(p):
        d = docx.Document()
        para = d.add_paragraph("PEC: info77@pec.")
        para.add_run("rossiservizi").bold = True  # Docling: "info77@pec. rossiservizi .it"
        para.add_run(".it per le notifiche.")
        d.save(p)

    html = ("<p>Scrivere a <b>amministrazione</b>@<b>pec</b>.<b>Ditta-Zeta</b>.IT o a "
            "<b>r.villa</b>@<b>Villa</b>.<b>Group</b>.IT entro lunedì.</p>")
    text = published(tmp_path, {"mail.html": html, "card.docx": card})
    assert leaks(text, "amministrazione", "Ditta-Zeta", "r.villa", "Villa", "rossiservizi") == [], text
    for v in ["amministrazione @ pec . Ditta-Zeta .IT", "r.villa @ Villa . Group .IT",
              "info77@pec. rossiservizi .it"]:
        assert found(f"Scrivere a {v} per info.") == [("EMAIL", v)], v
    assert found("Scrivi a info@ditta.it .Grazie. Poi") == [("EMAIL", "info@ditta.it")]
    assert ("EMAIL", "info@ditta.it") in found("Scrivi a info@ditta.it. Tel. 0612345678")


def test_account_number_across_cells_lines_and_more_words():
    for t in ["| Conto deposito | 45645645 |", "Conto deposito\nn. 12312312",
              "conto intestato a Lucia Ferri n. 78978978", "conto titoli 6677889"]:
        assert ("IBAN", re.findall(r"\d+", t)[-1]) in found(t), t
    for t in ["Nel conto economico 2023 2024 i ricavi", "Il conto finale è di 1500000 euro."]:
        assert found(t) == [], t


def test_phone_with_international_prefix_after_a_comma():
    for v in ["+393478889990", "00393471230003"]:
        assert found(f"Rossi 45,{v}") == [("TELEFONO", v)], v


def test_more_dash_lookalikes_and_undeclared_html_charset(tmp_path):
    for c in "═╴╶╸⎼⎽¯‾￣":
        t = ingest.clean(f"Tel. 333{c}765{c}4321")  # ¯ ‾ ￣: NFKC leaves a space and a mark
        assert [k for k, _ in found(t)] == ["TELEFONO"], hex(ord(c))
    html = "<p>Cellulare 333─9876543</p>".encode()  # UTF-8, no charset: bs4 guesses
    text = published(tmp_path, {"c.html": lambda p: p.write_bytes(html)})
    assert leaks(text, "3339876543") == [] and "Ō" not in text, text


def test_profile_id_on_the_short_facebook_host():
    t = "Profilo: fb.com/profile.php?id=100045678901234"
    assert any(s.type == "PERSONA" and s.end == len(t) for s in pii._rules(t)), found(t)


def test_spaced_landline_passing_luhn_is_a_phone():
    for v in ["08995 289769", "06585 656934", "07020 429796"]:
        assert found(f"Tel. {v} ore ufficio") == [("TELEFONO", v)], v


def test_account_marker_after_punctuation_and_dates_are_no_account():
    for t in ["conto intestato a Bianchetti Mario, n. 55667781",
              "conto corrente intestato al Sig. Rossetti n. 66778899",
              "Conto corrente (n. 77889900)", "| Conto | Banca Popolare | n. 5544332 |",
              "conto corrente n.ro 44556677"]:
        assert ("IBAN", re.findall(r"\d+", t)[-1]) in found(t), t
    assert found("Nel conto economico al 31 12 2023 nulla.") == []


def test_profile_id_after_a_social_label():
    for t in ["Facebook: fb.com/profile.php?id=100011122233344",
              "FB: facebook.com/profile.php?id=100022233344455",
              "facebook: www.facebook.com/profile.php?id=100033344455566"]:
        assert any(t.endswith(v) for _, v in found(t)), (t, found(t))


def test_email_with_capitalized_labels_in_a_row():
    for t, v in [("protocollo @ pec . Comune . Roccaraso .IT", "protocollo @ pec . Comune . Roccaraso .IT"),
                 ("ced.amm @ Omega . Servizi . Italia .IT", "ced.amm @ Omega . Servizi . Italia .IT"),
                 ("r.villa @ Villa . Group . It", "r.villa @ Villa . Group . It")]:
        assert ("EMAIL", v) in found(f"Scrivere a {t} entro lunedì"), (t, found(t))
    assert found("Scrivi a info@ditta.it .Grazie. Poi") == [("EMAIL", "info@ditta.it")]
    assert ("EMAIL", "info@ditta.it") in found("Mail info@ditta.it. Tel. 06 1234567")


def test_a_title_is_not_part_of_the_name(tmp_path):
    t = "tra Avv. Concetta Savorgnan, nata a Roma, e PROF.SSA Druso Mariana. Firme: Concetta Savorgnan"
    assert [t[s.start:s.end] for s in pii.detect(t) if s.type == "PERSONA"] == [
        "Concetta Savorgnan", "Druso Mariana", "Concetta Savorgnan"]
    assert pii._untitle("Dott.ssa X", pii.Span(0, 4, "PERSONA")) is None  # a window cut "Dott"
    out = published(tmp_path, {"a.txt": "Il locatore Avv. Concetta Savorgnan firma.",
                               "b.txt": "Firmato: Concetta Savorgnan"})
    assert set(re.findall(r"\[PERSONA_\d+\]", out)) == {"[PERSONA_1]"}, out


# --- context: what stays in clear --------------------------------------------------------------


def test_public_names_and_common_nouns_stay_in_clear(tmp_path):
    t = ("Il sig. Mario Messina, residente a Milano, ha ricevuto da UniCredit S.p.A. un bonifico per "
         "l'Agenzia delle Entrate. Messina ha poi scritto al Comune di Pontecuti e al Tribunale di "
         "Roma. Il lastrico solare è parte comune del condominio, come da catasto fabbricati.\n")
    out = published(tmp_path, {"a.txt": t})
    for v in ["Milano", "UniCredit S.p.A.", "Agenzia delle Entrate", "Roma", "lastrico solare",
              "condominio", "catasto fabbricati"]:
        assert v in out, (v, out)
    assert leaks(out, "Messina", "Pontecuti") == []  # a surname that is a city too, a small town
    assert "Roma" not in published(tmp_path, deny="Roma\n")  # deny.txt beats public.txt
    assert not ingest._public("ORG", "ferrara s.r.l.", ingest.PUBLIC, {})  # a firm, not the city


def test_common_italian_phrases_and_roles_stay_in_clear(tmp_path):
    phrases = [
        "Sono andato a casa dopo la riunione.",
        "Ho fatto una telefonata al responsabile del servizio clienti.",
        "State attenti al cane nel cortile.",
        "Chi ha parlato con lui ieri pomeriggio?",
        "Molti clienti e tutti gli utenti hanno confermato la ricezione.",
        "Oggi sono andato in banca per chiedere informazioni sul mutuo casa.",
        "Ho parlato con il direttore dell agenzia e con il tecnico della ditta.",
        "La presente per comunicare la variazione di residenza.",
        "Gentile Avvocato, resto in attesa di un suo riscontro.",
        "Il dottore mi ha prescritto una visita specialistica di controllo.",
        "Il sindaco e la giunta comunale hanno approvato la delibera di giunta.",
    ]
    for p in phrases:
        spans = pii.detect(p)
        assert spans == [], f"Unexpected detection in {p!r}: {[(s.type, p[s.start:s.end]) for s in spans]}"

    # Test that valid names in greetings and after titles are still properly masked
    t = "Gentile Mario Rossi, le confermo l'appuntamento con l'Avv. Bianchi e il Dott. Verdi.\n"
    out = published(tmp_path, {"test.txt": t})
    assert leaks(out, "Mario Rossi", "Bianchi", "Verdi") == []
    for word in ["Gentile", "Avv.", "Dott."]:
        assert word in out
