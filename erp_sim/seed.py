"""Deterministic seed data for the simulated ERP.

Generates customers, invoices (with lines) and double-entry ledger entries, then
injects a known number of defects of each kind. The injected counts are written to
a manifest so the data quality checks can be tested against ground truth.
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 20260401
N_CUSTOMERS = 120
N_INVOICES = 3000
START = date(2025, 1, 1)
END = date(2026, 6, 30)
AS_OF = date(2026, 9, 30)  # "today" for the simulated ERP

PRODUCTS = [
    ("Platform license", 1200.00),
    ("Implementation hours", 150.00),
    ("Support plan", 400.00),
    ("Data connector", 250.00),
    ("Training session", 800.00),
]
REGIONS = ["North America", "EMEA", "APAC"]
CURRENCIES = ["USD"]


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def build(seed: int = SEED) -> tuple[dict, dict]:
    rng = random.Random(seed)

    customers = []
    for i in range(1, N_CUSTOMERS + 1):
        customers.append(
            {
                "id": f"CUST-{i:04d}",
                "name": f"Customer {i:03d} Ltd",
                "region": rng.choice(REGIONS),
                "created_at": (START - timedelta(days=rng.randint(30, 700))).isoformat(),
                "status": "active" if rng.random() > 0.05 else "inactive",
            }
        )

    invoices, lines, ledger = [], [], []
    line_no = 0
    entry_no = 0
    for n in range(1, N_INVOICES + 1):
        inv_id = f"INV-{n:06d}"
        cust = rng.choice(customers)["id"]
        issued = START + timedelta(days=rng.randint(0, (END - START).days))
        due = issued + timedelta(days=30)
        n_lines = rng.randint(1, 4)
        total = 0.0
        for _ in range(n_lines):
            line_no += 1
            name, price = rng.choice(PRODUCTS)
            qty = rng.randint(1, 10) if "hours" in name else rng.randint(1, 3)
            amount = _money(qty * price)
            total += amount
            lines.append(
                {
                    "id": f"LINE-{line_no:07d}",
                    "invoice_id": inv_id,
                    "product": name,
                    "quantity": qty,
                    "unit_price": price,
                    "amount": amount,
                }
            )
        total = _money(total)
        paid = rng.random() < 0.8
        invoices.append(
            {
                "id": inv_id,
                "customer_id": cust,
                "issued_on": issued.isoformat(),
                "due_on": due.isoformat(),
                "currency": "USD",
                "total": total,
                "status": "paid" if paid else "open",
            }
        )
        # Double-entry: invoice -> debit receivables, credit revenue
        entry_no += 1
        ledger.append(_entry(entry_no, inv_id, issued, "1200-AR", "debit", total, "invoice"))
        entry_no += 1
        ledger.append(_entry(entry_no, inv_id, issued, "4000-REVENUE", "credit", total, "invoice"))
        if paid:
            paid_on = issued + timedelta(days=rng.randint(5, 40))
            entry_no += 1
            ledger.append(_entry(entry_no, inv_id, paid_on, "1000-CASH", "debit", total, "payment"))
            entry_no += 1
            ledger.append(_entry(entry_no, inv_id, paid_on, "1200-AR", "credit", total, "payment"))

    manifest = inject_defects(rng, customers, invoices, lines, ledger)
    erp = {"customers": customers, "invoices": invoices, "invoice_lines": lines, "ledger_entries": ledger}
    return erp, manifest


def _entry(n, inv_id, posted, account, side, amount, kind):
    return {
        "id": f"JE-{n:07d}",
        "invoice_id": inv_id,
        "posted_on": posted.isoformat(),
        "account": account,
        "side": side,
        "amount": amount,
        "kind": kind,
    }


def inject_defects(rng, customers, invoices, lines, ledger) -> dict:
    """Mutate the data in place with a known number of each defect type."""
    m: dict[str, list[str]] = {}

    def pick(pool, k):
        return rng.sample(pool, k)

    # 1. invoices with a null customer
    victims = pick(invoices, 12)
    for v in victims:
        v["customer_id"] = None
    m["invoice_null_customer"] = [v["id"] for v in victims]

    # 2. invoices referencing a customer that does not exist (orphans)
    pool = [i for i in invoices if i["customer_id"] is not None]
    victims = pick(pool, 9)
    for v in victims:
        v["customer_id"] = "CUST-9999"
    m["invoice_orphan_customer"] = [v["id"] for v in victims]

    # 3. invoice header total != sum of its lines
    pool = [i for i in invoices if i["customer_id"] not in (None, "CUST-9999")]
    victims = pick(pool, 15)
    for v in victims:
        v["total"] = _money(v["total"] + rng.choice([-50.0, 25.0, 100.0]))
    m["invoice_total_mismatch"] = [v["id"] for v in victims]

    # 4. negative invoice totals
    pool = [i for i in invoices if i["id"] not in set(m["invoice_total_mismatch"]) and i["customer_id"] not in (None, "CUST-9999")]
    victims = pick(pool, 6)
    for v in victims:
        v["total"] = -abs(v["total"])
    m["invoice_negative_total"] = [v["id"] for v in victims]

    # 5. inconsistent currency casing
    pool = [i for i in invoices if i["id"] not in set(m["invoice_negative_total"])]
    victims = pick(pool, 20)
    for v in victims:
        v["currency"] = rng.choice(["usd", "Usd"])
    m["invoice_currency_casing"] = [v["id"] for v in victims]

    # 6. invoices dated in the future relative to the ERP's as-of date
    pool = [i for i in invoices if i["id"] not in set(m["invoice_currency_casing"])]
    victims = pick(pool, 5)
    for v in victims:
        v["issued_on"] = (AS_OF + timedelta(days=rng.randint(30, 200))).isoformat()
    m["invoice_future_dated"] = [v["id"] for v in victims]

    # 7. unbalanced journal: drop one side of a few invoice entries
    inv_entries = [e for e in ledger if e["kind"] == "invoice" and e["side"] == "credit"]
    victims = pick(inv_entries, 10)
    for v in victims:
        v["amount"] = _money(v["amount"] - rng.choice([10.0, 40.0, 75.0]))
    m["ledger_unbalanced"] = sorted({v["invoice_id"] for v in victims})

    # 8. exact duplicate invoice records (same id returned twice by the API)
    dup_src = pick(invoices, 8)
    m["invoice_duplicate_id"] = [d["id"] for d in dup_src]
    invoices.extend(dict(d) for d in dup_src)

    # 9. ledger entries for invoices that do not exist
    orphan_entries = []
    for k in range(7):
        orphan_entries.append(
            _entry(900000 + k, f"INV-{800000 + k:06d}", date(2026, 3, 1), "4000-REVENUE", "credit", 500.0, "invoice")
        )
    ledger.extend(orphan_entries)
    m["ledger_orphan_invoice"] = [e["id"] for e in orphan_entries]

    rng.shuffle(invoices)
    return m


def write(out_dir: Path, seed: int = SEED) -> dict:
    erp, manifest = build(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "erp_data.json").write_text(json.dumps(erp))
    (out_dir / "defect_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    out = Path(__file__).parent / "data"
    man = write(out)
    for k, v in man.items():
        print(f"{k:28s} {len(v)}")
