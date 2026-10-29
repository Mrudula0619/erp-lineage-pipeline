from __future__ import annotations

from evalset import baselines, build_eval
from evalset.score import load_jsonl, score


def test_eval_set_is_reproducible_from_the_database(pipeline_run):
    a = build_eval.build()
    b = build_eval.build()
    assert [(q["id"], q["question"], q["expected"]) for q in a] == [(q["id"], q["question"], q["expected"]) for q in b]
    assert len(a) >= 80 and len({q["id"] for q in a}) == len(a)
    assert {q["split"] for q in a} == {"dev", "test"}


def test_eval_covers_quality_and_lineage_not_just_aggregates(pipeline_run):
    cats = {q["category"] for q in build_eval.build()}
    assert {"dq_quarantine_count", "dq_unreconciled", "lineage_source_page", "unreconciled_revenue_by_month"} <= cats


def test_oracle_scores_100_and_empty_scores_0(pipeline_run, tmp_path, monkeypatch):
    qs = build_eval.build()
    p = tmp_path / "eval.jsonl"
    import json
    p.write_text("\n".join(json.dumps(q) for q in qs))
    monkeypatch.setattr(baselines, "EVAL", p)
    rows = load_jsonl(p)
    assert score(rows, {r["id"]: r["answer"] for r in baselines.answer_all("oracle")})["accuracy"] == 1.0
    assert score(rows, {r["id"]: r["answer"] for r in baselines.answer_all("empty")})["accuracy"] == 0.0


def test_naive_raw_queries_are_much_worse_than_the_clean_layer(pipeline_run, tmp_path, monkeypatch):
    qs = build_eval.build()
    p = tmp_path / "eval.jsonl"
    import json
    p.write_text("\n".join(json.dumps(q) for q in qs))
    monkeypatch.setattr(baselines, "EVAL", p)
    res = score(load_jsonl(p), {r["id"]: r["answer"] for r in baselines.answer_all("naive_raw")})
    assert res["accuracy"] < 0.5
    assert res["by_category"]["revenue_by_month"]["accuracy"] < 0.2
