"""Where run records go besides logs/runs.jsonl.

On a laptop, the JSONL file is enough. In the cloud the container's disk is
thrown away after each run, so records are also appended to BigQuery when
SDA_BQ_TABLE is set (project.dataset.table).

Records are written with a batch *load job*, not streaming inserts: load jobs
are free, streaming inserts are billed per row. One load job per run.
"""

from __future__ import annotations

import json
import os
from typing import Any

# Kept in step with infra/bigquery_schema.json (a test checks they match).
COLUMNS = [
    "run_id", "run_at", "provider", "model", "prompt_version", "news_id", "headline", "kind", "url",
    "status", "error", "is_disruption", "event_type", "supplier_ids", "high_risk_suppliers",
    "min_days_of_cover", "backup_warnings", "revisions", "input_tokens", "output_tokens",
    "cost_usd", "seconds", "record",
    # added with event memory; new columns go at the end so BigQuery can add them in place
    "event_id", "event_status", "model_calls",
]


def to_row(r: dict[str, Any]) -> dict[str, Any]:
    """Flatten one run record: the columns people filter on, plus the full record as JSON text."""
    scan = r.get("scan") or {}
    impacts = (r.get("impact") or {}).get("impacts", [])
    covers = [i["min_days_of_cover"] for i in impacts if i.get("min_days_of_cover") is not None]
    return {
        "run_id": r["run_id"],
        "run_at": r["at"],
        "provider": r.get("provider"),
        "model": r.get("model"),
        "prompt_version": r.get("prompt_version"),
        "news_id": r["news_id"],
        "headline": r.get("headline"),
        "kind": r.get("kind"),
        "url": r.get("url"),
        "status": r.get("status"),
        "error": r.get("error"),
        "is_disruption": scan.get("is_disruption"),
        "event_type": scan.get("event_type"),
        "supplier_ids": [s["supplier_id"] for s in r.get("suppliers") or []],
        "high_risk_suppliers": sum(1 for i in impacts if i.get("risk_level") == "high"),
        "min_days_of_cover": min(covers) if covers else None,
        "backup_warnings": len(r.get("backup_warnings") or []),
        "revisions": r.get("revisions", 0),
        "input_tokens": r.get("input_tokens", 0),
        "output_tokens": r.get("output_tokens", 0),
        "cost_usd": r.get("cost_usd", 0.0),
        "seconds": r.get("seconds"),
        "record": json.dumps(r, default=str),
        "event_id": r.get("event_id"),
        "event_status": r.get("event_status"),
        "model_calls": r.get("model_calls"),
    }


def write_bigquery(records: list[dict], table: str | None = None, client: Any = None) -> int:
    """Append records to BigQuery with one free load job. Returns rows written."""
    table = table or os.getenv("SDA_BQ_TABLE")
    if not table or not records:
        return 0
    from google.cloud import bigquery  # optional dependency: uv sync --extra gcp

    client = client or bigquery.Client()
    job = client.load_table_from_json(
        [to_row(r) for r in records], table,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        ),
    )
    job.result()  # wait, and raise if the load failed
    return len(records)
