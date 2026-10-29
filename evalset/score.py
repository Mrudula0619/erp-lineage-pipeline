"""Score any system's answers against the evaluation set.

Answers file: JSON lines, one per question: {"id": "q001", "answer": "481550.00"}
Missing or null answers count as wrong.

    python -m evalset.score --answers my_answers.jsonl [--split test]
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

EVAL = Path(__file__).parent / "eval_set.jsonl"


def load_jsonl(path: Path | str) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def parse_number(x) -> float | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = re.sub(r"[,$£€\s]", "", str(x))
    try:
        return float(s)
    except ValueError:
        return None


def is_correct(q: dict, answer) -> bool:
    if answer is None:
        return False
    t = q["answer_type"]
    if t == "string":
        return str(answer).strip().casefold() == str(q["expected"]).strip().casefold()
    got = parse_number(answer)
    if got is None:
        return False
    if t == "integer":
        return got == q["expected"]
    return abs(got - q["expected"]) <= max(q.get("tolerance", 0.01), 1e-9)


def score(eval_rows: list[dict], answers: dict[str, object], split: str | None = None) -> dict:
    rows = [q for q in eval_rows if split in (None, "all", q["split"])]
    per_cat: dict[str, list[bool]] = defaultdict(list)
    failures = []
    for q in rows:
        ok = is_correct(q, answers.get(q["id"]))
        per_cat[q["category"]].append(ok)
        if not ok:
            failures.append({"id": q["id"], "category": q["category"], "question": q["question"],
                             "expected": q["expected"], "got": answers.get(q["id"])})
    total = sum(len(v) for v in per_cat.values())
    correct = sum(sum(v) for v in per_cat.values())
    return {
        "n": total, "correct": correct, "accuracy": correct / total if total else 0.0,
        "by_category": {c: {"n": len(v), "correct": sum(v), "accuracy": sum(v) / len(v)} for c, v in per_cat.items()},
        "failures": failures,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers", required=True)
    ap.add_argument("--eval", default=str(EVAL))
    ap.add_argument("--split", default="all", choices=["all", "dev", "test"])
    ap.add_argument("--show-failures", type=int, default=0)
    a = ap.parse_args()
    answers = {r["id"]: r.get("answer") for r in load_jsonl(a.answers)}
    res = score(load_jsonl(a.eval), answers, a.split)
    print(f"accuracy {res['correct']}/{res['n']} = {res['accuracy']:.1%}  (split={a.split})")
    for c, v in sorted(res["by_category"].items()):
        print(f"  {c:32s} {v['correct']:>3}/{v['n']:<3} {v['accuracy']:.0%}")
    for f in res["failures"][: a.show_failures]:
        print(f"  FAIL {f['id']} expected={f['expected']} got={f['got']}  | {f['question']}")


if __name__ == "__main__":
    main()
