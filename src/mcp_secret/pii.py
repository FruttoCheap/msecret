"""PII detection. The only contract between the pipeline (A1) and the detector (A2).

Checksum-validated rules for structured identifiers, GLiNER for names/orgs/places/addresses,
plus cheap name/place heuristics and a full name's words wherever else the text has them. Fusion: overlapping spans of the same type merge; across
types rules beat the model, then the longest wins, and a span cut by one already kept keeps its
uncovered rest. Metrics and model choice: fixtures/README.md.
"""

import bisect
import re
import unicodedata
from dataclasses import dataclass
from functools import cache

# Fine-tune of urchade/gliner_multi_pii-v1: best PERSONA recall at precision >= 0.95 here, and it
# ships tokenizer.json + safetensors (no protobuf, no pickle). Comparison: fixtures/README.md.
MODEL, REVISION = "E3-JSI/gliner-multi-pii-domains-v1", "66b7951b1e020535d2f470d4b35bc90149a1ed0d"
_FILES = ["gliner_config.json", "model.safetensors", "tokenizer*", "*_tokens*.json"]
# "address" backs up the Italian-only address rules (foreign formats); same-type spans merge.
LABELS = {"person": "PERSONA", "organization": "ORG", "location": "LUOGO", "address": "INDIRIZZO"}
# Per type: the highest threshold that keeps the best recall on the tuning set (see README).
THRESHOLD = {"PERSONA": 0.05, "ORG": 0.05, "LUOGO": 0.1, "INDIRIZZO": 0.5}
WINDOW, OVERLAP = 200, 50  # words per model call: <= ~380 of the backbone's 512 subword tokens


@dataclass(frozen=True)
class Span:
    """A detected PII value in `text[start:end]`.

    Offsets are character offsets on the NFC-normalized text, `end` exclusive.
    `type` is one of: PERSONA, ORG, LUOGO, INDIRIZZO, CF, PIVA, IBAN, EMAIL,
    TELEFONO, TARGA, DOCUMENTO, DATA_NASCITA.
    """

    start: int
    end: int
    type: str


# --- checksums ------------------------------------------------------------------------------
_CF_ODD = [1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14, 16, 10, 22, 25, 24, 23]  # A..Z


def _cf_ok(s: str) -> bool:
    """Codice fiscale check char; omocodia letters are valued like the digits they replace."""
    s = s.upper()
    v = [int(c) if c.isdigit() else ord(c) - 65 for c in s[:15]]
    return chr(65 + sum(_CF_ODD[x] if i % 2 == 0 else x for i, x in enumerate(v)) % 26) == s[15]


def _piva_ok(s: str) -> bool:
    """Partita IVA: Luhn over the 11 digits."""
    d = [int(c) for c in s[-11:]]
    return sum(x if i % 2 == 0 else sum(divmod(2 * x, 10)) for i, x in enumerate(d)) % 10 == 0


# IBAN length per country (SWIFT registry). ponytail: add a country here when it joins.
_IBAN_LEN = {c: int(n) for c, n in re.findall(r"([A-Z]{2})(\d\d)", """
AD24 AE23 AL28 AT20 AZ28 BA20 BE16 BG22 BH22 BI27 BR29 BY28 CH21 CR22 CY28 CZ24 DE22 DJ27 DK18 DO28
EE20 EG29 ES24 FI18 FK18 FO18 FR27 GB22 GE22 GI23 GL18 GR27 GT28 HN28 HR21 HU28 IE22 IL23 IQ23 IS26
IT27 JO30 KW30 KZ20 LB28 LC32 LI21 LT20 LU20 LV21 LY25 MC27 MD24 ME22 MK19 MN20 MR27 MT31 MU30 NI28
NL18 NO15 OM23 PK24 PL28 PS29 PT25 QA29 RO24 RS22 RU33 SA24 SC31 SD18 SE24 SI19 SK24 SM27 SO23 ST25
SV28 TL23 TN24 TR26 UA29 VA22 VG24 XK20 YE30""")}


def _iban_ok(s: str) -> bool:
    """`s`: letters and digits only. Registry length, then mod 97."""
    s = s.upper()
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return len(s) == _IBAN_LEN.get(s[:2]) and int(digits) % 97 == 1


