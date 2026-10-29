from __future__ import annotations

import json

from pipeline import db, dq, lineage
from pipeline.ingest import sha256_of


def ids(sql: str) -> set[str]:
    return {r["x"] for r in db.query(sql)}


def quarantined(load_id, entity, reason) -> set[str]:
    return ids(f"select distinct source_id as x from quarantine.records where load_id={db.lit(load_id)} "
               f"and entity={db.lit(entity)} and reason={db.lit(reason)}")


# ------------------------------------------------------------------ ingestion
def test_landed_everything_the_api_reported(pipeline_run):
    run = db.query(f"select expected_counts, received_counts from raw.load_runs where load_id={db.lit(pipeline_run['load_id'])}")[0]
    expected, received = json.loads(run["expected_counts"]), json.loads(run["received_counts"])
    assert expected == received and all(v > 0 for v in received.values())
    for entity, n in expected.items():
        got = int(db.scalar(f"select count(*) from raw.records where load_id={db.lit(pipeline_run['load_id'])} and entity={db.lit(entity)}"))
        assert got == n


def test_every_raw_record_knows_where_it_came_from(pipeline_run):
    bad = db.scalar("select count(*) from raw.records where source_url !~ '^http.*/api/' or page < 1 or position < 0")
    assert int(bad) == 0


def test_payload_hashes_verify_and_detect_tampering(pipeline_run):
    lid = pipeline_run["load_id"]
    assert lineage.verify_hashes(lid) == 0
    rid = db.scalar(f"select raw_id from raw.records where load_id={db.lit(lid)} and entity='invoices' limit 1")
    original = db.scalar(f"select payload::text from raw.records where raw_id={rid}")
    try:
        db.execute(f"update raw.records set payload = jsonb_set(payload, '{{total}}', '1'::jsonb) where raw_id={rid}")
        assert lineage.verify_hashes(lid) == 1
    finally:
        db.execute(f"update raw.records set payload = {db.lit(original)}::jsonb where raw_id={rid}")
    assert lineage.verify_hashes(lid) == 0
    assert sha256_of(json.loads(original)) == db.scalar(f"select payload_sha256 from raw.records where raw_id={rid}")


# ------------------------------------------------- injected defects are caught
def test_every_injected_invoice_defect_is_quarantined(pipeline_run, manifest):
    lid = pipeline_run["load_id"]
    assert quarantined(lid, "invoices", "null_customer") == set(manifest["invoice_null_customer"])
    assert quarantined(lid, "invoices", "orphan_customer") == set(manifest["invoice_orphan_customer"])
    assert quarantined(lid, "invoices", "negative_total") == set(manifest["invoice_negative_total"])
    assert quarantined(lid, "invoices", "future_dated") == set(manifest["invoice_future_dated"])
    assert quarantined(lid, "invoices", "duplicate_source_id") == set(manifest["invoice_duplicate_id"])


def test_orphan_ledger_entries_are_distinguished_from_quarantined_parents(pipeline_run, manifest):
    lid = pipeline_run["load_id"]
    assert quarantined(lid, "ledger_entries", "orphan_invoice") == set(manifest["ledger_orphan_invoice"])
    parent = quarantined(lid, "ledger_entries", "parent_quarantined")
    assert parent and not parent & set(manifest["ledger_orphan_invoice"])


def test_currency_casing_defects_are_normalised_not_dropped(pipeline_run, manifest):
    assert int(db.scalar("select count(*) from core.invoices where currency <> 'USD'")) == 0
    fixed = {r["x"] for r in db.query(
        "select invoice_id as x from core.invoices where invoice_id = any(" +
        "array[" + ",".join(db.lit(i) for i in manifest["invoice_currency_casing"]) + "])")}
    lost = set(manifest["invoice_currency_casing"]) - fixed
    all_q = ids(f"select distinct source_id as x from quarantine.records where load_id={db.lit(pipeline_run['load_id'])} and entity='invoices'")
    assert lost <= all_q, "a casing-defect invoice may only be missing if it was quarantined for another reason"


def test_reconciliation_breaks_match_injected_defects(pipeline_run, manifest):
    lid = pipeline_run["load_id"]
    in_core = ids("select invoice_id as x from core.invoices")
    for break_type, key in [("header_vs_lines", "invoice_total_mismatch"), ("ledger_unbalanced", "ledger_unbalanced")]:
        found = ids(f"select invoice_id as x from dq.recon_breaks where load_id={db.lit(lid)} and break_type={db.lit(break_type)}")
        assert found == set(manifest[key]) & in_core, break_type
    assert len(in_core & set(manifest["invoice_total_mismatch"])) > 0


