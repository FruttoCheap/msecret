"""PII detection. The only contract between the pipeline (A1) and the detector (A2)."""

import re
from dataclasses import dataclass


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


# ponytail: regex baseline without checksums, replaced by A2's detector.
_RULES = [
    ("EMAIL", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    ("IBAN", r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b"),
    ("CF", r"\b[A-Z]{6}[0-9LMNPQRSTUV]{2}[A-EHLMPR-T][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]\b"),
    ("PIVA", r"\b\d{11}\b"),
    ("TELEFONO", r"(?:\+\d{2}[ .]?)?\b3\d{2}[ .]?\d{6,7}\b"),
]


def fetch_models() -> None:
    """Download detector weights for offline use. Called by `mcp-secret models`."""


def detect(text: str) -> list[Span]:
    """Return sorted, non-overlapping spans. Never logs or returns anything but offsets/types."""
    found = sorted(
        (Span(m.start(), m.end(), t) for t, p in _RULES for m in re.finditer(p, text)),
        key=lambda s: (s.start, -s.end),
    )
    out: list[Span] = []
    for s in found:
        if not out or s.start >= out[-1].end:
            out.append(s)
    return out
