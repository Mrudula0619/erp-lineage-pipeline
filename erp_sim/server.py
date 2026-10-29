"""Simulated ERP REST API (Flask).

Mimics the behaviours that make real ERP extraction annoying:
  * page-number pagination
  * bearer-token auth
  * incremental extraction via `updated_since`
  * optional flakiness (HTTP 429/503) so the client's retry logic is exercised

Run:  python -m erp_sim.server --port 8081 [--flaky 0.1]
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from flask import Flask, jsonify, request

from . import seed

TOKEN = "demo-token"
ENTITIES = ("customers", "invoices", "invoice_lines", "ledger_entries")


def create_app(flaky: float = 0.0, flaky_seed: int = 7) -> Flask:
    app = Flask(__name__)
    data_file = Path(__file__).parent / "data" / "erp_data.json"
    if not data_file.exists():
        seed.write(data_file.parent)
    data = json.loads(data_file.read_text())
    rng = random.Random(flaky_seed)

    @app.before_request
    def _auth_and_chaos():
        if request.path == "/health":
            return None
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return jsonify(error="unauthorized"), 401
        if flaky and rng.random() < flaky:
            code = rng.choice([429, 503])
            return jsonify(error="transient"), code
        return None

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/api/<entity>")
    def list_entity(entity: str):
        if entity not in ENTITIES:
            return jsonify(error="unknown entity"), 404
        page = max(int(request.args.get("page", 1)), 1)
        size = min(max(int(request.args.get("page_size", 200)), 1), 500)
        rows = data[entity]
        start = (page - 1) * size
        chunk = rows[start : start + size]
        return jsonify(
            entity=entity,
            as_of=seed.AS_OF.isoformat(),
            page=page,
            page_size=size,
            total=len(rows),
            has_more=start + size < len(rows),
            results=chunk,
        )

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--flaky", type=float, default=0.0)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    create_app(flaky=args.flaky).run(host=args.host, port=args.port)
