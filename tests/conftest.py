"""Integration fixtures: a throw-away database plus the simulated ERP served from a background thread.

Requires a reachable Postgres (see README). Connection comes from the standard PG* env vars;
the tests create and use their own database, `erp_pipeline_test`.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = "erp_pipeline_test"
PORT = 8082


@pytest.fixture(scope="session")
def manifest() -> dict:
    from erp_sim import seed

    if not (ROOT / "erp_sim" / "data" / "defect_manifest.json").exists():
        seed.write(ROOT / "erp_sim" / "data")
    return json.loads((ROOT / "erp_sim" / "data" / "defect_manifest.json").read_text())


@pytest.fixture(scope="session")
def pipeline_run():
    os.environ["PGDATABASE"] = "postgres"
    from pipeline import db

    try:
        db.execute(f"drop database if exists {TEST_DB}")
        db.execute(f"create database {TEST_DB}")
    except Exception as exc:  # no Postgres available
        pytest.skip(f"Postgres not reachable: {exc}")
    os.environ["PGDATABASE"] = TEST_DB

    from erp_sim.server import create_app
    from pipeline import cli

    server = make_server("127.0.0.1", PORT, create_app(flaky=0.1), threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        rc = cli.run(f"http://127.0.0.1:{PORT}", "demo-token", 200)
        assert rc == 0, "pipeline run should publish"
        load_id = db.scalar("select load_id from raw.load_runs where status='published' order by started_at desc limit 1")
        yield {"load_id": load_id}
    finally:
        server.shutdown()
