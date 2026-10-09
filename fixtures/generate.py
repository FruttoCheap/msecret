"""Synthetic Italian documents with gold PII annotations. Never real data.

    uv run python fixtures/generate.py [seed]   # -> fixtures/out/: .txt + .json, a few .docx/.pdf/.png

`generate(seed)` is deterministic and is what tests call. Each document is
{"name", "text", "spans": [{"start", "end", "type", "text"}]} with text[start:end] == span text.
The generator knows where it inserts values, so it writes the annotations.
Thresholds were tuned on generate(SEED, n=60); tests/test_pii.py reports on a held-out seed.
"""

import json
import random
import re
import subprocess
import sys
import textwrap
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from faker import Faker
from faker.providers.address.it_IT import Provider as ItAddress
from faker.providers.ssn.it_IT import checksum

SEED = 1
OUT = Path(__file__).parent / "out"

FOREIGN = ["en_US", "en_GB", "de_DE", "fr_FR", "es_ES", "pt_BR", "ro_RO", "pl_PL", "en_IN", "tr_TR"]
IBAN_LOCALES = ["de_DE", "fr_FR", "es_ES", "en_GB", "nl_NL", "ro_RO", "pl_PL"]
# Names that are also common words, plus romanized names Faker has no locale for.
COMMON_F = ["Rosa", "Sole", "Bianca", "Serena", "Viola", "Gioia", "Vera", "Speranza", "Celeste",
            "Stella", "Aurora", "Marina", "Ambra", "Fortuna", "Luce", "Perla"]
COMMON_M = ["Felice", "Fortunato", "Angelo", "Natale", "Benvenuto", "Sereno", "Fiore", "Leone",
            "Massimo", "Primo", "Settimo", "Santo", "Prospero", "Costante"]
COMMON_LAST = ["Sole", "Rosa", "Ferro", "Leone", "Gallo", "Lupo", "Conte", "Costa", "Fiore", "Bianco",
               "Neri", "Monti", "Porta", "Villa", "Forte", "Piccolo", "Grande", "Santo", "Mare",
               "Pace", "Marino", "Volpe", "Riva", "Valle", "Serra", "Pozzo", "Re", "Papa", "Guerra",
               "Cavallo", "Ricci", "Rossi", "Verdi", "Russo", "Bassi", "Longo", "Martini", "Galli"]
OTHER_NAMES = [("Mohamed", "El Amrani", False), ("Fatima", "Benali", True), ("Wei", "Zhang", False),
               ("Xiaoling", "Chen", True), ("Oluwaseun", "Adeyemi", False), ("Aminata", "Diallo", True),
               ("Youssef", "Bouzid", False), ("Arben", "Hoxha", False), ("Elona", "Shehu", True),
               ("Kwame", "Mensah", False), ("Svetlana", "Ivanova", True), ("Nguyen", "Thi Lan", True)]
IT_CITIES = sorted(ItAddress.cities)  # Faker keeps them in a set: unordered across runs
BIG_CITIES = ["Milano", "Roma", "Napoli", "Torino", "Bologna", "Firenze", "Genova", "Bari", "Palermo",
              "Verona", "Padova", "Brescia", "Bergamo", "Trieste", "Cagliari", "Pescara", "Lecce"]
STREETS = ["Via", "Via", "Via", "Viale", "Piazza", "Corso", "P.zza", "C.so", "Largo", "Vicolo", "Strada"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
        "settembre", "ottobre", "novembre", "dicembre"]
BANKS = ["Intesa Sanpaolo", "UniCredit", "BPER Banca", "Banco BPM", "Banca Sella", "Poste Italiane"]
PHONES = ["3## #######", "3##-#######", "3#########", "+39 3## ### ####", "+39 3#########",
          "(+39) 3## #######", "0039 3## #######", "3## ### ####", "3##.###.####", "02 ########",
          "06 #######", "0#### ######", "0##-#######", "+39 02 ########", "+39 0### ######",
          "06/#######", "+44 20 #### ####", "+49 30 #######", "+33 # ## ## ## ##",
          "+1 (###) ###-####", "+34 ### ### ###", "+41 ## ### ## ##", "+40 7## ### ###"]
