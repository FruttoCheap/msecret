"""Precision/recall per type of pii.detect on generated fixtures (held-out seed).

Recall is leak-oriented: a gold value counts as found when every letter and digit of it is
inside some detected span, of any type. Precision: a detected span counts when it overlaps a
gold value. `uv run python tests/test_pii.py SEED [N]` prints the table for any fixture set.
"""

import runpy
import sys
import time
from collections import Counter
from pathlib import Path

from mcp_secret import pii

generate = runpy.run_path(str(Path(__file__).parents[1] / "fixtures" / "generate.py"))["generate"]
HELDOUT = 2  # thresholds were tuned on generate(1, n=60)
MIN_RECALL = {"CF": 0.98, "IBAN": 0.98, "PIVA": 0.98, "EMAIL": 0.98, "TELEFONO": 0.98, "PERSONA": 0.90}


def evaluate(seed: int, n: int = 30) -> tuple[dict, str]:
    docs = generate(seed, n)
    pii.detect("warm up")  # load the model outside the timing
    gold, found, pred, hit, chars, secs = Counter(), Counter(), Counter(), Counter(), 0, 0.0
    for d in docs:
        text = d["text"]
        t0 = time.perf_counter()
        spans = pii.detect(text)
        secs += time.perf_counter() - t0
        chars += len(text)
        covered = bytearray(len(text))
        for s in spans:
            covered[s.start : s.end] = b"\1" * (s.end - s.start)
            pred[s.type] += 1
            hit[s.type] += any(g["start"] < s.end and s.start < g["end"] for g in d["spans"])
        for g in d["spans"]:
            gold[g["type"]] += 1
            found[g["type"]] += all(covered[i] for i in range(g["start"], g["end"]) if text[i].isalnum())
    recall = {t: found[t] / gold[t] for t in gold}
    rows = [f"| {t} | {gold[t]} | {recall[t]:.3f} | {hit[t] / pred[t] if pred[t] else 0:.3f} |" for t in sorted(gold)]
    table = "\n".join(
        [f"seed {seed}: {len(docs)} docs, {chars} chars, {1e6 * secs / chars:.0f} ms per 1000 chars",
         "| type | gold | recall | precision |", "| --- | --- | --- | --- |", *rows]
    )
    return recall, table


def test_pii(capsys):
    recall, table = evaluate(HELDOUT)
    with capsys.disabled():
        print("\n" + table)
    for t, floor in MIN_RECALL.items():
        assert recall[t] >= floor, f"{t} recall {recall[t]:.3f} < {floor}"


if __name__ == "__main__":
    print(evaluate(*map(int, sys.argv[1:]))[1])  # seed [n]
