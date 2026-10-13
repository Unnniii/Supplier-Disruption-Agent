"""Event memory: alert on what changed, not on every headline.

Five articles about one port strike are one event. Without memory the system
alerts five times, every day, for as long as the strike runs. With memory:

    new        first time this event is seen          -> full analysis, alert
    escalated  known event, and something material changed -> full analysis, alert
    ongoing    known event, nothing new              -> stop early, no alert
    resolved   not seen for WINDOW_DAYS              -> closed; a later report is new again

The LLM extracts what happened and where (the Scanner) and which suppliers are
exposed (the Mapper, through ERP tools). Whether two reports are the same event
is decided here, in plain code, so it is repeatable and testable:

    same event if seen within WINDOW_DAYS and either
      strong: exposed suppliers overlap a lot (Jaccard >= SUPPLIER_OVERLAP), or
      weak:   a shared place/route/port/country AND (same hazard family OR a shared supplier)

    escalated (alert again) if
      a supplier is exposed that was not before   (from the ERP tools: deterministic), or
      severity jumps two levels (low -> high)     (one-level wobble is LLM noise), or
      the hazard family is new for this event     (a flood after a fire at the same plant;
                                                   "flood" vs "severe_weather" is one family: noise)
    otherwise ongoing (no alert)

Why not trust the LLM's labels more: the same flood can be called "flood" one
day and "severe_weather" the next, and "medium" then "high". Supplier exposure
comes from the ERP, so it is the signal the decision leans on. "Houthi attacks"
(conflict) and "shippers worried about Suez transits" (route_blocked) differ in
type but expose the same 15 suppliers, so they are one situation.

Storage: a JSON file on a laptop, a BigQuery table in the cloud (the container's
disk is wiped after every run). The whole state is loaded at the start of a run
and written back at the end; one scan runs at a time, so there is no locking.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from agent import config

WINDOW_DAYS = 14          # an event not reported for this long is over
SUPPLIER_OVERLAP = 0.6    # Jaccard similarity of exposed suppliers that means "same situation"
KEEP_RESOLVED_DAYS = 90   # resolved events are kept this long, then dropped
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
SEVERITY_JUMP = 2         # levels; medium -> high alone is treated as noise
LOCAL_PATH = config.ROOT / "data" / "events.json"

# Labels a model uses interchangeably for one kind of hazard. A change of label
# inside a family is noise ("flood" -> "severe_weather"); a change across
# families is a new hazard ("fire" -> "flood" at the same plant) and escalates.
HAZARD_FAMILY = {
    "route_blocked": "shipping", "conflict": "shipping", "port_closure": "port", "port_strike": "port",
    "flood": "weather", "severe_weather": "weather", "typhoon": "weather", "hurricane": "weather",
    "heatwave": "weather", "fire": "fire", "explosion": "fire", "earthquake": "earthquake",
    "export_control": "trade", "lockdown": "lockdown", "cyberattack": "cyber",
}


def family(event_type: str | None) -> str | None:
    return HAZARD_FAMILY.get(event_type, event_type) if event_type else None


@dataclass
class Event:
    event_id: str
    event_type: str | None
    title: str
    locations: list[str]
    supplier_ids: list[str]
    max_severity: str | None
    first_seen: str            # YYYY-MM-DD, from the article's event date
    last_seen: str
    headline_count: int = 1
    status: str = "open"       # open | resolved
    news_ids: list[str] = field(default_factory=list)
    updated_at: str = ""
    event_types: list[str] = field(default_factory=list)   # every type reports of it were given


def _day(value: str | None) -> date:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return datetime.now(timezone.utc).date()


def locations_of(scan: dict) -> list[str]:
    """Every place the Scanner named, normalised so 'Red Sea' and 'Suez Canal' match."""
    from agent.erp import resolve_route
    locs = set()
    for key in ("places", "ports", "countries"):
        locs.update(x.strip().lower() for x in scan.get(key) or [] if x and x.strip())
    locs.update(resolve_route(r).strip().lower() for r in scan.get("routes") or [] if r and r.strip())
    return sorted(locs)


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def decide(events: list[Event], scan: dict, supplier_ids: list[str], event_date: str) -> dict[str, Any]:
    """Pure function: which event is this report, and what changed? Mutates nothing."""
    today, sups, locs = _day(event_date), set(supplier_ids), set(locations_of(scan))
    etype, efam = scan.get("event_type"), family(scan.get("event_type"))
    best, best_score, why = None, -1.0, ""
    for ev in events:
        if ev.status != "open" or abs((today - _day(ev.last_seen)).days) > WINDOW_DAYS:
            continue
        known = {family(t) for t in (ev.event_types or [ev.event_type])}
        overlap = jaccard(sups, set(ev.supplier_ids))
        shared = locs & set(ev.locations)
        strong = overlap >= SUPPLIER_OVERLAP
        weak = bool(shared) and (efam in known or bool(sups & set(ev.supplier_ids)))
        if not (strong or weak):
            continue
        score = overlap + (0.5 if shared else 0) + (0.25 if efam in known else 0)
        if score > best_score:
            best, best_score = ev, score
            why = (f"{overlap:.0%} of exposed suppliers shared" if strong
                   else f"same place ({', '.join(sorted(shared))})")
    if best is None:
        digest = hashlib.sha1(f"{etype}|{'|'.join(sorted(locs))}|{event_date}".encode()).hexdigest()[:6]
        return {"event_id": f"EV-{today:%Y%m%d}-{digest}", "event_status": "new", "new_suppliers": sorted(sups),
                "matched_on": None}
    added = sorted(sups - set(best.supplier_ids))
    now, before = SEVERITY_RANK.get(scan.get("severity")), SEVERITY_RANK.get(best.max_severity)
    worse = now is not None and before is not None and now - before >= SEVERITY_JUMP
    new_type = efam is not None and efam not in {family(t) for t in (best.event_types or [best.event_type])}
    reasons = [why]
    if added:
        reasons.append(f"new suppliers {', '.join(added)}")
    if worse:
        reasons.append(f"severity {best.max_severity} -> {scan.get('severity')}")
    if new_type:
        reasons.append(f"now also {etype}")
    status = "escalated" if added or worse or new_type else "ongoing"
    return {"event_id": best.event_id, "event_status": status, "new_suppliers": added, "matched_on": "; ".join(reasons)}


# --- storage --------------------------------------------------------------------

class EventStore(Protocol):
    def load(self) -> list[dict]: ...
    def save(self, rows: list[dict]) -> None: ...


class MemoryStore:
    """Nothing persists: used for scored replays and the event sequence."""
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    def load(self) -> list[dict]:
        return list(self.rows)

    def save(self, rows: list[dict]) -> None:
        self.rows = list(rows)


class JsonStore:
    def __init__(self, path: Path = LOCAL_PATH) -> None:
        self.path = path

    def load(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    def save(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


class BigQueryStore:
    """Reads with tabledata.list (free) and rewrites the table with one load job (free)."""
    def __init__(self, table: str, client: Any = None) -> None:
        self.table, self._client = table, client

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery
            self._client = bigquery.Client()
        return self._client

    def load(self) -> list[dict]:
        rows = []
        for r in self.client.list_rows(self.table):
            d = dict(r.items())
            rows.append({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()})
        return rows

    def save(self, rows: list[dict]) -> None:
        from google.cloud import bigquery
        if not rows:  # a load job with no data is not a reliable way to empty a table
            self.client.query(f"TRUNCATE TABLE `{self.table}`").result()
            return
        job = self.client.load_table_from_json(rows, self.table, job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON))
        job.result()


def default_store() -> EventStore:
    table = os.getenv("SDA_BQ_EVENTS")
    return BigQueryStore(table) if table else JsonStore()


class EventMemory:
    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or default_store()
        fields = set(Event.__dataclass_fields__)
        self.events = [Event(**{k: v for k, v in r.items() if k in fields}) for r in self.store.load()]

    def decide(self, scan: dict, supplier_ids: list[str], event_date: str) -> dict[str, Any]:
        return decide(self.events, scan, supplier_ids, event_date)

    def commit(self, decision: dict, scan: dict, supplier_ids: list[str], news: dict) -> None:
        """Record a report once its headline has been fully handled (not on error)."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        day = _day(news.get("event_date")).isoformat()
        ev = next((e for e in self.events if e.event_id == decision["event_id"]), None)
        if ev is None:
            self.events.append(Event(
                event_id=decision["event_id"], event_type=scan.get("event_type"), title=news.get("headline", "")[:200],
                locations=locations_of(scan), supplier_ids=sorted(supplier_ids), max_severity=scan.get("severity"),
                first_seen=day, last_seen=day, news_ids=[news["news_id"]], updated_at=now,
                event_types=[scan["event_type"]] if scan.get("event_type") else []))
            return
        ev.supplier_ids = sorted(set(ev.supplier_ids) | set(supplier_ids))
        ev.locations = sorted(set(ev.locations) | set(locations_of(scan)))
        if SEVERITY_RANK.get(scan.get("severity"), 0) > SEVERITY_RANK.get(ev.max_severity, 0):
            ev.max_severity = scan.get("severity")
        if scan.get("event_type") and scan["event_type"] not in ev.event_types:
            ev.event_types = sorted(set(ev.event_types) | {scan["event_type"]})
        ev.last_seen = max(ev.last_seen, day)
        ev.headline_count += 1
        ev.news_ids = (ev.news_ids + [news["news_id"]])[-20:]
        ev.updated_at = now

    def sweep(self, as_of: str | None = None) -> list[str]:
        """Close events not reported for WINDOW_DAYS; forget long-resolved ones. Returns closed ids."""
        today = _day(as_of)
        closed = []
        for ev in self.events:
            if ev.status == "open" and (today - _day(ev.last_seen)).days > WINDOW_DAYS:
                ev.status = "resolved"
                closed.append(ev.event_id)
        self.events = [e for e in self.events
                       if not (e.status == "resolved" and (today - _day(e.last_seen)).days > KEEP_RESOLVED_DAYS)]
        return closed

    def save(self) -> None:
        # BigQuery rejects "" in DATE/TIMESTAMP columns; send null instead.
        self.store.save([{k: (None if v == "" else v) for k, v in asdict(e).items()} for e in self.events])

    def open_events(self) -> list[Event]:
        return [e for e in self.events if e.status == "open"]

