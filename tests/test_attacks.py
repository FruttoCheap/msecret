"""Attacks on the pipeline: values hidden by formatting, separators, Unicode, file formats and
the detector's blind spots must not reach public.db (the only thing `serve` can read)."""

import re
import sqlite3
import unicodedata
from pathlib import Path

import docx
import openpyxl
from test_leak import _pdf

from mcp_secret import ingest

CF, IBAN, EMAIL = "RSSMRA80A01H501U", "IT60X0542811101000000123456", "zq.canary@example.org"


def published(tmp_path: Path, docs: dict | None = None, deny: str = "") -> str:
    """Write `docs` (name -> text, or a function writing the file), ingest the folder in-process
    (same code as the CLI, minus the sandbox), return every published chunk."""
    root, d = tmp_path / "data", tmp_path / "docs"
    (root / "private").mkdir(parents=True, exist_ok=True)
    d.mkdir(exist_ok=True)
    (root / "private" / "deny.txt").write_text(deny)
    for name, content in (docs or {}).items():
        content(d / name) if callable(content) else (d / name).write_text(content)
    assert ingest.run(root, [d]) == 0
    con = sqlite3.connect(root / "public" / "public.db")
    return "\n".join(t for (t,) in con.execute("SELECT text FROM chunks ORDER BY doc_id, seq"))


def squash(s: str) -> str:
    """What a reader recovers once markup, separators and compatibility forms are dropped."""
    return re.sub(r"[\W_]", "", unicodedata.normalize("NFKC", s)).casefold()


def leaks(text: str, *values: str) -> list[str]:
    return [v for v in values if squash(v) in squash(text)]


def test_inline_formatting_inside_a_value(tmp_path):
    def contract(p):
        d = docx.Document()
        para = d.add_paragraph("Codice fiscale del titolare: RSSMRA")
        para.add_run("80A01").bold = True  # Docling used to export "RSSMRA **80A01** H501U"
        para.add_run("H501U. Scrivere a zq.canary@")
        para.add_run("example.org").italic = True
        para.add_run(" entro marzo.")
        d.save(p)

    html = "<p>Codice fiscale RSSMRA<b>80A01</b>H501U, saldo a marzo.</p>"
    text = published(tmp_path, {"contratto.docx": contract, "scheda.html": html})
    assert leaks(text, CF, EMAIL) == []


def test_unicode_spaces_inside_iban_and_phone(tmp_path):
    def transfer(p):
        nb = "\u00a0"
        d = docx.Document()
        d.add_paragraph(f"Accredito su IBAN IT60{nb}X054{nb}2811{nb}1010{nb}0000{nb}0123{nb}456.")
        d.add_paragraph(f"Per conferme chiamare il cellulare 333{nb}1234567.")
        d.save(p)

    note = ("IBAN DE89\t3704\t0044\t0532\t0130\t00, GB82  WEST  1234  5698  7654  32 oppure "
            "FR14.2004.1010.0505.0001.3M02.606. Cell. 347\u202f7654321.\n")
    text = published(tmp_path, {"bonifico.docx": transfer, "nota.txt": note})
    ibans = [IBAN, "DE89370400440532013000", "GB82WEST12345698765432", "FR1420041010050500013M02606"]
    assert leaks(text, *ibans, "3331234567", "3477654321") == []


def test_cf_wrapped_across_pdf_lines(tmp_path):
    def request(p):  # two text lines: Docling joins them into "RSSMRA80A0 1H501U"
        _pdf(p, "Il cliente con codice fiscale RSSMRA80A0) Tj 0 -14 Td "
                "(1H501U chiede il rimborso sul conto indicato.", "Richiesta", "Ufficio")

    assert leaks(published(tmp_path, {"richiesta.pdf": request}), CF) == []


def test_identifier_split_across_table_cells(tmp_path):
    def form(p):
        d = docx.Document()
        d.add_paragraph("Codice fiscale (una lettera per casella):")
        for cells in [CF, ["IT", "60", "X", "05428", "11101", "000000123456"]]:
            for cell, v in zip(d.add_table(rows=1, cols=len(cells)).rows[0].cells, cells):
                cell.text = v
        d.save(p)

    assert leaks(published(tmp_path, {"modulo.docx": form}), CF, IBAN) == []


def test_xlsx_number_format_keeps_leading_zero(tmp_path):
    def suppliers(p):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Ragione sociale", "Partita IVA", "Telefono"])
        ws.append(["Ditta individuale", 7123456787, 612345678])  # P.IVA 07123456787, tel 06 12345678
        ws["B2"].number_format, ws["C2"].number_format = "00000000000", "0000000000"
        wb.save(p)

    assert leaks(published(tmp_path, {"fornitori.xlsx": suppliers}), "7123456787", "612345678") == []


