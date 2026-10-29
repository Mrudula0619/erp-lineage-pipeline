"""Data quality checks. Results are persisted to dq.check_results for every load.

Severity model
  critical  a violation blocks publishing the mart (the circuit breaker)
  warn      a real problem in the source data, reported and tracked, does not block
  info      a transformation we applied (quarantine, normalisation), counted for transparency
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db
from .lineage import verify_hashes

QUARANTINE_RATE_LIMIT = 0.05   # >5% of an entity rejected means something is wrong upstream
VOLUME_SWING_LIMIT = 0.20      # >20% change in invoice count vs the previous load


@dataclass
class Result:
    name: str
    layer: str
    entity: str | None
    severity: str
    violations: int
    detail: str = ""

    @property
    def status(self) -> str:
        if self.severity == "info":
            return "info"
        return "pass" if self.violations == 0 else "fail"


def _count(sql: str) -> int:
    return int(db.scalar(sql) or 0)


def run_checks(load_id: str) -> list[Result]:
    L = db.lit(load_id)
    out: list[Result] = []

    # ---- raw layer: did we get everything, exactly as sent? (critical)
    run = db.query(f"select expected_counts, received_counts from raw.load_runs where load_id = {L}")[0]
    import json

    expected = json.loads(run["expected_counts"])
    for entity, exp in expected.items():
        got = _count(f"select count(*) from raw.records where load_id={L} and entity={db.lit(entity)}")
        out.append(Result("completeness", "raw", entity, "critical", abs(exp - got), f"API reported {exp}, landed {got}"))
    bad_hash = verify_hashes(load_id)
    out.append(Result("payload_hash_intact", "raw", None, "critical", bad_hash, "sha256 of stored payload equals hash taken at extraction"))

    # ---- core layer invariants (critical)
    out.append(Result("core_no_negative_totals", "core", "invoices", "critical",
                      _count("select count(*) from core.invoices where total < 0")))
    out.append(Result("core_currency_is_usd", "core", "invoices", "critical",
                      _count("select count(*) from core.invoices where currency <> 'USD'")))
    for ent, tbl in [("customers", "core.customers"), ("invoices", "core.invoices"),
                     ("invoice_lines", "core.invoice_lines"), ("ledger_entries", "core.ledger_entries")]:
        out.append(Result("core_row_traces_to_raw", "core", ent, "critical", _count(
            f"select count(*) from {tbl} t left join raw.records r on r.raw_id = t.raw_id "
            f"and r.load_id = t.load_id and r.entity = {db.lit(ent)} where r.raw_id is null")))

    # ---- quarantine rate guard (critical)
    for ent in expected:
        total = _count(f"select count(*) from raw.records where load_id={L} and entity={db.lit(ent)}")
        q = _count(f"select count(distinct raw_id) from quarantine.records where load_id={L} and entity={db.lit(ent)}")
        rate = q / total if total else 0
        out.append(Result("quarantine_rate_under_limit", "core", ent, "critical",
                          1 if rate > QUARANTINE_RATE_LIMIT else 0, f"{q}/{total} = {rate:.2%} (limit {QUARANTINE_RATE_LIMIT:.0%})"))

    # ---- volume swing vs previous load (warn)
    prev = db.query(
        "select load_id, (received_counts->>'invoices')::int as n from raw.load_runs "
        f"where load_id <> {L} and status in ('published') order by started_at desc limit 1")
    if prev:
        cur = _count(f"select count(*) from raw.records where load_id={L} and entity='invoices'")
        swing = abs(cur - int(prev[0]["n"])) / max(int(prev[0]["n"]), 1)
        out.append(Result("invoice_volume_vs_previous_load", "raw", "invoices", "warn",
                          1 if swing > VOLUME_SWING_LIMIT else 0, f"{cur} vs {prev[0]['n']} ({swing:.1%})"))

    # ---- source-data defects found and handled (info = quarantined / normalised)
    for r in db.query(
        f"select entity, reason, count(distinct source_id) as n from quarantine.records where load_id={L} group by 1,2 order by 1,2"):
        out.append(Result(f"quarantined:{r['reason']}", "core", r["entity"], "info", int(r["n"]), "rejected from core, kept in quarantine with raw_id"))
    out.append(Result("normalised:currency_casing", "core", "invoices", "info", _count(
        f"select count(distinct source_id) from raw.records where load_id={L} and entity='invoices' "
        "and payload->>'currency' <> upper(payload->>'currency')"), "upper-cased to ISO form"))

    # ---- reconciliation breaks (warn: valid records whose numbers disagree)
    for bt in ("header_vs_lines", "ledger_unbalanced", "header_vs_ledger"):
        out.append(Result(f"recon:{bt}", "core", "invoices", "warn", _count(
            f"select count(*) from dq.recon_breaks where load_id={L} and break_type={db.lit(bt)}"),
            "kept in core, excluded from trusted revenue"))
    out.append(Result("line_amount_equals_qty_x_price", "core", "invoice_lines", "warn", _count(
        "select count(*) from core.invoice_lines where abs(amount - quantity * unit_price) > 0.005")))
    out.append(Result("invoice_has_lines", "core", "invoices", "warn", _count(
        "select count(*) from core.invoices i where not exists (select 1 from core.invoice_lines l where l.invoice_id=i.invoice_id)")))
    out.append(Result("invoice_has_ledger_entries", "core", "invoices", "warn", _count(
        "select count(*) from core.invoices i where not exists (select 1 from core.ledger_entries g where g.invoice_id=i.invoice_id)")))
    return out


def persist(load_id: str, results: list[Result]) -> None:
    db.execute(f"delete from dq.check_results where load_id = {db.lit(load_id)}")
    values = ",".join(
        f"({db.lit(load_id)},{db.lit(r.name)},{db.lit(r.layer)},{db.lit(r.entity)},{db.lit(r.severity)},"
        f"{r.violations},{db.lit(r.status)},{db.lit(r.detail)})" for r in results)
    db.execute(
        "insert into dq.check_results (load_id, check_name, layer, entity, severity, violations, status, detail) "
        f"values {values}")


def critical_failures(results: list[Result]) -> list[Result]:
    return [r for r in results if r.severity == "critical" and r.status == "fail"]


def format_report(results: list[Result]) -> str:
    lines = [f"{'STATUS':6} {'SEV':8} {'CHECK':38} {'ENTITY':15} {'N':>5}  DETAIL"]
    for r in results:
        lines.append(f"{r.status.upper():6} {r.severity:8} {r.name:38} {(r.entity or '-'):15} {r.violations:>5}  {r.detail}")
    return "\n".join(lines)
