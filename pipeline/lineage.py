"""Trace any reported figure back to the exact API response that produced it."""
from __future__ import annotations

import csv
import json
from decimal import Decimal

from . import db
from .ingest import sha256_of


def latest_published_load() -> str:
    lid = db.scalar("select load_id from raw.load_runs where status='published' order by started_at desc limit 1")
    if not lid:
        raise RuntimeError("no published load found; run the pipeline first")
    return lid


def verify_hashes(load_id: str) -> int:
    """Re-hash every stored payload. Returns the number whose hash no longer matches."""
    rows = db.query(
        f"select raw_id, payload::text as payload, payload_sha256 from raw.records where load_id = {db.lit(load_id)}"
    )
    return sum(1 for r in rows if sha256_of(json.loads(r["payload"])) != r["payload_sha256"])


def trace_month(month: str, load_id: str | None = None) -> dict:
    """month = 'YYYY-MM'. Returns the mart figure plus the source records that add up to it."""
    load_id = load_id or latest_published_load()
    first = f"{month}-01"
    figure = db.query(
        f"select trusted_revenue, trusted_invoices from mart.monthly_revenue where month = {db.lit(first)}::date")
    if not figure:
        raise ValueError(f"no revenue for {month}")
    reported = Decimal(figure[0]["trusted_revenue"])
    rows = db.query(
        "select f.invoice_id, f.customer_id, f.total, f.raw_id, r.source_url, r.page, r.position, r.extracted_at, "
        "r.payload_sha256, r.load_id "
        "from mart.invoice_facts f join raw.records r on r.raw_id = f.raw_id "
        f"where f.month = {db.lit(first)}::date and f.is_trusted order by f.invoice_id")
    traced = sum((Decimal(r["total"]) for r in rows), Decimal("0"))
    return {
        "month": month,
        "load_id": load_id,
        "reported_revenue": reported,
        "traced_revenue": traced,
        "ties_out": reported == traced and int(figure[0]["trusted_invoices"]) == len(rows),
        "invoice_count": len(rows),
        "sources": rows,
    }


def trace_invoice(invoice_id: str) -> dict:
    """Full chain for one invoice: raw records, quarantined duplicates, reconciliation, ledger."""
    iid = db.lit(invoice_id)

    def src(entity: str, where: str) -> list[dict]:
        return db.query(
            "select r.raw_id, r.source_id, r.source_url, r.page, r.position, r.payload_sha256, r.extracted_at "
            f"from raw.records r where r.entity = {db.lit(entity)} and {where} order by r.page, r.position")

    latest = latest_published_load()
    return {
        "invoice_id": invoice_id,
        "in_core": bool(db.query(f"select 1 from core.invoices where invoice_id = {iid}")),
        "invoice_sources": src("invoices", f"r.source_id = {iid} and r.load_id = {db.lit(latest)}"),
        "line_sources": src("invoice_lines", f"r.payload->>'invoice_id' = {iid} and r.load_id = {db.lit(latest)}"),
        "ledger_sources": src("ledger_entries", f"r.payload->>'invoice_id' = {iid} and r.load_id = {db.lit(latest)}"),
        "recon_breaks": db.query(
            f"select break_type, header_total, lines_total, ledger_revenue, diff from dq.recon_breaks where invoice_id = {iid} and load_id = {db.lit(latest)}"),
        "quarantine": db.query(
            f"select entity, reason, detail from quarantine.records where source_id = {iid} and load_id = {db.lit(latest)}"),
    }


def write_trace_csv(trace: dict, path: str) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(trace["sources"][0].keys()))
        w.writeheader()
        w.writerows(trace["sources"])