def _alnum(s: str) -> str:
    return re.sub(r"[\W_]", "", s)


# --- rules ----------------------------------------------------------------------------------
_OMO = r"[\dLMNP-V]"
# What can sit between two characters of one identifier: grouping (space, NBSP after clean(),
# dot, dash, slash), a padded table cell border ("| R   | S   |"), a line wrap. Rules validate
# the value with its separators removed, so the checksum keeps the precision.
_SEP = r"\s*+[.|/-]?+\s*+"  # possessive: linear on padded table cells
# The CF rule is letter by letter: "RSS MRA 80A01 H501U", "R S S M R A ...", "RSSMRA80A0 1H501U".
_CF = _SEP.join(["[A-Z]"] * 6 + [_OMO] * 2 + ["[A-EHLMPR-T]"] + [_OMO] * 2 + ["[A-Z]"]
                + [_OMO] * 3 + ["[A-Z]"])
_B, _E = r"(?<![^\W_])", r"(?![^\W_])"  # word boundaries where "_" is a separator (CI_<CF>.pdf)
# Any case: "NATA IL 12 MARZO DEL 1958", "Nato il 9 Luglio Del 1977", "1ST OF MAY".
_MON = r"(?i:(?:gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic|jan|may|jun|jul|aug|sep|oct|dec)[a-z]*)\.?"
_DAY = (r"(?i:\d{1,2}(?:°|st|nd|rd|th)?|primo|(?:(?:twenty|thirty)[- ]?)?(?:first|second|third"  # 1°, 5th,
        r"|(?:four|fif|six|seven|eigh|nin|ten|eleven|twelf|[a-z]+teen|twentie|thirtie)th))")  # primo, first
_DATE = (
    r"(?:\d{1,2}(?P<sep>[./-])\d{1,2}(?P=sep)(?:\d{4}|\d{2})|\d{4}-\d{2}-\d{2}"
    rf"|{_DAY}(?i:\s+of)?[\s-]+{_MON},?(?i:\s+del)?[\s-]+'?(?:\d{{4}}|\d{{2}})\b"  # 1st of May, 1980
    rf"|{_MON}\s+{_DAY},?\s+\d{{4}}"  # Feb 14th, 1983
    r"|\d{1,2}(?P<rs>[./ ])(?:I{1,3}|IV|VI{0,3}|IX|XI{0,2})(?P=rs)\d{2,4})"  # 12.III.1970
)
# Birth context only: compact 19900705, a form's letter boxes "0 1 / 0 1 / 1 9 9 0", "|2|1|0|4|...".
_BIRTH = rf"{_DATE}|\d{{8}}\b|(?:\d[ \t|]+(?:[./-][ \t|]+)?){{7}}\d"
_STREET = (
    r"Via|Viale|V\.le|Piazza|P\.zza|P\.za|Piazzale|Corso|C\.so|Largo|Vicolo|Vico|Strada|Contrada"
    r"|C\.da|Località|Loc\.|Borgo|Lungomare|Lungotevere|Salita|Traversa|Rotonda|Incrocio|Stretto"
    r"|Canale|Galleria|Calle|Fondamenta|Frazione|Fraz\.|Circonvallazione|Passaggio|Rione"
)
_STREET = rf"(?:{_STREET}|{_STREET.upper()}|via|viale|piazza|corso)"  # VIA APPIA on ID cards
_UP = "A-ZÀ-ÖØ-ÞĀ-žȘ-ț"  # uppercase Latin, give or take (re has no \p{Lu})
_CITY = rf"[{_UP}][\w'’]*(?:[ -](?:[a-zà-ÿ]{{1,5}}['’]?\s?)?[{_UP}][\w'’]*)*"
_CAP_CITY = rf"\d{{5}},?\s+{_CITY}(?:\s*\([A-Z]{{2}}\))?"
# A field or table column holding a name.
_LABEL = (r"Cognome|Nome|Nome e cognome|Cognome e nome|Nominativo|Intestatario|Dipendente|Referenze"
          r"|References?|Surname|(?:First |Last |Full )?[Nn]ame")