def test_hyperlink_targets_are_not_published(tmp_path):
    html = ("<h1>Contatti</h1><p>Per urgenze <a href='https://wa.me/393331234567'>scrivimi su "
            "WhatsApp</a> oppure <a href='mailto:zq.canary%40example.org'>mandami una mail</a>.</p>")
    assert leaks(published(tmp_path, {"contatti.html": html}), "3331234567", "zq.canary") == []


def test_identifier_glued_to_underscore(tmp_path):
    text = published(tmp_path, {"a.txt": f"Il codice fiscale del cliente è {CF}.\n",
                                "b.txt": f"Allegati: CI_{CF}.pdf e contabile_{IBAN}.pdf\n"})
    assert leaks(text, CF, IBAN) == []


def test_fullwidth_and_invisible_characters(tmp_path):
    fullwidth = "".join(chr(ord(c) + 0xFEE0) for c in CF)
    note = (f"Codice fiscale {fullwidth}.\n\nIBAN IT60X054\u200b2811101000000123456 per il rimborso.\n\n"
            f"Coniuge: RSSMRA80\u00adA01H501U, scrivere a zq.canary\u200b@example.org\n")
    assert leaks(published(tmp_path, {"nota.txt": note}), CF, IBAN, EMAIL) == []


def test_deny_entry_with_dotted_capital_i(tmp_path):
    text = published(tmp_path, {"nota.txt": "Pratica riservata, parola chiave: İnce\n"}, deny="İnce\n")
    assert "İnce" not in text


def test_iban_in_lowercase(tmp_path):
    text = published(tmp_path, {"bonifico.txt": f"Gentile cliente,\n\naccreditare l'importo "
                                                f"sull'IBAN {IBAN.lower()}.\n"})
    assert leaks(text, IBAN) == []


def test_iban_with_dashes_or_lowercase(tmp_path):
    mail = """Ciao Bianca,

per l'acconto usate l'IBAN IT59-X030-6909-6061-0000-0063-112 intestato a lei
(copiato così dal suo home banking).

appunti: marco f. vuole iban x rimborso: it31 k053 8712 9000 0000 0234 567

Un abbraccio,
Rosa
"""
    text = published(tmp_path, {"mail.txt": mail})
    assert leaks(text, "IT59X0306909606100000063112", "it31 k053 8712 9000") == []


def test_iban_in_italian_cin_abi_cab_grouping(tmp_path):
    invoice = """FATTURA N. 112/2025

Spett.le Studio Dentistico
Pagamento: bonifico a 30 gg su IBAN IT 59 X 03069 09606 100000063112
Grazie.
"""
    text = published(tmp_path, {"fattura.txt": invoice})
    assert leaks(text, "100000063112", "IT 59 X 03069 09606") == []


def test_codice_fiscale_written_in_groups(tmp_path):
    letter = """AVV. MARCO DE LUCA
VIA TOLEDO 256 - NAPOLI
C.F. dlc mrc 72b14 f839s

Pratica Rossi: codice fiscale del cliente RSS GPP 61D20 F205H, della moglie
FRR-BNC-90L45-L219L (come da tessera sanitaria).
"""
    text = published(tmp_path, {"lettera.txt": letter})
    assert leaks(text, "DLCMRC72B14F839S", "RSSGPP61D20F205H", "FRRBNC90L45L219L") == []


def test_phone_numbers_in_csv_rows(tmp_path):
    rows = """Export rubrica volontari (da sistemare)

nome,cognome,telefono,email,citta
Rosa,Leone,3471234567,rosa.leone@gmail.com,Torino
Sole,Bianchi,+393409876543,sole.b@tiscali.it,Moncalieri
Mei,Lin,3201239876,mei.lin@qq.com,Prato
"""
    text = published(tmp_path, {"rubrica.txt": rows})
    assert leaks(text, "3471234567", "393409876543", "3201239876") == []


def test_birth_dates_under_a_nato_il_header(tmp_path):
    table = """# Anagrafica dipendenti

| Cognome | Nome | Nato il | Cellulare |
|---|---|---|---|
| Sole | Bianca | 01/01/1985 | 333 4455667 |
| Leone | Anna | 15.11.1992 | 320 1122334 |
| Kovalenko | Oleksandr | 1980-08-12 | +48 512 345 678 |
"""
    text = published(tmp_path, {"anagrafica.md": table})
    assert leaks(text, "01/01/1985", "15.11.1992", "1980-08-12") == []


def test_italian_birth_date_variants(tmp_path):
    chat = """[12/03/25, 09:15] Leone: mio padre è nato il 20 aprile 61
[12/03/25, 09:16] Bianca: e la zia?
[12/03/25, 09:17] Leone: nata il 12.III.1970, a Lecce

Scheda iscrizione: Kevin Hoxha, nato il primo maggio 2015 a Tirana.
Data di nascita (aaaammgg): 19900705
"""
    text = published(tmp_path, {"note.txt": chat})
    assert leaks(text, "20 aprile 61", "12.III.1970", "primo maggio 2015", "19900705") == []


