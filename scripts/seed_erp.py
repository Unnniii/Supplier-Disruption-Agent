"""Build the fake ERP database for Northgate Motors (a fictional car maker).

Run:  uv run python scripts/seed_erp.py
Output: data/erp.db  (SQLite; regenerated from scratch every run)

The data is deterministic (fixed random seed) so evaluation results are
repeatable. Suppliers S001-S010 are hand-placed at the locations of real past
supply chain disruptions; S011-S030 are ordinary suppliers, some of which are
backups and some of which are deliberate traps (near an event, or in a city that
appears in an irrelevant headline). S031-S041 sit where real 2023-2025
disruptions happened (export controls, fires, floods, typhoons, canal limits).
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "erp.db"
SNAPSHOT_DATE = date(2026, 10, 9)  # "today" for the fake ERP
rng = random.Random(42)

PLANTS = [
    ("PL1", "Midlands Assembly", "Solihull, UK", 52.4128, -1.7780),
    ("PL2", "Merseyside Assembly", "Halewood, UK", 53.3627, -2.8301),
]

MODELS = ["NX-1 SUV", "NX-3 Saloon", "NX-EV"]

# supplier_id, name, city, country, lat, lon, category, tier,
# origin_port, shipping_route, port_of_entry
SUPPLIERS = [
    # --- Hand-placed: sit where real disruptions happened -------------------
    ("S001", "Hitachinaka Microchip KK", "Hitachinaka", "Japan", 36.3966, 140.5345, "Electronics", 2, "Tokyo", "Suez Canal", "Southampton"),
    ("S002", "Ayutthaya Sensor Co", "Ayutthaya", "Thailand", 14.3532, 100.5689, "Electronics", 2, "Laem Chabang", "Suez Canal", "Southampton"),
    ("S003", "Gulf Coast Polymers LLC", "Freeport, Texas", "USA", 28.9541, -95.3597, "Raw material", 2, "Houston", "Atlantic", "Liverpool"),
    ("S004", "Lviv Harness Systems", "Lviv", "Ukraine", 49.8397, 24.0297, "Wiring harness", 1, "Road", "Road via Poland", "Dover"),
    ("S005", "Iwaki Pigments KK", "Iwaki", "Japan", 37.0505, 140.8877, "Paint pigment", 2, "Yokohama", "Suez Canal", "Southampton"),
    ("S006", "Lingang Cell Tech", "Shanghai", "China", 30.9000, 121.9300, "Battery cells", 1, "Shanghai", "Suez Canal", "Felixstowe"),
    ("S007", "Pune Precision Castings", "Pune", "India", 18.5204, 73.8567, "Castings", 1, "Nhava Sheva", "Suez Canal", "Felixstowe"),
    ("S008", "Ningbo Motor Components", "Ningbo", "China", 29.8683, 121.5440, "E-motor parts", 1, "Ningbo", "Suez Canal", "Southampton"),
    ("S009", "Pohang Steel Strip Co", "Pohang", "South Korea", 36.0190, 129.3435, "Steel", 2, "Busan", "Suez Canal", "Felixstowe"),
    ("S010", "Midlands Fasteners Ltd", "Birmingham", "UK", 52.4862, -1.8904, "Fasteners", 1, "Road", "Domestic road", "Domestic"),
    # --- Ordinary suppliers (backups, decoys, the rest of the base) ---------
    ("S011", "Taichung Semiconductor Ltd", "Taichung", "Taiwan", 24.1477, 120.6736, "Electronics", 2, "Kaohsiung", "Suez Canal", "Southampton"),
    ("S012", "Penang Electronics Sdn Bhd", "Penang", "Malaysia", 5.4141, 100.3288, "Electronics", 2, "Penang", "Cape of Good Hope", "Southampton"),
    ("S013", "Lyon Polymeres SA", "Lyon", "France", 45.7640, 4.8357, "Raw material", 2, "Road", "Road via Channel", "Dover"),
    ("S014", "Wroclaw Wiring Sp z o.o.", "Wroclaw", "Poland", 51.1079, 17.0385, "Wiring harness", 1, "Road", "Road via Channel", "Dover"),
    ("S015", "Stuttgart Antriebstechnik GmbH", "Stuttgart", "Germany", 48.7758, 9.1829, "Drivetrain", 1, "Road", "Road via Channel", "Dover"),
    ("S016", "Munich Lighting GmbH", "Munich", "Germany", 48.1351, 11.5820, "Lighting", 1, "Road", "Road via Channel", "Dover"),
    ("S017", "Brno Seating s.r.o.", "Brno", "Czechia", 49.1951, 16.6068, "Seating", 1, "Road", "Road via Channel", "Dover"),
    ("S018", "Gyor Axle Kft", "Gyor", "Hungary", 47.6875, 17.6504, "Axles", 1, "Road", "Road via Channel", "Dover"),
    ("S019", "Torino Brakes SpA", "Turin", "Italy", 45.0703, 7.6869, "Brakes", 1, "Road", "Road via Channel", "Dover"),
    ("S020", "Valencia Glass SL", "Valencia", "Spain", 39.4699, -0.3763, "Glass", 1, "Valencia", "Atlantic", "Southampton"),
    ("S021", "Porto Textiles Lda", "Porto", "Portugal", 41.1579, -8.6291, "Interior textiles", 2, "Leixoes", "Atlantic", "Southampton"),
    ("S022", "Coventry Composites Ltd", "Coventry", "UK", 52.4068, -1.5197, "Composites", 1, "Road", "Domestic road", "Domestic"),
    ("S023", "Wearside Pressings Ltd", "Sunderland", "UK", 54.9069, -1.3838, "Body panels", 1, "Road", "Domestic road", "Domestic"),
    ("S024", "Swansea Coatings Ltd", "Swansea", "UK", 51.6214, -3.9436, "Coatings", 1, "Road", "Domestic road", "Domestic"),
    ("S025", "Monterrey Forgings SA", "Monterrey", "Mexico", 25.6866, -100.3161, "Forgings", 2, "Veracruz", "Atlantic", "Liverpool"),
    ("S026", "Detroit Electronics Inc", "Detroit", "USA", 42.3314, -83.0458, "Electrical", 1, "New York", "Atlantic", "Liverpool"),
    ("S027", "Chennai Auto Electricals", "Chennai", "India", 13.0827, 80.2707, "Electrical", 1, "Chennai", "Suez Canal", "Felixstowe"),
    ("S028", "Gwangju Tyre Co", "Gwangju", "South Korea", 35.1595, 126.8526, "Tyres", 1, "Busan", "Suez Canal", "Southampton"),
    ("S029", "Izmir Rubber AS", "Izmir", "Turkey", 38.4237, 27.1428, "Rubber seals", 2, "Izmir", "Mediterranean", "Southampton"),
    ("S030", "Casablanca Cables SARL", "Casablanca", "Morocco", 33.5731, -7.5898, "Cables", 2, "Casablanca", "Atlantic", "Southampton"),
]

# Added 2026-10-10: suppliers in places with recent (2023-2025) real disruptions.
# Generated with their own random stream so S001-S030 data stays byte-identical.
NEW_SUPPLIERS = [
    ("S031", "Dongguan Discrete Devices Co", "Dongguan", "China", 23.0207, 113.7518, "Electronics", 2, "Yantian", "Suez Canal", "Felixstowe"),
    ("S032", "Baotou Rare Earth Magnetics", "Baotou", "China", 40.6574, 109.8403, "Rare earth magnets", 2, "Tianjin", "Suez Canal", "Felixstowe"),
    ("S033", "Ganzhou Magnet Works", "Ganzhou", "China", 25.8292, 114.9336, "Rare earth magnets", 2, "Xiamen", "Suez Canal", "Southampton"),
    ("S034", "Oswego Aluminium Rolling Inc", "Oswego, New York", "USA", 43.4553, -76.5105, "Aluminium sheet", 1, "New York", "Atlantic", "Liverpool"),
    ("S035", "Spruce Pine Quartz LLC", "Spruce Pine, North Carolina", "USA", 35.9154, -82.0646, "High-purity quartz", 3, "Charleston", "Atlantic", "Southampton"),
    ("S036", "Almussafes Interior Trim SL", "Almussafes", "Spain", 39.2936, -0.4146, "Interior trim", 1, "Valencia", "Atlantic", "Southampton"),
    ("S037", "Haiphong Wire & Cable Co", "Haiphong", "Vietnam", 20.8449, 106.6881, "Wiring harness", 1, "Haiphong", "Suez Canal", "Southampton"),
    ("S038", "Hsinchu Silicon Ltd", "Hsinchu", "Taiwan", 24.8138, 120.9675, "Electronics", 2, "Kaohsiung", "Suez Canal", "Southampton"),
    ("S039", "Chesapeake Bearing Co", "York, Pennsylvania", "USA", 39.9626, -76.7277, "Bearings", 1, "Baltimore", "Atlantic", "Liverpool"),
    ("S040", "Andes Copper Wire SA", "Santiago", "Chile", -33.4489, -70.6693, "Copper wire", 2, "San Antonio", "Panama Canal", "Liverpool"),
    ("S041", "Guadalajara Electronics SA", "Guadalajara", "Mexico", 20.6597, -103.3496, "Electrical", 2, "Manzanillo", "Panama Canal", "Liverpool"),
]
ALL_SUPPLIERS = SUPPLIERS + NEW_SUPPLIERS

# Part names per supplier category. Each supplier makes 2-6 of these.
PART_NAMES = {
    "Electronics": ["Engine control microcontroller", "Body control module chip", "Parking sensor", "Radar sensor", "Camera module", "Power management IC"],
    "Raw material": ["Polypropylene resin", "ABS resin pellets", "Polyamide compound"],
    "Wiring harness": ["Main body harness", "Door harness", "Engine bay harness", "Battery pack harness"],
    "Paint pigment": ["Metallic flake pigment", "Pearlescent pigment"],
    "Battery cells": ["Pouch cell 60Ah", "Cell module assembly", "Battery thermal pad"],
    "Castings": ["Cylinder head casting", "Motor housing casting", "Suspension knuckle casting"],
    "E-motor parts": ["Rotor lamination stack", "Stator winding", "E-motor bearing"],
    "Steel": ["Galvanised steel coil", "High-strength steel strip"],
    "Fasteners": ["M8 flange bolt", "Wheel nut", "Body clip set"],
    "Drivetrain": ["Gearbox assembly", "Drive shaft"],
    "Lighting": ["LED headlamp unit", "Rear lamp cluster"],
    "Seating": ["Front seat frame", "Seat recliner mechanism"],
    "Axles": ["Rear axle assembly", "Front sub-frame"],
    "Brakes": ["Brake caliper", "Brake disc"],
    "Glass": ["Windscreen", "Panoramic roof glass"],
    "Interior textiles": ["Seat fabric roll", "Headliner fabric"],
    "Composites": ["Carbon tailgate panel", "Underbody shield"],
    "Body panels": ["Door outer panel", "Bonnet panel"],
    "Coatings": ["Primer coat", "Clear coat"],
    "Forgings": ["Crankshaft forging", "Steering knuckle forging"],
    "Electrical": ["Alternator", "Starter motor", "DC-DC converter"],
    "Tyres": ["20-inch all-season tyre", "21-inch performance tyre"],
    "Rubber seals": ["Door seal", "Window run channel"],
    "Cables": ["High-voltage cable", "Charging cable assembly"],
    "Rare earth magnets": ["NdFeB traction motor magnet", "Sensor magnet set", "Speaker magnet"],
    "Aluminium sheet": ["Aluminium body sheet", "Aluminium closure panel sheet"],
    "High-purity quartz": ["High-purity quartz sand (for wafer crucibles)", "Quartz glass tubing"],
    "Interior trim": ["Dashboard moulding", "Door trim panel", "Centre console"],
    "Bearings": ["Wheel hub bearing", "Gearbox bearing"],
    "Copper wire": ["Copper magnet wire", "Copper busbar"],
}

CRITICAL_CATEGORIES = {"Electronics", "Wiring harness", "Battery cells", "Paint pigment", "Castings", "Steel", "E-motor parts",
                       "Rare earth magnets", "Aluminium sheet", "High-purity quartz"}


def build_parts(suppliers=SUPPLIERS, rng=rng, start: int = 1) -> list[tuple]:
    parts = []
    n = start
    for s in suppliers:
        sid, category = s[0], s[6]
        names = PART_NAMES[category]
        # hand-placed suppliers make more parts so disruptions hurt
        count = min(len(names), rng.randint(3, 6) if sid <= "S010" else rng.randint(2, 3))
        for name in rng.sample(names, count):
            models = rng.sample(MODELS, rng.randint(1, 3))
            if category in {"Battery cells", "E-motor parts", "Rare earth magnets"}:
                models = ["NX-EV"]
            parts.append((
                f"P{n:03d}",
                name,
                category,
                sid,
                round(rng.uniform(0.2, 900.0), 2),  # unit cost GBP
                rng.choice([14, 21, 28, 42, 56, 70]),  # lead time days
                1 if category in CRITICAL_CATEGORIES else 0,
                ", ".join(sorted(models)),
            ))
            n += 1
    return parts


def build_inventory(parts: list[tuple], rng=rng) -> list[tuple]:
    rows = []
    for p in parts:
        plants = rng.sample(["PL1", "PL2"], rng.randint(1, 2))
        for plant in plants:
            daily = rng.randint(50, 900)
            days_cover = rng.choice([2, 4, 6, 9, 12, 18, 25, 40])
            rows.append((p[0], plant, daily * days_cover, daily, daily * rng.choice([3, 5, 7])))
    return rows


def build_purchase_orders(parts: list[tuple], rng=rng, start: int = 1) -> list[tuple]:
    by_id = {s[0]: s for s in ALL_SUPPLIERS}
    rows = []
    n = start
    for p in parts:
        s = by_id[p[3]]
        for _ in range(rng.randint(0, 2)):
            eta = SNAPSHOT_DATE + timedelta(days=rng.randint(3, 45))
            status = "in_transit" if eta < SNAPSHOT_DATE + timedelta(days=21) else "confirmed"
            rows.append((
                f"PO{n:04d}", p[0], s[0], rng.randint(500, 20000),
                s[8], s[9], s[10], eta.isoformat(), status,
            ))
            n += 1
    return rows


SCHEMA = """
CREATE TABLE plants (
    plant_id TEXT PRIMARY KEY, name TEXT, location TEXT, lat REAL, lon REAL
);
CREATE TABLE suppliers (
    supplier_id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT, country TEXT,
    lat REAL NOT NULL, lon REAL NOT NULL, category TEXT, tier INTEGER,
    origin_port TEXT, shipping_route TEXT, port_of_entry TEXT
);
CREATE TABLE parts (
    part_id TEXT PRIMARY KEY, part_name TEXT NOT NULL, category TEXT,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    unit_cost_gbp REAL, lead_time_days INTEGER, is_critical INTEGER,
    used_in_models TEXT
);
CREATE TABLE inventory (
    part_id TEXT NOT NULL REFERENCES parts(part_id),
    plant_id TEXT NOT NULL REFERENCES plants(plant_id),
    on_hand_units INTEGER, daily_usage_units INTEGER, safety_stock_units INTEGER,
    PRIMARY KEY (part_id, plant_id)
);
CREATE TABLE purchase_orders (
    po_id TEXT PRIMARY KEY,
    part_id TEXT NOT NULL REFERENCES parts(part_id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    quantity INTEGER, origin_port TEXT, shipping_route TEXT, port_of_entry TEXT,
    eta_date TEXT, status TEXT
);
"""


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    parts = build_parts()
    inventory = build_inventory(parts)
    pos = build_purchase_orders(parts)
    # New suppliers: separate stream, numbering continues, originals untouched.
    rng2 = random.Random(2026)
    new_parts = build_parts(NEW_SUPPLIERS, rng2, start=len(parts) + 1)
    inventory += build_inventory(new_parts, rng2)
    pos += build_purchase_orders(new_parts, rng2, start=len(pos) + 1)
    parts += new_parts
    with sqlite3.connect(DB_PATH) as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(SCHEMA)
        con.executemany("INSERT INTO plants VALUES (?,?,?,?,?)", PLANTS)
        con.executemany("INSERT INTO suppliers VALUES (?,?,?,?,?,?,?,?,?,?,?)", ALL_SUPPLIERS)
        con.executemany("INSERT INTO parts VALUES (?,?,?,?,?,?,?,?)", parts)
        con.executemany("INSERT INTO inventory VALUES (?,?,?,?,?)", inventory)
        con.executemany("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?)", pos)
    print(f"Wrote {DB_PATH.relative_to(ROOT)}: {len(ALL_SUPPLIERS)} suppliers, {len(parts)} parts, "
          f"{len(inventory)} inventory rows, {len(pos)} purchase orders")


if __name__ == "__main__":
    main()