_TITLE = (
    r"(?:Sig(?:\.ra|\.na|\.)|Dott(?:\.ssa|\.)|Avv\.|Ing\.|Prof(?:\.ssa|\.)|Geom\.|Rag\.|Arch\."
    rf"|Mr\.|Mrs\.|Ms\.|Dr\.|Gent(?:\.mo|\.ma|ile)|Egregi[oa]|Car[oa]|Ciao|Dear|Hi|Hello|(?:{_LABEL}):)"
)
_NAME = rf"(?:[{_UP}][\w'’-]+|[A-Z]\.)"
_RULES = [
    ("CF", re.compile(rf"(?i){_B}{_CF}{_E}"), lambda v: _cf_ok(_alnum(v))),
    ("PIVA", re.compile(rf"{_B}(?<!\+)(?:IT ?)?\d{{11}}{_E}"), _piva_ok),
    # anchored: no O(n²). Spaces: Docling joins formatting runs with one (*x* .y@ *domain* .it,
    # *s* . *y* @ *Domain* .IT); after a spaced dot a label is lowercase or an ALL-CAPS TLD, so
    # "x@y.it .Grazie" ends at ".it".
    ("EMAIL", re.compile(r"(?<![\w.+-])[\w.+-][\w.+'’-]{0,63}(?: \. ?[\w+'’-]+)* ?@ ?[\w-]+"
                         r"(?:\.[\w-]+| \. ?(?:[a-z\d][\w-]*|[A-Z]{2,6}\b))+"), None),
    ("DATA_NASCITA", re.compile(  # only in context: "nato a X il", "data di nascita", "born"; the
        # value on the same line or right below it (a form's boxes: a table under the label)
        rf"(?i:\bnat[oa]\b|\bborn\b|nascita|date of birth|\bd\.?o\.?b\b)[^\n]{{0,60}}?(?:\n[\s|]*)?"
        rf"\b(?P<v>{_BIRTH})"
    ), None),
    ("DOCUMENTO", re.compile(  # CIE, passport, US SSN, tessera sanitaria card; or after a keyword
        r"\b(?:[A-Z]{2} ?\d{5} ?[A-Z]{2}|[A-Z]{2} ?\d{7}[A-Z]?|\d{3}-\d{2}-\d{4}|80\d{18})\b"
        r"|(?i:carta d['’]identit[àa]|\bC\.I\.|passaport[oi]|passport|patente|documento|licen[cs]e"
        r"|tessera sanitaria|\bNHS\b|\bSSN\b|social security)\D{0,25}?\b(?P<v>\d{3}[ -]?\d{2,3}[ -]?\d{4}"
        r"|(?:[A-Z]{1,3} ?)?(?=[A-Z0-9]*\d)[A-Z0-9]{5,20}(?: [A-Z]{2}\b)?)\b"
    ), None),
    # Names the model scores low: after a title or greeting, and "M. Rossi".
    ("PERSONA", re.compile(rf"\b{_TITLE} (?P<v>{_NAME}(?: {_NAME}){{0,2}})"), None),
    ("PERSONA", re.compile(rf"(?<![\w.])[A-Z]\. [{_UP}][a-zà-ÿ'’]{{2,}}"), None),
    # These two keep their anchor in the span, so the value never becomes a bare common word
    # ("Support", "groups") swept through every document. A first name under an informal closing:
    ("PERSONA", re.compile(rf"(?m)^[ \t]*(?:Cheers|Thanks|Thank you|Best|Love|Un abbraccio|Baci"
                           rf"|A presto)[,!.]*[ \t]*\n[ \t]*[{_UP}][a-zà-ÿ'’-]{{2,}}[ \t]*$"), None),
    # Profile URLs and handles carry the name: linkedin.com/in/<slug>, any host's /@<user>,
    # "IG: @<user>", "Skype: <user.name>" (no "@": a digit, "." or "_" inside, so "Discord:
    # problemi" stays). Not an email's local part ("Instagram: x@y.it"): EMAIL masks that once.
    ("PERSONA", re.compile(
        r"(?i:\b(?:linkedin\.com/in|github\.com|gitlab\.com|t\.me|facebook\.com|fb\.com|fb\.me"
        r"|instagram\.com|twitter\.com|x\.com|tiktok\.com|reddit\.com/u(?:ser)?|youtube\.com/(?:c|user))"
        r"/@?|/@|\b(?:skype|telegram|insta(?:gram)?|ig|twitter|tiktok|facebook|fb|youtube|threads"
        r"|snapchat|discord|twitch|mastodon|bluesky)\b"
        r"(?::\s*(?:@|(?=[^\W\d_][\w.-]*(?:\d|[._]\w)))|\s+@))[\w.-]*\w(?![\w.-]*@)"), None),
    ("TELEFONO", re.compile(r"(?i:\bwa\.me/)(?P<v>\+?\d{8,15})\b"), None),  # WhatsApp link as text
    # A bare account number after its label: "ABI 03069 CAB 09606 c/c n. 100000012345",
    # "Account no.\n12345678". Only number words in between: "conto finale di 1500000" is no account.
    ("IBAN", re.compile(r"(?i:\bc/c\b|\bconto\b|\baccount\b)(?:[\s|:.#°]|(?i:\b(?:n|no|nr|num|numero"
                        r"|number|corrente|postale|bancario)\b))*(?P<v>\d{6,18})\b"), None),
    # "Pontecuti, lì 14 febbraio 2025": the place line of Italian letters and contracts.
    ("LUOGO", re.compile(rf"(?m)^(?P<v>{_CITY}),? (?:lì |li |addì )?{_DATE}"), None),
    ("TARGA", re.compile(  # car plates; after "targa": motorcycle AB 12345, old ROMA A12345
        r"\b[A-Z]{2}[ -]?\d{3}[ -]?[A-Z]{2}\b|(?i:\btarg(?:a|at[oa])\b|\bplate\b)\W{0,12}"
        r"(?P<v>(?:[A-Z]{2}|ROMA)[ -]?(?=[A-Z]?\d)[A-Z0-9]{5,6})\b"
    ), None),
    ("INDIRIZZO", re.compile(
        rf"\b{_STREET}\s+[\w'’. ]{{1,40}}?,?\s*(?:n\.\s*)?\d+[A-Za-z]?(?:/\w+)?\b(?![./-]\d)"
        rf"(?:,?\s*(?:int\.?|interno|scala|sc\.|piano)\s*\w+)*(?:[\s,–-]+{_CAP_CITY})?"
        rf"|\b{_CAP_CITY}"
    ), None),
]
# Lookahead: candidates may overlap (two IBANs in one table row), _ibans keeps the first.
_IBAN = re.compile(rf"(?i){_B}(?=([A-Z]{_SEP}[A-Z]{_SEP}\d{_SEP}\d(?:{_SEP}[A-Z0-9]){{11,30}}))")
# Not after a letter or digit, "/" or a "-" not glued to a word (codes, dates), nor after "1."
# (amounts, versions); after "_" or "x-" (contatto_<phone>.vcf, scheda-<phone>.vcf) and a comma
# (CSV rows, "Rosa,34,<phone>") is fine. Groups: one of ./- with optional spaces ("051 / 22 33").
_PHONE = re.compile(r"(?<![^\W_])(?<![+(/])(?:(?<=[^\W\d_]-)|(?<!-))(?<!\d\.)"
                    r"\(?\+?\d+\)?(?:(?: ?[./-] ?| )?\(?\d+\)?)*")
