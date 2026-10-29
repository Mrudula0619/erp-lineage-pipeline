"""Command line entry point.

    python -m pipeline.cli init-db
    python -m pipeline.cli run            # extract -> raw -> core -> dq gate -> mart
    python -m pipeline.cli dq-report
    python -m pipeline.cli trace --month 2026-03
    python -m pipeline.cli trace --invoice INV-000123
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import db, dq, ingest, lineage

ROOT = Path(__file__).resolve().parent.parent
SQL = ROOT / "sql"
DEFAULT_URL = "http://127.0.0.1:8081"
DEFAULT_TOKEN = "demo-token"


def init_db() -> None:
    db.run_file(SQL / "001_schema.sql")


def run(base_url: str, token: str, page_size: int) -> int:
    init_db()
    landed = ingest.ingest(base_url, token, page_size)
    load_id = landed["load_id"]
    print(f"load {load_id}")
    print(f"  landed   {landed['received']}  (API said {landed['expected']}, {landed['retries']} retried requests)")

    db.run_file(SQL / "010_transform.sql", {"load_id": load_id})
    results = dq.run_checks(load_id)
    dq.persist(load_id, results)
    failures = dq.critical_failures(results)
    if failures:
        db.execute(f"update raw.load_runs set status='blocked' where load_id={db.lit(load_id)}")
        print("\nPUBLISH BLOCKED. Critical checks failed:")
        for f in failures:
            print(f"  - {f.name} [{f.entity}] {f.violations} violation(s) {f.detail}")
        return 1

    db.run_file(SQL / "020_mart.sql", {"load_id": load_id})
    n_q = db.scalar(f"select count(*) from quarantine.records where load_id={db.lit(load_id)}")
    n_b = db.scalar(f"select count(distinct invoice_id) from dq.recon_breaks where load_id={db.lit(load_id)}")
    print(f"  core     built; {n_q} records quarantined, {n_b} invoices flagged unreconciled")
    print("  mart     published")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    r = sub.add_parser("run")
    r.add_argument("--base-url", default=DEFAULT_URL)
    r.add_argument("--token", default=DEFAULT_TOKEN)
    r.add_argument("--page-size", type=int, default=200)
    d = sub.add_parser("dq-report")
    d.add_argument("--load-id")
    t = sub.add_parser("trace")
    g = t.add_mutually_exclusive_group(required=True)
    g.add_argument("--month", help="YYYY-MM: trace the trusted revenue figure for that month")
    g.add_argument("--invoice", help="trace one invoice end to end")
    t.add_argument("--csv", help="write all contributing source records to this file")
    args = ap.parse_args(argv)

    if args.cmd == "init-db":
        init_db()
        return 0
    if args.cmd == "run":
        return run(args.base_url, args.token, args.page_size)
    if args.cmd == "dq-report":
        lid = args.load_id or db.scalar("select load_id from raw.load_runs order by started_at desc limit 1")
        rows = db.query(
            "select status, severity, check_name, coalesce(entity,'-') entity, violations, detail "
            f"from dq.check_results where load_id={db.lit(lid)} order by result_id")
        print(f"load {lid}")
        print(f"{'STATUS':6} {'SEV':8} {'CHECK':38} {'ENTITY':15} {'N':>5}  DETAIL")
        for x in rows:
            print(f"{x['status'].upper():6} {x['severity']:8} {x['check_name']:38} {x['entity']:15} {x['violations']:>5}  {x['detail']}")
        return 0
    if args.cmd == "trace":
        if args.month:
            tr = lineage.trace_month(args.month)
            print(f"Trusted revenue {tr['month']}: {tr['reported_revenue']:,.2f} from {tr['invoice_count']} invoices (load {tr['load_id']})")
            print(f"Sum of traced source records:   {tr['traced_revenue']:,.2f}   ties out: {tr['ties_out']}")
            print("\nfirst 5 contributing source records:")
            for s in tr["sources"][:5]:
                print(f"  {s['invoice_id']}  {s['total']:>10}  raw_id={s['raw_id']}  {s['source_url']}  position={s['position']}  sha256={s['payload_sha256'][:12]}")
            if args.csv:
                lineage.write_trace_csv(tr, args.csv)
                print(f"\nfull list written to {args.csv}")
            return 0 if tr["ties_out"] else 2
        tr = lineage.trace_invoice(args.invoice)
        print(f"{tr['invoice_id']}  in core: {tr['in_core']}")
        for label in ("invoice_sources", "line_sources", "ledger_sources"):
            print(f"\n{label} ({len(tr[label])})")
            for s in tr[label]:
                print(f"  raw_id={s['raw_id']} {s['source_id']}  {s['source_url']}  position={s['position']}  sha256={s['payload_sha256'][:12]}")
        print("\nreconciliation breaks:", tr["recon_breaks"] or "none")
        print("quarantine:", tr["quarantine"] or "none")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