def test_english_birth_date_variants(tmp_path):
    agreement = """SERVICE AGREEMENT

between Sole Holdings Ltd and Mr. Hamid Reza Karimi, born 5th March 1975, passport no. K12345678.

New hire: Liam O'Brien, DOB: Feb 14th, 1983.
Contractor: Aoife Kelly, date of birth 14-Feb-1983.
Reference: Ann Lee, born on the 4th of July 1981.
"""
    text = published(tmp_path, {"agreement.txt": agreement})
    assert leaks(text, "5th March 1975", "Feb 14th, 1983", "14-Feb-1983", "4th of July 1981") == []


def test_letter_boxed_form_fields(tmp_path):
    form = """MODULO ADESIONE CONTO - compilare in stampatello

CODICE FISCALE  F R R G L I 9 0 A 4 1 L 7 8 1 O
DATA DI NASCITA  0 1 / 0 1 / 1 9 9 0
IBAN  I T 6 0 X 0 5 4 2 8 1 1 1 0 1 0 0 0 0 0 0 1 2 3 4 5 6
"""
    text = published(tmp_path, {"modulo.txt": form})
    assert leaks(text, "FRRGLI90A41L781O", "01011990", IBAN) == []


def test_health_and_social_security_numbers(tmp_path):
    cards = """Scheda iscrizione: Kevin Hoxha
Tessera sanitaria n. 80380000001234567890

PATIENT NOTE: Mr Oleksandr KOVALENKO, NHS No: 943 476 5919

Hi Kevin,

Payroll needs these for Liam O'Brien before Friday:
SSN 123-45-6789
"""
    text = published(tmp_path, {"schede.txt": cards})
    assert leaks(text, "80380000001234567890", "943 476 5919", "123-45-6789") == []


def test_motorcycle_and_old_province_plates(tmp_path):
    report = """CONSTATAZIONE AMICHEVOLE - note del perito

Veicolo A: Fiat 500 d'epoca targa ROMA A12345.
Veicolo B: motociclo targa AB 12345, conducente Li Na.
Il papà arriva in moto (targa AC 54321) e parcheggia nel cortile.
"""
    text = published(tmp_path, {"constatazione.txt": report})
    assert leaks(text, "AB 12345", "AC 54321", "A12345") == []


def test_all_caps_address(tmp_path):
    card = """# REPUBBLICA ITALIANA - CARTA DI IDENTITÀ

**N. CA12345AB**

COGNOME / SURNAME: SOLE
NOME / NAME: BIANCA
LUOGO E DATA DI NASCITA: ROMA (RM) 01.01.1985
SESSO: F   STATURA: 168   CITTADINANZA: ITA
INDIRIZZO DI RESIDENZA: VIA APPIA NUOVA 1021 ROMA (RM)
CODICE FISCALE: SLO BNC 85A41 H501C
SCADENZA: 01.01.2034

Firma del titolare: Bianca Sole
"""
    assert leaks(published(tmp_path, {"carta_identita.md": card}), "VIA APPIA NUOVA 1021") == []


def test_us_phone_without_keyword(tmp_path):
    mail = """Hi Kevin,

Liam starts Monday. If anything is missing just call me at 212-555-0187,
or his partner Aoife at 917-555-0143 after 6pm.

Thanks!
"""
    assert leaks(published(tmp_path, {"mail.txt": mail}), "212-555-0187", "917-555-0143") == []


def test_landline_with_spaced_slash(tmp_path):
    ad = """Affittasi stanza a Bologna zona Bolognina, 400 euro tutto incluso.
Chiamate ore pasti allo 051 / 22 33 44 (chiedere di Giulia).
"""
    assert leaks(published(tmp_path, {"annuncio.txt": ad}), "051 / 22 33 44") == []


def test_email_with_apostrophe(tmp_path):
    mail = """Payroll needs the personal email of the new hire: liam.o'brien@fintrail.co.uk
Please add it to the onboarding sheet.
"""
    assert leaks(published(tmp_path, {"mail.txt": mail}), "liam.o'") == []


def test_spaced_electronic_id_card_number(tmp_path):
    form = """CENTRO ESTIVO "IL GIRASOLE" - SCHEDA DI ISCRIZIONE 2025

Bambino/a: Kevin Hoxha
Allergie: nessuna

Genitore 1: Elira Hoxha, cell. 389/1122334, elira.hoxha@libero.it
Genitore 2: Arben Hoxha, documento: CA 98765 BD
Ritiro autorizzato anche a: nonna Drita (vicina di casa, Via Gramsci 8, Fano)
"""
    text = published(tmp_path, {"iscrizione.txt": form})
    assert not re.search(r"\bBD\b", text), text