_PHONE_TOK = re.compile(r"\(?\+?\d+\)?")
_PHONE_CTX = re.compile(r"(?i)\b(?:tel|cell|phone|fax|mob|recapit|numero|whatsapp)\w*\W{0,12}$")
# "12,<digits>", "1.234,<digits>": an amount; ",34,<digits>", ",1.75,<digits>": a CSV column
_DECIMAL = re.compile(r"[^\d.,\n][\d.]*\d,$")
_TABLE = re.compile(r"(?m)^\|.*\n(?:\|.*(?:\n|$))+")
_ROW = re.compile(r"(?m)^\|.*")
_NAME_COL = re.compile(rf"\s*(?i:{_LABEL})\s*")
_PART = re.compile(rf"[{_UP}][^\W\d_]*(?:['’-][^\W\d_]+)*")  # one word of a name: Rossi, D'Amico


def _phone_ok(s: str, before: str) -> bool:
    d = sum(c.isdigit() for c in s)
    if _DECIMAL.search(before) or re.fullmatch(r"\d{1,2}([./-])\d{1,2}\1\d{2,4}", s):
        return False
    if s[0] in "+(" or s.startswith("00"):
        return 8 <= d <= 15
    if re.fullmatch(r"\d{3}([.-])\d{3}\1\d{4}", s):  # North American 212-555-0187
        return True
    if _PHONE_CTX.search(before):
        return 6 <= d <= 15
    return (s[0] == "3" and 9 <= d <= 11) or (s[0] == "0" and 8 <= d <= 11)