def test_core_contains_no_invalid_rows(pipeline_run):
    checks = {
        "null customer": "select count(*) from core.invoices where customer_id is null",
        "negative total": "select count(*) from core.invoices where total < 0",
        "future dated": "select count(*) from core.invoices i, raw.load_runs r where r.load_id = i.load_id and i.issued_on > r.erp_as_of",
        "orphan line": "select count(*) from core.invoice_lines l left join core.invoices i using (invoice_id) where i.invoice_id is null",
        "orphan ledger": "select count(*) from core.ledger_entries g left join core.invoices i using (invoice_id) where i.invoice_id is null",
    }
    for name, sql in checks.items():
        assert int(db.scalar(sql)) == 0, name


def test_nothing_is_lost_every_raw_record_is_in_core_or_quarantine(pipeline_run):
    lid = db.lit(pipeline_run["load_id"])
    for entity, table in [("customers", "core.customers"), ("invoices", "core.invoices"),
                          ("invoice_lines", "core.invoice_lines"), ("ledger_entries", "core.ledger_entries")]:
        raw = int(db.scalar(f"select count(*) from raw.records where load_id={lid} and entity={db.lit(entity)}"))
        core = int(db.scalar(f"select count(*) from {table}"))
        q = int(db.scalar(f"select count(distinct raw_id) from quarantine.records where load_id={lid} and entity={db.lit(entity)}"))
        assert core + q == raw, entity


# -------------------------------------------------------------------- lineage
def test_every_monthly_revenue_figure_ties_out_to_source_records(pipeline_run):
    months = [r["m"] for r in db.query("select to_char(month,'YYYY-MM') as m from mart.monthly_revenue")]
    assert len(months) == 18
    for m in months:
        tr = lineage.trace_month(m)
        assert tr["ties_out"], m
        assert all(s["source_url"].startswith("http") for s in tr["sources"])


def test_trusted_plus_unreconciled_equals_core_total(pipeline_run):
    mart = db.query("select sum(trusted_revenue) t, sum(unreconciled_revenue) u from mart.monthly_revenue")[0]
    core = db.scalar("select sum(total) from core.invoices")
    assert float(mart["t"]) + float(mart["u"]) == float(core)


def test_trace_invoice_returns_full_chain(pipeline_run, manifest):
    iid = manifest["invoice_total_mismatch"][0]
    tr = lineage.trace_invoice(iid)
    if tr["in_core"]:
        assert tr["invoice_sources"] and tr["line_sources"] and tr["ledger_sources"]
        assert {b["break_type"] for b in tr["recon_breaks"]} >= {"header_vs_lines"}


# ------------------------------------------------------------ DQ circuit breaker
def test_dq_passes_critical_checks_on_clean_run(pipeline_run):
    results = dq.run_checks(pipeline_run["load_id"])
    assert dq.critical_failures(results) == []


def test_dq_blocks_when_source_is_incomplete(pipeline_run):
    lid = pipeline_run["load_id"]
    original = db.scalar(f"select expected_counts::text from raw.load_runs where load_id={db.lit(lid)}")
    try:
        bumped = json.loads(original)
        bumped["invoices"] += 5
        db.execute(f"update raw.load_runs set expected_counts={db.lit(json.dumps(bumped))}::jsonb where load_id={db.lit(lid)}")
        failures = dq.critical_failures(dq.run_checks(lid))
        assert [f.name for f in failures] == ["completeness"] and failures[0].entity == "invoices"
    finally:
        db.execute(f"update raw.load_runs set expected_counts={db.lit(original)}::jsonb where load_id={db.lit(lid)}")


def test_transform_is_idempotent(pipeline_run):
    def fingerprint():
        return db.scalar(
            "select md5(string_agg(invoice_id||total::text||raw_id::text, ',' order by invoice_id)) from core.invoices")
    before = fingerprint()
    from pipeline.cli import SQL
    db.run_file(SQL / "010_transform.sql", {"load_id": pipeline_run["load_id"]})
    db.run_file(SQL / "020_mart.sql", {"load_id": pipeline_run["load_id"]})
    assert fingerprint() == before
