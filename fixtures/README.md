# PII fixtures and metrics

`generate.py` writes ~30 synthetic documents per seed (contracts, invoices, emails, CVs,
payslips, minutes, client records) with Faker it_IT plus foreign locales, and annotates every
value it inserts (`{start, end, type, text}`). Covered on purpose: omocodia CFs, foreign IBANs,
foreign names, names that are common words (Rosa, Sole, Bianca, Ferro, Costa...), the same person
written several ways (`Mario Rossi`, `ROSSI MARIO`, `M. Rossi`, `Dott. Rossi`, `Mario`), PII in
markdown tables. `python fixtures/generate.py` also writes one DOCX (textutil), one PDF
(cupsfilter) and one PNG (sips) for the ingest/OCR path. Output goes to `fixtures/out/`, ignored.

## Protocol

- Recall is leak-oriented: a gold value is found when every letter and digit of it is inside a
  detected span of any type. Precision: a detected span is correct when it overlaps a gold value.
- Model, labels and thresholds were chosen on the tuning set `generate(1, n=60)`.
  `tests/test_pii.py` reports on the held-out set `generate(2)` and fails below the plan's floors
  (recall >= 0.98 on CF, IBAN, PIVA, EMAIL, TELEFONO; >= 0.90 on PERSONA).
- Speed: whole `detect()` (rules + model), CPU, Apple M3 Pro, model already loaded.
- Templates overestimate recall (plan gate 2: measure on 10 real, hand-annotated documents).
  ORG precision is low partly by construction: banks, "Parte Prima" and the like are masked but not
  annotated.

## Held-out: seed 2, 30 docs, 26,894 chars, 211 ms per 1000 chars

| type | gold | recall | precision |
| --- | --- | --- | --- |
| CF | 27 | 1.000 | 1.000 |
| DATA_NASCITA | 28 | 1.000 | 1.000 |
| DOCUMENTO | 18 | 1.000 | 1.000 |
| EMAIL | 43 | 1.000 | 1.000 |
| IBAN | 24 | 1.000 | 1.000 |
| INDIRIZZO | 48 | 0.979 | 1.000 |
| LUOGO | 57 | 1.000 | 0.960 |
| ORG | 38 | 0.947 | 0.578 |
| PERSONA | 136 | 1.000 | 0.993 |
| PIVA | 14 | 1.000 | 1.000 |
| TARGA | 8 | 1.000 | 1.000 |
| TELEFONO | 47 | 1.000 | 1.000 |

Tuning set (seed 1, 60 docs, 56,005 chars, 212 ms per 1000 chars): PERSONA 1.000 / 1.000,
ORG 0.948 / 0.552, LUOGO 0.977 / 0.945, every rule type 1.000 recall (TELEFONO precision 0.974).
PERSONA reached 1.000 once a full name's words are masked wherever else the same document
capitalizes them; before that it was 0.956 held-out and 0.962 tuning.

## Model choice (seed 1, 30 docs, same rules, labels person/organization/location/address)

Criterion: best PERSONA recall with PERSONA precision >= 0.95 (a mask that is never a name is
noise the agent has to read around); ties go to fewer dependencies.

| model | thr | PERSONA R / P | ORG R / P | LUOGO R / P | ms/1k chars | notes |
| --- | --- | --- | --- | --- | --- | --- |
| E3-JSI/gliner-multi-pii-domains-v1 | 0.05 | 0.966 / 1.000 | 0.919 / 0.538 | 0.954 / 0.945 | 224 | chosen: fine-tune of multi_pii-v1, ships tokenizer.json + safetensors, Apache-2.0 |
| urchade/gliner_multi_pii-v1 | 0.05 | 0.960 / 0.994 | 0.946 / 0.500 | 0.985 / 0.963 | 221 | no tokenizer in repo: mdeberta conversion needs `protobuf` under transformers 5; pickle weights |
| urchade/gliner_multi-v2.1 | 0.30 | 0.926 / 0.964 | 0.892 / 0.614 | 0.954 / 0.965 | 223 | 0.989 / 0.778 at 0.05; needs `protobuf` |
| knowledgator/gliner-x-base | 0.40 | 0.966 / 0.988 | 0.892 / 0.473 | 0.985 / 0.894 | 208 | needs `stanza` (patched to whitespace splitting to test); mt5-base, 2 GB |

Model-only time (rules excluded); INDIRIZZO recall was 1.000 for all four. The address label
is there for foreign formats (German, British, US) that the Italian address rules miss: seed 3,
used only to debug rules, went from 0.938 to 1.000 with it. Seed 2 was run once mid-way (it
showed that address gap) and otherwise only for this report.

Thresholds in `pii.THRESHOLD` are, per type, the highest grid value that keeps the best tuning
recall: PERSONA 0.05, ORG 0.05, LUOGO 0.10, INDIRIZZO 0.50.