def _phones(text: str):
    """Digit groups, then the longest run of groups that reads as a phone number."""
    for m in _PHONE.finditer(text):
        toks = [(t.start() + m.start(), t.end() + m.start()) for t in _PHONE_TOK.finditer(m.group())]
        a = 0
        while a < len(toks):
            if a and text[toks[a][0] - 1] in "./-":  # not mid-date or mid-amount
                a += 1
                continue
            for b in range(min(len(toks), a + 8) - 1, a - 1, -1):  # no phone has > 8 groups
                s, e = toks[a][0], toks[b][1]
                if _phone_ok(text[s:e], text[max(0, s - 25) : s]):
                    yield Span(s, e, "TELEFONO")
                    a = b
                    break
            a += 1


def _ibans(text: str):
    """Candidate from each word start, cut at its country's length; any case, any grouping."""
    done = 0
    for m in _IBAN.finditer(text):
        pos = [m.start() + i for i, c in enumerate(m[1]) if c.isalnum()]
        n = _IBAN_LEN.get((text[pos[0]] + text[pos[1]]).upper(), 0)
        if (m.start() >= done and _iban_ok("".join(text[i] for i in pos[:n]))
                and not text[pos[n - 1] + 1 : pos[n - 1] + 2].isalnum()):  # ends where a word does
            done = pos[n - 1] + 1
            yield Span(m.start(), done, "IBAN")


def _tables(text: str):
    # ponytail: every date of a markdown table whose header mentions birth (a form's boxes in
    # the row below "| DATA DI NASCITA |" too); per column if noisy.
    for t in _TABLE.finditer(text):
        head = t.group().split("\n", 1)
        off = t.start() + len(head[0]) + 1
        if re.search(r"(?i)nascit|birth|\bnat[oa]\b|\bd\.?o\.?b\b", head[0]):
            for m in re.finditer(rf"\b(?:{_BIRTH})", head[1]):
                yield Span(off + m.start(), off + m.end(), "DATA_NASCITA")
        # Every cell under a name header ("| Dipendente |" over "| PACE TELEMACO |").
        # ponytail: "| Nome |" over products masks products too; a stricter header list if noisy
        cols = {i for i, h in enumerate(head[0].split("|")) if _NAME_COL.fullmatch(h)}
        for row in _ROW.finditer(text, off, t.end()) if cols else ():
            a = row.start()
            for i, cell in enumerate(row[0].split("|")):
                if i in cols and (m := re.search(r"[^\W\d_](?:.*\S)?", cell)):
                    yield Span(a + m.start(), a + m.end(), "PERSONA")
                a += len(cell) + 1


def _fold(m: re.Match) -> str:
    c, cat = m[0], unicodedata.category(m[0])
    return "-" if cat == "Pd" or c == "\u2212" else " " if cat in ("Zs", "Mn", "Me") else c


def _shadow(text: str) -> str:
    """What the rules read: `text` with every Unicode dash as "-", every Unicode space and
    combining mark (U+FE0F inside a number) as a space. Same length, so offsets hold."""
    return re.sub(r"[^\x00-\x7f]", _fold, text)


def _rules(text: str):
    text = _shadow(text)
    for t, rx, ok in _RULES:
        for m in rx.finditer(text):
            g = "v" if "v" in rx.groupindex and m.start("v") >= 0 else 0
            if ok is None or ok(m.group(g)):
                yield Span(m.start(g), m.end(g), t)
    yield from _ibans(text)
    yield from _phones(text)
    yield from _tables(text)


