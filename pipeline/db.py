"""Thin Postgres access layer built on the `psql` client.

Why psql and not a Python driver: it keeps the project dependency-free beyond
`requests` and `flask`, and `\\copy` is the fastest bulk-load path anyway. Swapping
in psycopg is a change to this one file.
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

PSQL_BASE = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1"]


def _env() -> dict:
    env = os.environ.copy()
    env.setdefault("PGHOST", "/tmp")
    env.setdefault("PGPORT", "5433")
    env.setdefault("PGUSER", "pguser")
    env.setdefault("PGDATABASE", "erp_pipeline")
    return env


class DBError(RuntimeError):
    pass


def _run(args: list[str], stdin: str | None = None) -> str:
    proc = subprocess.run(
        PSQL_BASE + args, input=stdin, capture_output=True, text=True, env=_env()
    )
    if proc.returncode != 0:
        raise DBError(proc.stderr.strip() or "psql failed")
    return proc.stdout


def lit(value) -> str:
    """Render a Python value as a SQL literal (for the handful of dynamic values we need)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def execute(sql: str) -> None:
    _run(["-c", sql])


def query(sql: str) -> list[dict]:
    out = _run(["--csv", "-c", sql])
    if not out.strip():
        return []
    return list(csv.DictReader(io.StringIO(out)))


def scalar(sql: str):
    rows = query(sql)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def run_file(path: Path, variables: dict[str, str] | None = None) -> None:
    args = []
    for k, v in (variables or {}).items():
        args += ["-v", f"{k}={v}"]
    _run(args + ["-f", str(path)])


def copy_rows(table: str, columns: Sequence[str], rows: Iterable[Sequence]) -> int:
    buf = io.StringIO()
    writer = csv.writer(buf)
    n = 0
    for r in rows:
        writer.writerow(r)
        n += 1
    cols = ", ".join(columns)
    _run(["-c", f"\\copy {table} ({cols}) from stdin with (format csv)"], stdin=buf.getvalue())
    return n
