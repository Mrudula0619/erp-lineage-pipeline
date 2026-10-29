"""Extract from the ERP and land every record, untouched, in raw.records."""
from __future__ import annotations

import hashlib
import json
import uuid

from . import db
from .extract import ENTITIES, ERPClient, EntityStats


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def ingest(base_url: str, token: str, page_size: int = 200) -> dict:
    load_id = str(uuid.uuid4())
    db.execute(
        f"insert into raw.load_runs (load_id, source_base_url) values ({db.lit(load_id)}, {db.lit(base_url)})"
    )
    client = ERPClient(base_url, token, page_size=page_size)
    stats: dict[str, EntityStats] = {}
    try:
        for entity in ENTITIES:
            st = stats[entity] = EntityStats()
            rows = (
                (
                    load_id,
                    x.entity,
                    x.source_id,
                    x.source_url,
                    x.page,
                    x.position,
                    x.extracted_at.isoformat(),
                    canonical_json(x.record),
                    sha256_of(x.record),
                )
                for x in client.fetch(entity, st)
            )
            db.copy_rows(
                "raw.records",
                ["load_id", "entity", "source_id", "source_url", "page", "position", "extracted_at", "payload", "payload_sha256"],
                rows,
            )
    except Exception:
        db.execute(f"update raw.load_runs set status='failed', finished_at=now() where load_id={db.lit(load_id)}")
        raise

    expected = {e: s.expected_total for e, s in stats.items()}
    received = {e: s.received for e, s in stats.items()}
    as_of = next((s.as_of for s in stats.values() if s.as_of), None)
    db.execute(
        "update raw.load_runs set status='landed', finished_at=now(), "
        f"expected_counts={db.lit(json.dumps(expected))}::jsonb, "
        f"received_counts={db.lit(json.dumps(received))}::jsonb, "
        f"erp_as_of={db.lit(as_of)} "
        f"where load_id={db.lit(load_id)}"
    )
    return {"load_id": load_id, "expected": expected, "received": received, "retries": client.retries, "as_of": as_of}
