"""Reference answerers, used to sanity-check the eval set and to show why the data layer matters.

  oracle     answers every question from its stored label. Must score 100% (validates the scorer).
  naive_raw  what you get when someone queries the raw extract directly: no de-duplication, no
             quarantine, no reconciliation, no lineage. It cannot answer data-quality or lineage
             questions at all.
  empty      answers nothing. Must score 0%.

    python -m evalset.baselines --mode naive_raw --out /tmp/naive.jsonl
"""
from __future__ import annotations

import argparse
import json
import re

from pipeline import db
from pipeline.lineage import latest_published_load

from .score import load_jsonl, EVAL

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}


def naive_answer(q: dict, load_id: str):
    text, cat = q["question"], q["category"]
    L = db.lit(load_id)
    inv = f"raw.records r where r.load_id = {L} and r.entity = 'invoices'"

    if cat == "revenue_by_month":
        m = re.search(r"for (\w+) (\d{4})", text)
        mo, yr = MONTHS[m.group(1)], int(m.group(2))
        return db.scalar(
            f"select coalesce(sum((payload->>'total')::numeric),0) from {inv} "
            f"and extract(month from (payload->>'issued_on')::date) = {mo} and extract(year from (payload->>'issued_on')::date) = {yr}")
    if cat == "invoice_count_by_month":
        m = re.search(r"in (\w+) (\d{4})", text)
        mo, yr = MONTHS[m.group(1)], int(m.group(2))
        return db.scalar(
            f"select count(*) from {inv} and extract(month from (payload->>'issued_on')::date) = {mo} "
            f"and extract(year from (payload->>'issued_on')::date) = {yr}")
    if cat == "revenue_by_region_quarter":
        m = re.search(r"in (.+) during Q(\d) (\d{4})", text)
        reg, qn, yr = m.group(1), int(m.group(2)), int(m.group(3))
        return db.scalar(
            f"select coalesce(sum((r.payload->>'total')::numeric),0) from {inv} "
            f"and extract(quarter from (payload->>'issued_on')::date) = {qn} and extract(year from (payload->>'issued_on')::date) = {yr} "
            f"and payload->>'customer_id' in (select source_id from raw.records c where c.load_id = {L} and c.entity='customers' "
            f"and c.payload->>'region' = {db.lit(reg)})")
    if cat == "top_customer_by_quarter":
        m = re.search(r"in Q(\d) (\d{4})", text)
        qn, yr = int(m.group(1)), int(m.group(2))
        return db.scalar(
            "select c.payload->>'name' from raw.records r join raw.records c on c.load_id = r.load_id and c.entity='customers' "
            f"and c.source_id = r.payload->>'customer_id' where r.load_id = {L} and r.entity = 'invoices' "
            f"and extract(quarter from (r.payload->>'issued_on')::date) = {qn} and extract(year from (r.payload->>'issued_on')::date) = {yr} "
            "group by 1 order by sum((r.payload->>'total')::numeric) desc limit 1")
    if cat == "open_balance_by_customer":
        name = re.search(r"invoices for (.+)\?$", text).group(1)
        return db.scalar(
            "select coalesce(sum((r.payload->>'total')::numeric),0) from raw.records r join raw.records c "
            f"on c.load_id = r.load_id and c.entity='customers' and c.source_id = r.payload->>'customer_id' "
            f"where r.load_id = {L} and r.entity='invoices' and r.payload->>'status' = 'open' and c.payload->>'name' = {db.lit(name)}")
    return None  # data-quality and lineage questions are out of reach of a raw query


def answer_all(mode: str) -> list[dict]:
    qs = load_jsonl(EVAL)
    lid = latest_published_load()
    out = []
    for q in qs:
        if mode == "oracle":
            a = q["expected"]
        elif mode == "naive_raw":
            a = naive_answer(q, lid)
        else:
            a = None
        out.append({"id": q["id"], "answer": a})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["oracle", "naive_raw", "empty"], required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = answer_all(a.mode)
    with open(a.out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"wrote {len(rows)} answers to {a.out}")


if __name__ == "__main__":
    main()
