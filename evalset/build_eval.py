"""Build a labelled evaluation set from the published mart.

Every question carries the SQL that produced its answer, so a label can always be re-derived
and audited. Questions deliberately include ones that only a system that understands the data
quality rules (trusted vs unreconciled, quarantine) and the lineage metadata can answer.

    python -m evalset.build_eval            # writes evalset/eval_set.jsonl
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipeline import db
from pipeline.lineage import latest_published_load

OUT = Path(__file__).parent / "eval_set.jsonl"

REASON_TEXT = {
    "null_customer": "the invoice had no customer",
    "orphan_customer": "the invoice referenced a customer that does not exist",
    "negative_total": "the invoice total was negative",
    "future_dated": "the invoice was dated after the ERP's as-of date",
}


def split_of(qid: str) -> str:
    return "test" if int(hashlib.sha256(qid.encode()).hexdigest(), 16) % 2 else "dev"


def month_label(first_of_month: str) -> str:
    return db.scalar(f"select to_char({db.lit(first_of_month)}::date, 'FMMonth YYYY')")


def quarter_label(first_of_quarter: str) -> str:
    return db.scalar(
        f"select 'Q' || extract(quarter from {db.lit(first_of_quarter)}::date)::int || ' ' || extract(year from {db.lit(first_of_quarter)}::date)::int")


def build() -> list[dict]:
    load_id = latest_published_load()
    qs: list[dict] = []

    def add(category, question, answer_type, sql, tolerance=0.0, note=""):
        raw = db.scalar(sql)
        if raw is None:
            return
        expected = float(raw) if answer_type == "number" else int(raw) if answer_type == "integer" else str(raw)
        qid = f"q{len(qs) + 1:03d}"
        qs.append({
            "id": qid, "split": split_of(qid), "category": category, "question": question,
            "answer_type": answer_type, "expected": expected, "tolerance": tolerance,
            "ground_truth_sql": sql, "load_id": load_id, "note": note,
        })

    months = [r["month"] for r in db.query("select month::text as month from mart.monthly_revenue order by month")]
    quarters = [r["q"] for r in db.query(
        "select distinct date_trunc('quarter', month)::date::text as q from mart.monthly_revenue order by 1")]
    regions = [r["region"] for r in db.query("select distinct region from core.customers order by 1")]

    # A. trusted revenue by month
    for m in months:
        add("revenue_by_month", f"What was the total trusted revenue for {month_label(m)}?", "number",
            f"select trusted_revenue from mart.monthly_revenue where month = {db.lit(m)}::date", 0.01)

    # B. trusted invoice counts
    for m in months[::2]:
        add("invoice_count_by_month", f"How many trusted invoices were issued in {month_label(m)}?", "integer",
            f"select trusted_invoices from mart.monthly_revenue where month = {db.lit(m)}::date")

    # C. region x quarter revenue
    for q in quarters:
        for reg in regions:
            add("revenue_by_region_quarter",
                f"What was the trusted revenue in {reg} during {quarter_label(q)}?", "number",
                "select coalesce(sum(total),0) from mart.invoice_facts where is_trusted "
                f"and region = {db.lit(reg)} and date_trunc('quarter', issued_on) = {db.lit(q)}::date", 0.01)

    # D. top customer per quarter (skip ties so there is one right answer)
    for q in quarters:
        top = db.query(
            "select customer_name, sum(total) as s from mart.invoice_facts where is_trusted "
            f"and date_trunc('quarter', issued_on) = {db.lit(q)}::date group by 1 order by 2 desc limit 2")
        if len(top) == 2 and top[0]["s"] != top[1]["s"]:
            add("top_customer_by_quarter", f"Which customer had the highest trusted revenue in {quarter_label(q)}?", "string",
                "select customer_name from mart.invoice_facts where is_trusted "
                f"and date_trunc('quarter', issued_on) = {db.lit(q)}::date group by 1 order by sum(total) desc limit 1")

    # E. open balance for selected customers
    for r in db.query(
        "select customer_name from mart.invoice_facts where is_trusted and status = 'open' "
        "group by 1 order by sum(total) desc limit 12"):
        add("open_balance_by_customer", f"What is the total of trusted, still-open invoices for {r['customer_name']}?", "number",
            "select coalesce(sum(total),0) from mart.invoice_facts where is_trusted and status = 'open' "
            f"and customer_name = {db.lit(r['customer_name'])}", 0.01)

    # F. data-quality awareness: how many records were excluded and why
    for reason, text in REASON_TEXT.items():
        add("dq_quarantine_count", f"How many distinct invoice ids were excluded from reporting because {text}?", "integer",
            f"select count(distinct source_id) from quarantine.records where load_id = {db.lit(load_id)} "
            f"and entity = 'invoices' and reason = {db.lit(reason)}")
    add("dq_quarantine_count", "How many distinct invoice ids were returned more than once by the API (extra copies removed from reporting)?", "integer",
        f"select count(distinct source_id) from quarantine.records where load_id = {db.lit(load_id)} "
        "and entity = 'invoices' and reason = 'duplicate_source_id'")
    add("dq_unreconciled", "How many invoices in core have a header total that disagrees with the sum of their lines?", "integer",
        f"select count(*) from dq.recon_breaks where load_id = {db.lit(load_id)} and break_type = 'header_vs_lines'")
    add("dq_unreconciled", "How many invoices have unbalanced ledger entries (debits not equal to credits)?", "integer",
        f"select count(*) from dq.recon_breaks where load_id = {db.lit(load_id)} and break_type = 'ledger_unbalanced'")
    add("dq_unreconciled", "Which invoice has the largest absolute difference between its header total and the sum of its lines?", "string",
        f"select invoice_id from dq.recon_breaks where load_id = {db.lit(load_id)} and break_type = 'header_vs_lines' "
        "order by abs(diff) desc, invoice_id limit 1",
        note="ties broken by lowest invoice id")
    for m in months:
        n = db.scalar(f"select unreconciled_invoices from mart.monthly_revenue where month = {db.lit(m)}::date")
        if n and int(n) > 0 and sum(1 for q in qs if q["category"] == "unreconciled_revenue_by_month") < 5:
            add("unreconciled_revenue_by_month",
                f"How much revenue in {month_label(m)} is excluded from the trusted figure because it does not reconcile?", "number",
                f"select unreconciled_revenue from mart.monthly_revenue where month = {db.lit(m)}::date", 0.01)

    # G. lineage: which API page served a record (needs the lineage metadata, not just the numbers)
    for r in db.query("select invoice_id from core.invoices order by md5(invoice_id) limit 8"):
        add("lineage_source_page", f"On which page of the invoices API response was {r['invoice_id']} returned?", "integer",
            f"select page from raw.records where load_id = {db.lit(load_id)} and entity = 'invoices' "
            f"and source_id = {db.lit(r['invoice_id'])} order by page, position limit 1")
    return qs


def main() -> None:
    qs = build()
    OUT.write_text("\n".join(json.dumps(q) for q in qs) + "\n")
    by_cat: dict[str, int] = {}
    for q in qs:
        by_cat[q["category"]] = by_cat.get(q["category"], 0) + 1
    print(f"wrote {len(qs)} questions to {OUT}")
    for k, v in by_cat.items():
        print(f"  {k:32s} {v}")
    print(f"  split: dev={sum(q['split']=='dev' for q in qs)} test={sum(q['split']=='test' for q in qs)}")


if __name__ == "__main__":
    main()