DOCS = [("carta d'identità n.", "??#####??"), ("C.I. n.", "?? #######"), ("passaporto n.", "??#######"),
        ("patente di guida n.", "??#######?"), ("documento d'identità nr.", "??#####??")]


def _m(t: str, v: str) -> str:
    """Mark `v` as gold PII of type `t`; `_finish` turns marks into offsets."""
    return f"\x01{t}\x02{v}\x03"


_MARK = re.compile(r"\x01(\w+)\x02(.*?)\x03", re.DOTALL)


def _finish(name: str, marked: str) -> dict:
    marked = unicodedata.normalize("NFC", marked)
    text, spans, last = "", [], 0
    for x in _MARK.finditer(marked):
        text += marked[last : x.start()]
        spans.append({"start": len(text), "end": len(text) + len(x[2]), "type": x[1], "text": x[2]})
        text += x[2]
        last = x.end()
    return {"name": name, "text": text + marked[last:], "spans": spans}


def _ascii(s: str) -> str:
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode())


@dataclass
class Person:
    first: str
    last: str
    female: bool
    loc: str = "it_IT"


class Gen:
    def __init__(self, seed: int):
        self.r = random.Random(seed)
        self.fk = {loc: Faker(loc) for loc in dict.fromkeys(["it_IT", *FOREIGN, *IBAN_LOCALES])}
        for i, f in enumerate(self.fk.values()):
            f.seed_instance(seed * 100 + i)
        self.it = self.fk["it_IT"]

    # --- PII values -------------------------------------------------------------------------
    def person(self) -> Person:
        r, female = self.r, self.r.random() < 0.5
        roll = r.random()
        if roll < 0.15:
            loc = r.choice(FOREIGN)
            f = self.fk[loc]
            return Person(f.first_name_female() if female else f.first_name_male(), f.last_name(), female, loc)
        if roll < 0.2:
            return Person(*r.choice(OTHER_NAMES), loc="en_US")
        first = (
            r.choice(COMMON_F if female else COMMON_M) if r.random() < 0.25
            else self.it.first_name_female() if female else self.it.first_name_male()
        )
        return Person(first, r.choice(COMMON_LAST) if r.random() < 0.25 else self.it.last_name(), female)

    def name(self, p: Person, style: str = "") -> str:
        style = style or self.r.choices(["full", "rev", "upper", "init", "lower"], [50, 20, 12, 12, 3])[0]
        return _m("PERSONA", {
            "full": f"{p.first} {p.last}", "rev": f"{p.last} {p.first}", "upper": f"{p.last} {p.first}".upper(),
            "init": f"{p.first[0]}. {p.last}", "lower": f"{p.first} {p.last}".lower(),
            "first": p.first, "last": p.last,
        }[style])

    def title(self, p: Person) -> str:
        return self.r.choice(["", "", "Dott.ssa ", "Sig.ra ", "Avv. ", "Prof.ssa "] if p.female
                             else ["", "", "Dott. ", "Sig. ", "Avv. ", "Ing. "])

    def nato(self, p: Person) -> str:
        return self.r.choice(["nata" if p.female else "nato", "nato/a"])

    def org(self) -> str:
        r, last = self.r, self.it.last_name()
        v = r.choice([
            self.it.company(), self.it.company(),
            f"Studio {r.choice(['Legale', 'Associato', 'Tecnico', 'Commercialista'])} {last}",
            f"{last} {r.choice(['S.r.l.', 'S.p.A.', 'S.n.c.', 'Srl', 'SpA', '& C. s.a.s.'])}",
            f"{r.choice(['Farmacia', 'Autofficina', 'Panificio', 'Agenzia Immobiliare', 'Hotel'])} {last}",
            self.fk[r.choice(["en_US", "de_DE", "fr_FR", "en_GB"])].company(),
        ])
        return _m("ORG", v)

    def city(self, p: Person | None = None) -> str:
        if p and p.loc != "it_IT" and p.loc in self.fk:
            return _m("LUOGO", self.fk[p.loc].city())
        return _m("LUOGO", self.r.choice(BIG_CITIES) if self.r.random() < 0.5 else self.r.choice(IT_CITIES))

    def address(self, cap: bool = True, p: Person | None = None) -> str:
        r, it = self.r, self.it
        if p and p.loc in ("en_US", "en_GB", "de_DE", "fr_FR") and r.random() < 0.5:
            return _m("INDIRIZZO", self.fk[p.loc].address().replace("\n", ", "))
        street = it.street_name()
        if r.random() < 0.6:
            street = r.choice(STREETS) + " " + street.split(" ", 1)[1]
        n = it.building_number()
        num = r.choice([n, n, f"{n}/{r.choice('ABC')}", f"n. {n}", f"{n}{r.choice('abc')}"])
        if not cap:
            return _m("INDIRIZZO", r.choice([f"{street} {num}", f"{street}, {num}"]))
        extra = r.choice(["", "", "", f", int. {r.randint(1, 20)}", f" scala {r.choice('ABCD')}"])
        cc, prov = f"{it.postcode()} {r.choice(IT_CITIES)}", it.state_abbr()
        return _m("INDIRIZZO", r.choice([
            f"{street} {num}{extra}, {cc} ({prov})", f"{street}, {num} - {cc} {prov}",
            f"{street} {num}{extra}\n{cc} ({prov})", f"{street} {num}, {cc}",
        ]))

    def cf(self) -> str:
        v = self.it.ssn()
        if self.r.random() < 0.25:  # omocodia: digits -> LMNPQRSTUV from the right, new check char
            s = list(v[:15])
            for i in [14, 13, 12, 10, 9, 7, 6][: self.r.randint(1, 7)]:
                s[i] = "LMNPQRSTUV"[int(s[i])]
            v = "".join(s) + checksum("".join(s))
        return _m("CF", v)

    def iban(self) -> str:
        r = self.r
        v = self.it.iban() if r.random() < 0.65 else self.fk[r.choice(IBAN_LOCALES)].iban()
        return _m("IBAN", " ".join(v[i : i + 4] for i in range(0, len(v), 4)) if r.random() < 0.4 else v)

    def piva(self) -> str:
        v = self.it.company_vat()
        return _m("PIVA", self.r.choice([v, v[2:], v[2:], "IT " + v[2:]]))

    def phone(self) -> str:
        if self.r.random() < 0.15:
            return _m("TELEFONO", self.it.phone_number())
        return _m("TELEFONO", self.it.numerify(self.r.choice(PHONES)))

    def email(self, p: Person | None = None) -> str:
        r = self.r
        if not p or r.random() < 0.25:
            return _m("EMAIL", r.choice([self.it.email(), self.it.free_email(), self.it.company_email()]))
        f, last = _ascii(p.first), _ascii(p.last)
        user = r.choice([f"{f}.{last}", f"{f[0]}{last}", f"{last}{r.randint(60, 99)}", f"{f}_{last}"])
        dom = r.choice(["gmail.com", "libero.it", "outlook.it", "yahoo.it", "pec.it", "legalmail.it",
                        "hotmail.com", "tiscali.it", self.it.domain_name()])
        return _m("EMAIL", f"{user}@{dom}")

    def plate(self) -> str:
        v = self.it.license_plate()
        return _m("TARGA", self.r.choice([v, v, f"{v[:2]} {v[2:5]} {v[5:]}"]))

    def doc_id(self) -> str:
        label, fmt = self.r.choice(DOCS)
        return f"{label} {_m('DOCUMENTO', self.it.bothify(fmt).upper())}"

    def birth(self) -> str:
        d = self.it.date_between(date(1940, 1, 1), date(2006, 12, 31))
        return _m("DATA_NASCITA", self.r.choice([
            f"{d:%d/%m/%Y}", f"{d:%d/%m/%Y}", f"{d:%d-%m-%Y}", f"{d:%d.%m.%Y}",
            f"{d.day} {MESI[d.month - 1]} {d.year}", f"{d:%Y-%m-%d}", f"{d.day}/{d.month}/{d.year}",
        ]))

    def birth_en(self) -> str:
        d = self.it.date_between(date(1940, 1, 1), date(2006, 12, 31))
        return _m("DATA_NASCITA", self.r.choice([f"{d:%B} {d.day}, {d.year}", f"{d.day} {d:%B %Y}", f"{d:%d/%m/%Y}"]))

    # --- non-PII filler ---------------------------------------------------------------------
    def when(self) -> str:
        d = self.it.date_between(date(2023, 1, 1), date(2026, 12, 31))
        return self.r.choice([f"{d:%d/%m/%Y}", f"{d.day} {MESI[d.month - 1]} {d.year}", f"{d:%d.%m.%Y}"])

    def eur(self, x: float) -> str:
        return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    # --- documents --------------------------------------------------------------------------
    def contratto(self) -> str:
        r, a, b = self.r, self.person(), self.person()
        kind = r.choice(["LOCAZIONE AD USO ABITATIVO", "PRESTAZIONE D'OPERA PROFESSIONALE", "COMODATO D'USO GRATUITO"])
        return f"""CONTRATTO DI {kind}

Con la presente scrittura privata, redatta in {self.city()} in data {self.when()},

tra

{self.title(a)}{self.name(a, "full")}, {self.nato(a)} a {self.city(a)} il {self.birth()}, residente in {self.address(p=a)}, codice fiscale {self.cf()}, {self.doc_id()}, di seguito "Parte Prima",

e

{self.title(b)}{self.name(b, "upper")}, {self.nato(b)} il {self.birth()} a {self.city(b)}, C.F. {self.cf()}, recapito telefonico {self.phone()}, e-mail {self.email(b)}, di seguito "Parte Seconda",

si conviene e si stipula quanto segue.

Art. 1 - Oggetto
La Parte Prima concede alla Parte Seconda l'immobile sito in {self.address(cap=False)}, piano {r.randint(1, 6)}, composto da {r.randint(2, 5)} vani oltre servizi, censito al catasto fabbricati al foglio {r.randint(1, 99)}, particella {r.randint(100, 999)}, subalterno {r.randint(1, 30)}.

Art. 2 - Corrispettivo
Il corrispettivo annuo è fissato in euro {self.eur(r.randint(4800, 18000))}, da versare in rate mensili anticipate entro il giorno 5 di ogni mese mediante bonifico sul conto corrente IBAN {self.iban()} intestato a {self.name(a, "rev")}, presso {r.choice(BANKS)}.

Art. 3 - Durata
Il contratto ha durata di anni {r.randint(1, 4)} a decorrere dal {self.when()} e si rinnova tacitamente salvo disdetta da comunicarsi con lettera raccomandata almeno sei mesi prima della scadenza.

Art. 4 - Comunicazioni
Ogni comunicazione andrà inviata a {self.name(a, "init")} all'indirizzo {self.email(a)} oppure al numero {self.phone()}. La Sig.ra {self.name(Person(r.choice(COMMON_F), self.it.last_name(), True), "full")} è delegata a ricevere le chiavi.

Letto, confermato e sottoscritto.

{self.city()}, lì {self.when()}

Parte Prima: {self.name(a, "full")}
Parte Seconda: {self.name(b, "rev")}
"""

    def fattura(self) -> str:
        r, cli, ref = self.r, self.person(), self.person()
        items = [(d, r.randint(1, 20), r.randint(15, 400)) for d in r.sample(
            ["Consulenza tecnica", "Manutenzione impianto", "Fornitura materiale elettrico", "Sopralluogo",
             "Licenza software annuale", "Trasporto e consegna", "Ore di assistenza"], 3)]
        rows = "\n".join(f"| {d} | {q} | {self.eur(p)} | {self.eur(q * p)} |" for d, q, p in items)
        tot = sum(q * p for _, q, p in items)
        buyer = (f"{self.org()}\n{self.address()}\nP.IVA {self.piva()}" if r.random() < 0.6
                 else f"{self.title(cli)}{self.name(cli)}\n{self.address(p=cli)}\nC.F. {self.cf()}")
        return f"""{self.org()}
{self.address()}
P.IVA {self.piva()} - Tel. {self.phone()} - {self.email()}

FATTURA N. {r.randint(1, 999)}/{r.randint(2023, 2026)} del {self.when()}

Spett.le
{buyer}

| Descrizione | Quantità | Prezzo unitario | Importo |
| --- | --- | --- | --- |
{rows}

Imponibile: € {self.eur(tot)}
IVA 22%: € {self.eur(tot * 0.22)}
Totale documento: € {self.eur(tot * 1.22)}

Modalità di pagamento: bonifico bancario a 30 gg d.f., {r.choice(BANKS)}, IBAN {self.iban()}
Referente amministrativo: {self.title(ref)}{self.name(ref)} ({self.email(ref)}, tel. {self.phone()})
Codice destinatario SDI: {self.it.bothify("???????").upper()} - Documento emesso ai sensi del DPR 633/1972.
"""

    def mail(self) -> str:
        r, a, b, c = self.r, self.person(), self.person(), self.person()
        if r.random() < 0.3:
            return f"""From: {self.name(a, "full")} <{self.email(a)}>
To: {self.name(b, "full")} <{self.email(b)}>
Subject: Visa application - supporting documents
Date: {self.when()}

Dear {self.name(b, "first")},

please find below the details of {self.name(c, "full")} for the visa application: {self.doc_id()}, date of birth {self.birth_en()}, phone {self.phone()}. {self.name(c, "first")} currently lives at {self.address(p=c)} and works for {self.org()} in {self.city(c)}.

The deposit can be refunded to IBAN {self.iban()}. If anything is missing, call me on {self.phone()} or write to {self.email()}.

Best regards,
{self.name(a, "full")}
{self.org()}
"""
        return f"""Da: {self.name(a)} <{self.email(a)}>
A: {self.name(b, "full")} <{self.email(b)}>
Cc: {self.email()}
Data: {self.when()} {r.randint(8, 19)}:{r.randint(0, 59):02d}
Oggetto: pratica {r.randint(100, 9999)} - dati cliente

Ciao {self.name(b, "first")},

ti inoltro i dati di {self.name(c)} per l'apertura della pratica: codice fiscale {self.cf()}, {self.nato(c)} il {self.birth()} a {self.city(c)}, cellulare {self.phone()}. Mi ha detto che lo trovi in ufficio a {self.city()} fino a venerdì.

Ho sentito {self.name(c, "first")} stamattina: preferisce essere contattato/a via mail ({self.email(c)}). Per il rimborso usare l'IBAN {self.iban()} intestato a {self.name(c, "rev")}.

Ricordati anche di girare il preventivo a {self.title(a)}{self.name(a, "last")} di {self.org()} entro lunedì.

Un saluto,
{self.name(a, "full")}
{self.it.job()} - {self.org()}
{self.address()}
Tel. {self.phone()} | Cell. {self.phone()}
"""

    def cv(self) -> str:
        r, p, ref = self.r, self.person(), self.person()
        y = r.randint(2005, 2018)
        return f"""CURRICULUM VITAE

{self.name(p, r.choice(["full", "upper"]))}

Informazioni personali
Data di nascita: {self.birth()}
Luogo di nascita: {self.city(p)}
Residenza: {self.address(p=p)}
Telefono: {self.phone()}
E-mail: {self.email(p)}
Codice fiscale: {self.cf()}
Patente B, {self.doc_id()}

Esperienze professionali
{y + 4} - oggi    {self.it.job()} presso {self.org()}, {self.city()}
                Responsabile diretto: {self.title(ref)}{self.name(ref)}
{y} - {y + 4}     {self.it.job()} presso {self.org()}, {self.city()}

Istruzione
{y - 2}    Laurea magistrale in {r.choice(["Ingegneria Gestionale", "Economia", "Giurisprudenza", "Lettere"])}, Università degli Studi di {self.city()}, voto {r.randint(95, 110)}/110

Lingue: inglese (B2), francese (A2)
Referenze: {self.name(ref, "full")}, {self.phone()}

Autorizzo il trattamento dei dati personali ai sensi del Regolamento UE 2016/679 (GDPR).
{self.city()}, {self.when()}
{self.name(p, "full")}
"""

    def busta(self) -> str:
        r, p = self.r, self.person()
        base, extra = r.randint(1600, 3200), r.randint(0, 300)
        return f"""{self.org()}
Sede legale: {self.address()} - P.IVA {self.piva()}
Posizione INPS {self.it.numerify("##########")} - PAT INAIL {self.it.numerify("########")}

CEDOLINO PAGA - {r.choice(MESI)} {r.randint(2023, 2026)}

| Dipendente | Codice fiscale | Data di nascita | Matricola | Qualifica |
| --- | --- | --- | --- | --- |
| {self.name(p, r.choice(["upper", "rev"]))} | {self.cf()} | {self.birth()} | {self.it.numerify("00####")} | Impiegato {r.randint(1, 7)}° livello |

| Voce | Riferimento | Competenze | Trattenute |
| --- | --- | --- | --- |
| Retribuzione ordinaria | 168,00 | {self.eur(base)} | |
| Straordinario | {r.randint(1, 20)},00 | {self.eur(extra)} | |
| Contributi IVS | 9,19% | | {self.eur((base + extra) * 0.0919)} |
| IRPEF lorda | | | {self.eur((base + extra) * 0.23)} |

Netto del mese: € {self.eur((base + extra) * 0.68)}
Accredito su IBAN {self.iban()} - {r.choice(BANKS)}
Data assunzione: {self.when()} - Domicilio fiscale: {self.address(p=p)}
"""

    def verbale(self) -> str:
        r = self.r
        ps = [self.person() for _ in range(5)]
        if r.random() < 0.5:
            return f"""VERBALE DI ASSEMBLEA ORDINARIA
Condominio di {self.address()}

Il giorno {self.when()} alle ore 18:30, presso lo studio dell'amministratore {self.title(ps[0])}{self.name(ps[0], "full")} in {self.address(cap=False)}, si è riunita in seconda convocazione l'assemblea dei condomini.

Sono presenti i signori: {self.name(ps[1])} (int. 1, 85 millesimi), {self.name(ps[2])} (int. 3, 120 millesimi), {self.name(ps[3])} per delega di {self.name(ps[4])} (int. 7, 95 millesimi).

Assume la presidenza {self.title(ps[1])}{self.name(ps[1], "full")}; funge da segretario {self.name(ps[2], "full")}.

Punto 1 - Approvazione del rendiconto.
{"La Sig.ra" if ps[3].female else "Il Sig."} {self.name(ps[3], "last")} lamenta infiltrazioni dal lastrico solare. L'impresa {self.org()} (P.IVA {self.piva()}) ha presentato un preventivo di € {self.eur(r.randint(3000, 40000))}. Referente dei lavori: {self.name(ps[4], "full")}, tel. {self.phone()}.

Punto 2 - Varie ed eventuali.
I condomini {self.name(ps[2], "last")} e {self.name(ps[1], "last")} si astengono. Le quote vanno versate sul conto IBAN {self.iban()} intestato al condominio.

L'assemblea si chiude alle ore 20:15.
Il Presidente {self.name(ps[1], "full")} - Il Segretario {self.name(ps[2], "full")}
"""
        return f"""VERBALE DI SINISTRO STRADALE N. {r.randint(1, 900)}/{r.randint(23, 26)}
Comando di Polizia Locale di {self.city()}

In data {self.when()} alle ore {r.randint(0, 23)}:{r.randint(0, 59):02d}, in {self.address(cap=False)}, si è verificato un sinistro tra i seguenti veicoli.

Veicolo A: {r.choice(["Fiat Panda", "Volkswagen Golf", "Renault Clio", "Toyota Yaris"])} targato {self.plate()}, condotto da {self.name(ps[0])}, {self.nato(ps[0])} a {self.city(ps[0])} il {self.birth()}, residente in {self.address(p=ps[0])}, {self.doc_id()}, assicurato con {self.org()}, polizza n. {self.it.numerify("#########")}.

Veicolo B: {r.choice(["Fiat 500", "Ford Fiesta", "Peugeot 208", "Jeep Renegade"])} targa {self.plate()}, di proprietà di {self.org()}, condotto da {self.name(ps[1])}, C.F. {self.cf()}, tel. {self.phone()}.

Testimone: {self.name(ps[2], "full")}, tel. {self.phone()}, e-mail {self.email(ps[2])}.

Dichiarazioni: il conducente del veicolo A, {self.title(ps[0])}{self.name(ps[0], "last")}, dichiara di aver rispettato la precedenza. Il conducente del veicolo B dichiara di non aver visto il veicolo A.

L'agente accertatore: {self.name(ps[3], "full")}, matricola {self.it.numerify("####")}.
"""

    def anagrafica(self) -> str:
        r = self.r
        if r.random() < 0.5:
            p = self.person()
            return f"""SCHEDA ANAGRAFICA CLIENTE

Cognome: {self.name(p, "last")}
Nome: {self.name(p, "first")}
Sesso: {"F" if p.female else "M"}
{"Nata" if p.female else "Nato"} a: {self.city(p)} il {self.birth()}
Codice fiscale: {self.cf()}
Documento: {self.doc_id()} rilasciato dal Comune di {self.city()} il {self.when()}
Residenza: {self.address(p=p)}
Telefono: {self.phone()} - E-mail: {self.email(p)}
Targa veicolo: {self.plate()}
IBAN per addebito: {self.iban()}
Datore di lavoro: {self.org()}

Note: il cliente è stato presentato da {self.name(self.person())}.
"""
        ps = [self.person() for _ in range(r.randint(4, 6))]
        rows = "\n".join(
            f"| {self.name(p, 'last')} | {self.name(p, 'first')} | {self.cf()} | {self.birth()} | {self.city(p)} | {self.phone()} | {self.email(p)} |"
            for p in ps
        )
        q = self.person()
        return f"""ANAGRAFICA CLIENTI - estratto al {self.when()}

| Cognome | Nome | Codice fiscale | Data di nascita | Luogo di nascita | Telefono | E-mail |
| --- | --- | --- | --- | --- | --- | --- |
{rows}

Note:
- {self.name(ps[0], "full")} ha comunicato il nuovo indirizzo: {self.address()}.
- Per {self.name(ps[1])} usare il conto IBAN {self.iban()}.
- Referente aziendale di {self.org()}: {self.name(q, "full")}, {self.email(q)}.
"""


