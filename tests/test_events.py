"""Event memory: the matching rules, the state over time, storage, and the graph."""

from __future__ import annotations

import json

from agent import events
from agent.events import Event, EventMemory, JsonStore, MemoryStore, decide


def _ev(**kw) -> Event:
    base = dict(event_id="EV-1", event_type="route_blocked", title="t", locations=["suez canal"],
                supplier_ids=["S001", "S002", "S003"], max_severity="high", first_seen="2026-03-01",
                last_seen="2026-03-01")
    base.update(kw)
    return Event(**base)


RED_SEA = {"event_type": "route_blocked", "routes": ["Red Sea"], "severity": "high"}


# --- the decision rules (pure function) ------------------------------------------

def test_first_report_is_new():
    d = decide([], RED_SEA, ["S001"], "2026-03-01")
    assert d["event_status"] == "new" and d["event_id"].startswith("EV-20260301-")


def test_alias_route_and_same_suppliers_is_ongoing():
    d = decide([_ev()], RED_SEA, ["S001", "S002", "S003"], "2026-03-03")
    assert d["event_status"] == "ongoing" and d["event_id"] == "EV-1"     # "Red Sea" == "Suez Canal"


def test_different_type_but_same_suppliers_is_the_same_event():
    scan = {"event_type": "conflict", "countries": ["Yemen"], "severity": "high"}
    d = decide([_ev()], scan, ["S001", "S002", "S003"], "2026-03-03")
    assert d["event_status"] == "ongoing" and "shared" in d["matched_on"]


def test_new_supplier_on_known_event_escalates():
    d = decide([_ev()], RED_SEA, ["S001", "S002", "S003", "S004"], "2026-03-03")
    assert d["event_status"] == "escalated" and d["new_suppliers"] == ["S004"]


def test_higher_severity_escalates():
    d = decide([_ev(max_severity="medium")], RED_SEA, ["S001", "S002", "S003"], "2026-03-03")
    assert d["event_status"] == "escalated" and "severity up" in d["matched_on"]


def test_partial_supplier_overlap_of_a_different_event_is_new():
    strike = {"event_type": "port_strike", "ports": ["Felixstowe"], "severity": "high"}
    d = decide([_ev(supplier_ids=[f"S{i:03d}" for i in range(1, 16)])], strike,
               ["S001", "S002", "S003", "S004", "S005", "S006"], "2026-03-03")   # 6 of 15 = 0.4
    assert d["event_status"] == "new"


def test_outside_the_window_is_new():
    d = decide([_ev(last_seen="2026-03-01")], RED_SEA, ["S001", "S002", "S003"], "2026-04-20")
    assert d["event_status"] == "new"


def test_resolved_events_are_not_matched():
    d = decide([_ev(status="resolved")], RED_SEA, ["S001", "S002", "S003"], "2026-03-02")
    assert d["event_status"] == "new"


# --- state over time ---------------------------------------------------------------

def test_commit_merges_and_sweep_closes_then_forgets():
    mem = EventMemory(MemoryStore())
    news = {"news_id": "X1", "headline": "h", "event_date": "2026-03-01"}
    d = mem.decide(RED_SEA, ["S001"], "2026-03-01")
    mem.commit(d, RED_SEA, ["S001"], news)
    d2 = mem.decide(RED_SEA, ["S001", "S002"], "2026-03-05")
    mem.commit(d2, RED_SEA, ["S001", "S002"], {**news, "news_id": "X2", "event_date": "2026-03-05"})
    ev = mem.open_events()[0]
    assert (ev.headline_count, ev.supplier_ids, ev.last_seen) == (2, ["S001", "S002"], "2026-03-05")
    assert mem.sweep("2026-03-15") == []                    # 10 days quiet: still open
    assert mem.sweep("2026-03-25") == [ev.event_id]         # 20 days quiet: closed
    mem.sweep("2026-07-01")                                 # long after: dropped
    assert mem.events == []


def test_json_store_round_trip(tmp_path):
    store = JsonStore(tmp_path / "events.json")
    mem = EventMemory(store)
    mem.commit(mem.decide(RED_SEA, ["S001"], "2026-03-01"), RED_SEA, ["S001"],
               {"news_id": "X1", "headline": "h", "event_date": "2026-03-01"})
    mem.save()
    again = EventMemory(store)
    assert [e.event_id for e in again.events] == [e.event_id for e in mem.events]


def test_bigquery_store_reads_free_and_rewrites_with_one_load_job():
    from datetime import date
    from google.cloud import bigquery

    class Row(dict):
        def items(self):
            return super().items()

    class Client:
        def __init__(self):
            self.loads = []

        def list_rows(self, table):
            return [Row(event_id="EV-1", event_type="flood", title="t", locations=["taipei"], supplier_ids=["S038"],
                        max_severity="high", first_seen=date(2026, 3, 9), last_seen=date(2026, 3, 9),
                        headline_count=1, status="open", news_ids=["E07"], updated_at="x")]

        def load_table_from_json(self, rows, table, job_config):
            self.loads.append((list(rows), table, job_config))
            return type("J", (), {"result": lambda self: None})()

    client = Client()
    mem = EventMemory(events.BigQueryStore("p.d.events", client))
    assert mem.events[0].first_seen == "2026-03-09"         # dates come back as text
    mem.save()
    rows, table, cfg = client.loads[0]
    assert table == "p.d.events" and cfg.write_disposition == bigquery.WriteDisposition.WRITE_TRUNCATE
    assert rows[0]["event_id"] == "EV-1"


def test_events_schema_matches_the_dataclass():
    from agent import config
    schema = json.loads((config.ROOT / "infra" / "bigquery_events_schema.json").read_text())
    assert [c["name"] for c in schema] == list(Event.__dataclass_fields__)


# --- in the graph ------------------------------------------------------------------

def test_known_event_stops_before_impact_and_saves_model_calls():
    from agent.run import load_sequence, scan
    seq = {n["news_id"]: n for n in load_sequence()}
    mem = EventMemory(MemoryStore())
    first, repeat = scan([seq["E01"], seq["E02"]], fake=True, log=False, memory=mem)
    assert (first["event_status"], first["status"]) == ("new", "approved")
    assert (repeat["event_status"], repeat["status"]) == ("ongoing", "ongoing")
    assert repeat["impact"] is None and repeat["advice"] is None          # impact and advisor never ran
    assert repeat["model_calls"] < first["model_calls"]


def test_replay_without_memory_is_unchanged():
    from agent.run import load_news, scan
    r = scan(load_news(["N07"]), fake=True, log=False)[0]
    assert r["event_status"] is None and r["status"] == "approved"


def test_full_sequence_with_the_stand_in_model_scores_10_of_10():
    from agent.evaluate import score_events
    from agent.run import load_sequence, scan
    seq = load_sequence()
    records = scan(seq, fake=True, log=False, memory=EventMemory(MemoryStore()))
    ev = score_events(records, seq)
    assert ev["decisions_right"] == len(seq) and ev["grouping_accuracy"] == 1.0
    assert ev["alerts"] < ev["alerts_without_memory"]
