"""The cloud pieces that can be tested without a cloud: the BigQuery writer,
its schema, and the files the deployment depends on."""

from __future__ import annotations

import json

from agent import config
from agent.run import load_news, scan
from agent.sinks import COLUMNS, to_row, write_bigquery


class FakeJob:
    def __init__(self):
        self.waited = False

    def result(self):
        self.waited = True


class FakeBQ:
    def __init__(self):
        self.calls = []
        self.job = FakeJob()

    def load_table_from_json(self, rows, table, job_config):
        self.calls.append({"rows": list(rows), "table": table, "config": job_config})
        return self.job


def test_schema_file_matches_writer_columns():
    schema = json.loads((config.ROOT / "infra" / "bigquery_schema.json").read_text())
    assert [c["name"] for c in schema] == COLUMNS
    assert {c["name"] for c in schema if c["mode"] == "REQUIRED"} == {"run_id", "run_at", "news_id"}


def test_rows_flatten_a_real_record():
    rec = scan(load_news(["N07"]), fake=True, log=False)[0]
    row = to_row(rec)
    assert list(row) == COLUMNS
    assert row["supplier_ids"] and all(s.startswith("S") for s in row["supplier_ids"])
    assert row["is_disruption"] is True and row["event_type"]
    assert row["backup_warnings"] == len(rec["backup_warnings"])
    assert json.loads(row["record"])["news_id"] == "N07"   # full record kept for audit


def test_rows_for_a_non_disruption_have_no_suppliers():
    rec = scan(load_news(["N33"]), fake=True, log=False)[0]
    row = to_row(rec)
    assert row["supplier_ids"] == [] and row["min_days_of_cover"] is None and row["high_risk_suppliers"] == 0


def test_bigquery_uses_one_free_load_job_and_appends():
    from google.cloud import bigquery
    recs = scan(load_news(["N01", "N33"]), fake=True, log=False)
    client = FakeBQ()
    n = write_bigquery(recs, table="p.d.runs", client=client)
    assert n == 2 and len(client.calls) == 1 and client.job.waited
    cfg = client.calls[0]["config"]
    assert cfg.write_disposition == bigquery.WriteDisposition.WRITE_APPEND
    assert client.calls[0]["table"] == "p.d.runs"


def test_bigquery_is_skipped_without_a_table(monkeypatch):
    monkeypatch.delenv("SDA_BQ_TABLE", raising=False)
    assert write_bigquery([{"run_id": "x"}], client=FakeBQ()) == 0


def test_deployment_files_exist():
    for f in ["Dockerfile", ".dockerignore", "infra/main.tf", "infra/variables.tf", "infra/outputs.tf",
              "scripts/deploy_gcp.ps1"]:
        assert (config.ROOT / f).exists(), f


def test_every_file_read_and_write_is_utf8():
    """Windows defaults to cp1252, which cannot write ✓ or most non-English headlines."""
    import re
    bad = []
    for path in list((config.ROOT / "src").rglob("*.py")) + list((config.ROOT / "scripts").glob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.(read_text|write_text)\(", line) and "encoding=" not in line:
                bad.append(f"{path.name}:{n}")
            if re.search(r"\.open\(\"[aw]\"\)", line):
                bad.append(f"{path.name}:{n}")
    assert not bad, bad
