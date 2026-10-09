"""Deterministic ERP tools. No LLM in here on purpose.

The Map and Impact steps of the agent call these functions. Keeping them plain
Python means they are cheap, testable and always give the same answer.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "erp.db"
GAZETTEER_PATH = ROOT / "data" / "gazetteer.json"

# How far an event reaches, by type (km). A factory fire is a point; an
# earthquake or a war covers a region. Tuned against eval/ground_truth.json.
EVENT_RADIUS_KM = {
    "fire": 10,
    "explosion": 10,
    "cyberattack": 0,
    "lockdown": 80,
    "flood": 100,
    "typhoon": 75,
    "heatwave": 50,
    "severe_weather": 300,
    "earthquake": 200,
    "conflict": 600,
}
DEFAULT_RADIUS_KM = 50


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode(place: str) -> tuple[float, float] | None:
    places = json.loads(GAZETTEER_PATH.read_text())["places"]
    for name, coords in places.items():
        if name.lower() == place.strip().lower():
            return coords[0], coords[1]
    return None


def suppliers_near(place: str, event_type: str) -> list[dict]:
    """Suppliers within the event's radius of a named place, nearest first."""
    coords = geocode(place)
    if coords is None:
        return []
    radius = EVENT_RADIUS_KM.get(event_type, DEFAULT_RADIUS_KM)
    with _connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM suppliers")]
    hits = []
    for r in rows:
        d = haversine_km(coords[0], coords[1], r["lat"], r["lon"])
        if d <= radius:
            hits.append({**r, "distance_km": round(d, 1)})
    return sorted(hits, key=lambda r: r["distance_km"])


def suppliers_on_route(route: str) -> list[dict]:
    with _connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM suppliers WHERE lower(shipping_route) = lower(?)", (route,))]


def suppliers_using_port(port: str) -> list[dict]:
    """Suppliers that ship from OR into this port."""
    with _connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM suppliers WHERE lower(port_of_entry) = lower(?) OR lower(origin_port) = lower(?)",
            (port, port))]


def parts_at_risk(supplier_id: str) -> list[dict]:
    """Parts from a supplier with days of stock cover per plant."""
    with _connect() as con:
        rows = con.execute(
            """
            SELECT p.part_id, p.part_name, p.is_critical, p.used_in_models, p.lead_time_days,
                   i.plant_id, i.on_hand_units, i.daily_usage_units, i.safety_stock_units
            FROM parts p JOIN inventory i ON i.part_id = p.part_id
            WHERE p.supplier_id = ?
            ORDER BY p.part_id, i.plant_id
            """,
            (supplier_id,),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["days_of_cover"] = round(d["on_hand_units"] / d["daily_usage_units"], 1)
            out.append(d)
        return out


def open_purchase_orders(supplier_id: str) -> list[dict]:
    with _connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM purchase_orders WHERE supplier_id = ? ORDER BY eta_date", (supplier_id,))]
