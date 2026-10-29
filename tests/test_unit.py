"""Tests that need no database."""
from __future__ import annotations

from erp_sim import seed
from evalset.score import is_correct, parse_number, score
from pipeline.dq import Result, critical_failures
from pipeline.ingest import canonical_json, sha256_of


def test_seed_is_deterministic():
    a, ma = seed.build()
    b, mb = seed.build()
    assert a == b and ma == mb


def test_seed_injects_the_documented_number_of_defects():
    _, m = seed.build()
    assert {k: len(v) for k, v in m.items()} == {
        "invoice_null_customer": 12, "invoice_orphan_customer": 9, "invoice_total_mismatch": 15,
        "invoice_negative_total": 6, "invoice_currency_casing": 20, "invoice_future_dated": 5,
        "ledger_unbalanced": 10, "invoice_duplicate_id": 8, "ledger_orphan_invoice": 7}


def test_hash_is_independent_of_key_order():
    assert sha256_of({"a": 1, "b": [1, 2]}) == sha256_of({"b": [1, 2], "a": 1})
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert sha256_of({"a": 1}) != sha256_of({"a": 2})


def test_critical_failure_detection():
    ok = Result("x", "raw", None, "critical", 0)
    bad = Result("y", "raw", None, "critical", 2)
    warn = Result("z", "core", None, "warn", 9)
    assert critical_failures([ok, bad, warn]) == [bad]


def test_scorer_number_tolerance_and_formats():
    q = {"answer_type": "number", "expected": 1234.5, "tolerance": 0.01}
    assert is_correct(q, "$1,234.50") and is_correct(q, 1234.5) and not is_correct(q, 1234.6)
    assert not is_correct(q, None) and not is_correct(q, "n/a")
    assert parse_number("£2,000") == 2000.0


def test_scorer_integer_is_exact_and_string_is_normalised():
    assert is_correct({"answer_type": "integer", "expected": 12}, "12")
    assert not is_correct({"answer_type": "integer", "expected": 12}, 12.5)
    assert is_correct({"answer_type": "string", "expected": "Customer 007 Ltd"}, "  customer 007 ltd ")


def test_scorer_aggregates_by_category_and_split():
    rows = [
        {"id": "a", "split": "dev", "category": "c1", "question": "?", "answer_type": "integer", "expected": 1},
        {"id": "b", "split": "test", "category": "c2", "question": "?", "answer_type": "integer", "expected": 2},
    ]
    r = score(rows, {"a": 1, "b": 9})
    assert r["correct"] == 1 and r["n"] == 2 and r["by_category"]["c2"]["accuracy"] == 0.0
    assert score(rows, {"a": 1, "b": 9}, "dev")["accuracy"] == 1.0