KINDS = ["contratto", "fattura", "mail", "cv", "busta", "verbale", "anagrafica"]


def generate(seed: int = SEED, n: int = 30) -> list[dict]:
    g = Gen(seed)
    return [_finish(f"{i:02d}_{k}", getattr(g, k)()) for i, k in enumerate(KINDS[i % len(KINDS)] for i in range(n))]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    docs = generate(int(sys.argv[1]) if len(sys.argv) > 1 else SEED)
    for i, d in enumerate(docs):
        txt = OUT / f"{d['name']}.txt"
        txt.write_text(d["text"])
        (OUT / f"{d['name']}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
        # A few binary versions with native macOS tools, for the ingest/OCR path.
        if i == 0:
            subprocess.run(["textutil", "-convert", "docx", "-output", txt.with_suffix(".docx"), txt], check=True)
        if i in (1, 2):
            # cupsfilter cuts lines mid-word at 80 columns: wrap on spaces first.
            page = "\n".join(textwrap.fill(ln, 78, break_long_words=False) for ln in d["text"].split("\n"))
            pdf = txt.with_suffix(".pdf")
            with pdf.open("wb") as f:
                subprocess.run(["cupsfilter", "-i", "text/plain"], input=page.encode(), stdout=f,
                               stderr=subprocess.DEVNULL, check=True)
            if i == 2:
                subprocess.run(["sips", "-s", "format", "png", "-Z", "1700", pdf, "--out", txt.with_suffix(".png")],
                               stdout=subprocess.DEVNULL, check=True)
                pdf.unlink()
    print(f"{len(docs)} documents -> {OUT}")


if __name__ == "__main__":
    main()
