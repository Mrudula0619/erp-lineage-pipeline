"""Paginated, retrying extraction from the ERP REST API."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import requests

ENTITIES = ("customers", "invoices", "invoice_lines", "ledger_entries")
RETRYABLE = {429, 500, 502, 503, 504}


@dataclass
class Extracted:
    entity: str
    source_id: str
    source_url: str
    page: int
    position: int
    extracted_at: datetime
    record: dict


@dataclass
class EntityStats:
    expected_total: int = 0
    received: int = 0
    retries: int = 0
    as_of: str | None = None


class ERPClient:
    def __init__(self, base_url: str, token: str, page_size: int = 200, max_retries: int = 6, backoff: float = 0.05):
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.max_retries = max_retries
        self.backoff = backoff
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.retries = 0

    def _get(self, url: str, params: dict) -> requests.Response:
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except (requests.ConnectionError, requests.Timeout):
                resp = None
            if resp is not None and resp.status_code not in RETRYABLE:
                resp.raise_for_status()
                return resp
            if attempt == self.max_retries:
                code = resp.status_code if resp is not None else "connection error"
                raise RuntimeError(f"giving up on {url} {params} after {attempt + 1} attempts ({code})")
            self.retries += 1
            time.sleep(self.backoff * (2**attempt) * (0.5 + random.random()))
        raise AssertionError("unreachable")

    def fetch(self, entity: str, stats: EntityStats) -> Iterator[Extracted]:
        url = f"{self.base_url}/api/{entity}"
        page = 1
        while True:
            params = {"page": page, "page_size": self.page_size}
            before = self.retries
            resp = self._get(url, params)
            stats.retries += self.retries - before
            body = resp.json()
            stats.expected_total = body["total"]
            stats.as_of = body.get("as_of")
            full_url = resp.url  # exactly what was requested, query string included
            now = datetime.now(timezone.utc)
            for pos, rec in enumerate(body["results"]):
                if "id" not in rec or rec["id"] in (None, ""):
                    raise ValueError(f"{entity} record without id at page {page} position {pos}")
                stats.received += 1
                yield Extracted(entity, str(rec["id"]), full_url, page, pos, now, rec)
            if not body["has_more"]:
                return
            page += 1
