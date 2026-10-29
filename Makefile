# Assumes a reachable Postgres (PGHOST/PGPORT/PGUSER/PGDATABASE) and the `psql` client.
PY ?= python3

.PHONY: erp run test eval trace dq

erp:            ## start the simulated ERP API on :8081 (10% transient failures)
	$(PY) -m erp_sim.server --port 8081 --flaky 0.1

run:            ## extract -> raw -> core -> quality gate -> mart
	$(PY) -m pipeline.cli run

dq:             ## show the latest data quality report
	$(PY) -m pipeline.cli dq-report

trace:          ## trace one month's revenue back to the source API records
	$(PY) -m pipeline.cli trace --month 2026-03

eval:           ## rebuild the eval set and score the reference answerers
	$(PY) -m evalset.build_eval
	$(PY) -m evalset.baselines --mode oracle --out /tmp/oracle.jsonl && $(PY) -m evalset.score --answers /tmp/oracle.jsonl
	$(PY) -m evalset.baselines --mode naive_raw --out /tmp/naive.jsonl && $(PY) -m evalset.score --answers /tmp/naive.jsonl

test:
	$(PY) -m pytest