def _parts(text: str, spans: list[Span]):
    """Each word (3+ letters) of a full name wherever this text has it capitalized or ALL-CAPS:
    "Rossi" or "ROSSI" after "Mario Rossi". This text only: corpus-wide, a surname like Costa
    would mask the word everywhere (ingest gives a part the full name's token, no entity)."""
    parts = set()
    for s in spans:
        v = text[s.start : s.end]
        if s.type == "PERSONA" and "\n" not in v and len(v.split()) > 1:
            parts.update(w for w in v.split() if len(w) >= 3 and _PART.fullmatch(w))
    if parts:
        rx = re.compile(rf"{_B}(?:{'|'.join(map(re.escape, parts))}){_E}", re.IGNORECASE)
        yield from (Span(m.start(), m.end(), "PERSONA") for m in rx.finditer(text) if m[0][0].isupper())


# --- model ----------------------------------------------------------------------------------
_WORD = re.compile(r"\w+(?:[-_]\w+)*|\S")  # GLiNER's own word splitter


def _snapshot(offline: bool = True) -> str:
    from huggingface_hub import snapshot_download

    return snapshot_download(MODEL, revision=REVISION, allow_patterns=_FILES, local_files_only=offline)


@cache
def _gliner():
    from gliner import GLiNER

    return GLiNER.from_pretrained(_snapshot(), local_files_only=True)


def _uncaps(m: re.Match) -> str:
    t = m.group().title()
    return t if len(t) == len(m.group()) else m.group()


def _ner(text: str) -> list[Span]:
    # The model misses ALL-CAPS names ("ROSSI MARIO"): it reads them title-cased, offsets unchanged.
    text = re.sub(r"\b[A-ZÀ-Ý][A-ZÀ-Ý'’]+\b", _uncaps, text)
    words = [(w.start(), w.end()) for w in _WORD.finditer(text)]
    if not words:
        return []
    wins = [(words[i][0], words[min(i + WINDOW, len(words)) - 1][1])
            for i in range(0, max(len(words) - OVERLAP, 1), WINDOW - OVERLAP)]
    chunks = [text[a:b] for a, b in wins]
    preds = _gliner().inference(chunks, list(LABELS), threshold=min(THRESHOLD.values()))
    return [
        Span(a + e["start"], a + e["end"], LABELS[e["label"]])
        for (a, _), ents in zip(wins, preds)
        for e in ents
        if e["score"] >= THRESHOLD[LABELS[e["label"]]]
    ]


def fetch_models() -> None:
    """Download detector weights for offline use. Called by `mcp-secret models`."""
    from gliner import GLiNER

    GLiNER.from_pretrained(_snapshot(offline=False))  # online load also caches the backbone config


def detect(text: str) -> list[Span]:
    """Return sorted, non-overlapping spans. Never logs or returns anything but offsets/types."""
    cands = [(0, s) for s in _rules(text)] + [(1, s) for s in _ner(text)]
    cands += [(1, s) for s in _parts(text, [s for _, s in cands])]
    # Overlapping spans of the same type are one value: take the union (a rule's partial
    # address plus the model's full one must not leave the rest exposed).
    cands.sort(key=lambda c: (c[1].type, c[1].start))
    merged: list[tuple[int, Span]] = []
    for p, s in cands:
        if merged and merged[-1][1].type == s.type and s.start < merged[-1][1].end:
            q, m = merged[-1]
            merged[-1] = (min(p, q), Span(m.start, max(m.end, s.end), s.type))
        else:
            merged.append((p, s))
    out: list[Span] = []
    # Across types: rules first, then longest; ties go to the first type by name (PIVA > TELEFONO).
    for _, s in sorted(merged, key=lambda c: (c[0], c[1].start - c[1].end)):
        i = bisect.bisect(out, s.start, key=lambda o: o.end)
        cut = out[i : bisect.bisect_left(out, s.end, key=lambda o: o.start)]
        if not cut:
            out.insert(i, s)
            continue
        # A span cut by kept ones keeps its uncovered rest: dropping it would leak those words
        # ("Comune di San Pietro A" of an ORG cut by the address "Vico il 11").
        edges = [s.start, *(x for o in cut for x in (o.start, o.end)), s.end]
        for a, b in zip(edges[::2], edges[1::2]):
            m = re.search(r"[^\W_](?:.*[^\W_])?", text[a:b], re.DOTALL)
            if m and len(_alnum(m[0])) >= 3:  # not a stray "A" or "di"
                bisect.insort(out, Span(a + m.start(), a + m.end(), s.type), key=lambda o: o.start)
    return out
