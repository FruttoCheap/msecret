"""PII detection. The only contract between the pipeline (A1) and the detector (A2).

Checksum-validated rules for structured identifiers, GLiNER for names/orgs/places/addresses,
plus cheap name/place heuristics. Fusion: overlapping spans of the same type merge; across
types rules beat the model, then the longest wins. Metrics and model choice: fixtures/README.md.
"""

import bisect
import logging
import re
from collections import Counter
from dataclasses import dataclass
from functools import cache

log = logging.getLogger(__name__)

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


def _iban_ok(s: str) -> bool:
    s = s.replace(" ", "")
    return 15 <= len(s) <= 34 and int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


# --- rules ----------------------------------------------------------------------------------
_OMO = r"[\dLMNP-V]"
_DATE = (
    r"(?:\d{1,2}(?P<sep>[./-])\d{1,2}(?P=sep)(?:\d{4}|\d{2})|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}°?\s+(?i:gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic|jan|may|jun|jul|aug|sep|oct|dec)"
    r"[a-z]*\.?\s+\d{4}"
    r"|(?i:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})"
)
_STREET = (
    r"(?:Via|Viale|V\.le|Piazza|P\.zza|P\.za|Piazzale|Corso|C\.so|Largo|Vicolo|Vico|Strada|Contrada"
    r"|C\.da|Località|Loc\.|Borgo|Lungomare|Lungotevere|Salita|Traversa|Rotonda|Incrocio|Stretto"
    r"|Canale|Galleria|Calle|Fondamenta|Frazione|Fraz\.|Circonvallazione|Passaggio|Rione"
    r"|via|viale|piazza|corso)"
)
_UP = "A-ZÀ-ÖØ-ÞĀ-žȘ-ț"  # uppercase Latin, give or take (re has no \p{Lu})
_CITY = rf"[{_UP}][\w'’]*(?:[ -](?:[a-zà-ÿ]{{1,5}}['’]?\s?)?[{_UP}][\w'’]*)*"
_CAP_CITY = rf"\d{{5}},?\s+{_CITY}(?:\s*\([A-Z]{{2}}\))?"
_TITLE = (
    r"(?:Sig(?:\.ra|\.na|\.)|Dott(?:\.ssa|\.)|Avv\.|Ing\.|Prof(?:\.ssa|\.)|Geom\.|Rag\.|Arch\."
    r"|Mr\.|Mrs\.|Ms\.|Dr\.|Gent(?:\.mo|\.ma|ile)|Egregi[oa]|Car[oa]|Ciao|Dear|Hi|Hello"
    r"|(?:Cognome|Nome|Nome e cognome|Cognome e nome|Nominativo|Intestatario|Surname"
    r"|(?:First |Last |Full )?[Nn]ame):)"
)
_NAME = rf"(?:[{_UP}][\w'’-]+|[A-Z]\.)"
_RULES = [
    ("CF", re.compile(rf"(?i)\b[A-Z]{{6}}{_OMO}{{2}}[A-EHLMPR-T]{_OMO}{{2}}[A-Z]{_OMO}{{3}}[A-Z]\b"), _cf_ok),
    ("PIVA", re.compile(r"(?<![\w+])(?:IT ?)?\d{11}(?!\w)"), _piva_ok),
    ("EMAIL", re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+"), None),  # anchored: no O(n²)
    ("DATA_NASCITA", re.compile(  # only in context: "nato a X il", "data di nascita", "born"
        rf"(?i:\bnat[oa]\b|\bborn\b|nascita|date of birth|\bd\.?o\.?b\b)[^\n]{{0,60}}?\b(?P<v>{_DATE})"
    ), None),
    ("DOCUMENTO", re.compile(
        r"\b(?:[A-Z]{2}\d{5}[A-Z]{2}|[A-Z]{2} ?\d{7}[A-Z]?)\b"
        r"|(?i:carta d['’]identit[àa]|\bC\.I\.|passaport[oi]|passport|patente|documento|licen[cs]e)"
        r"\D{0,25}?\b(?P<v>(?:[A-Z]{1,3} ?)?(?=[A-Z0-9]*\d)[A-Z0-9]{5,12})\b"
    ), None),
    # Names the model scores low: after a title or greeting, and "M. Rossi".
    ("PERSONA", re.compile(rf"\b{_TITLE} (?P<v>{_NAME}(?: {_NAME}){{0,2}})"), None),
    ("PERSONA", re.compile(rf"(?<![\w.])[A-Z]\. [{_UP}][a-zà-ÿ'’]{{2,}}"), None),
    # "Pontecuti, lì 14 febbraio 2025": the place line of Italian letters and contracts.
    ("LUOGO", re.compile(rf"(?m)^(?P<v>{_CITY}),? (?:lì |li |addì )?{_DATE}"), None),
    ("TARGA", re.compile(r"\b[A-Z]{2}[ -]?\d{3}[ -]?[A-Z]{2}\b"), None),
    ("INDIRIZZO", re.compile(
        rf"\b{_STREET}\s+[\w'’. ]{{1,40}}?,?\s*(?:n\.\s*)?\d+[A-Za-z]?(?:/\w+)?\b(?![./-]\d)"
        rf"(?:,?\s*(?:int\.?|interno|scala|sc\.|piano)\s*\w+)*(?:[\s,–-]+{_CAP_CITY})?"
        rf"|\b{_CAP_CITY}"
    ), None),
]
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}")
_PHONE = re.compile(r"(?<![\w+(/,-])(?<!\d\.)\(?\+?\d+\)?(?:[ ./-]?\(?\d+\)?)*")
_PHONE_TOK = re.compile(r"\(?\+?\d+\)?")
_PHONE_CTX = re.compile(r"(?i)\b(?:tel|cell|phone|fax|mob|recapit|numero|whatsapp)\w*\W{0,12}$")
_TABLE = re.compile(r"(?m)^\|.*\n(?:\|.*(?:\n|$))+")


def _phone_ok(s: str, before: str) -> bool:
    d = sum(c.isdigit() for c in s)
    if re.fullmatch(r"\d{1,2}([./-])\d{1,2}\1\d{2,4}", s):
        return False
    if s[0] in "+(" or s.startswith("00"):
        return 8 <= d <= 15
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
    """Greedy candidate, then the longest prefix ending on a group that passes mod 97."""
    for m in _IBAN.finditer(text):
        s = m.group()
        for end in range(len(s), 14, -1):
            if (end == len(s) or s[end] == " ") and _iban_ok(s[:end]):
                yield Span(m.start(), m.start() + end, "IBAN")
                break


def _table_births(text: str):
    # ponytail: every date of a markdown table whose header mentions birth; per column if noisy.
    for t in _TABLE.finditer(text):
        head = t.group().split("\n", 1)
        if re.search(r"(?i)nascit|birth", head[0]):
            off = t.start() + len(head[0]) + 1
            for m in re.finditer(_DATE, head[1]):
                yield Span(off + m.start(), off + m.end(), "DATA_NASCITA")


def _rules(text: str):
    for t, rx, ok in _RULES:
        for m in rx.finditer(text):
            g = "v" if "v" in rx.groupindex and m.start("v") >= 0 else 0
            if ok is None or ok(m.group(g)):
                yield Span(m.start(g), m.end(g), t)
    yield from _ibans(text)
    yield from _phones(text)
    yield from _table_births(text)


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
        i = bisect.bisect(out, s.start, key=lambda o: o.start)
        if (i == 0 or out[i - 1].end <= s.start) and (i == len(out) or s.end <= out[i].start):
            out.insert(i, s)
    log.debug("pii spans: %s", dict(Counter(s.type for s in out)))
    return out
