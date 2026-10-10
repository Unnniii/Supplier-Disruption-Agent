"""Day 1 tests: is the fake data consistent, and can the deterministic Map step
reach the answer key when it is given a correct event description?

If these pass, any later evaluation miss is the LLM's fault (bad Scan), not the
data's or the Map step's. That separation is the point.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent import erp

ROOT = Path(__file__).resolve().parents[1]
TRUTH = json.loads((ROOT / "eval" / "ground_truth.json").read_text())


@pytest.fixture(scope="session", autouse=True)
def seeded_db():
    if not erp.DB_PATH.exists():
        import runpy
        runpy.run_path(str(ROOT / "scripts" / "seed_erp.py"), run_name="__main__")


def _ids(rows):
    return sorted({r["supplier_id"] for r in rows})


# --- Data integrity -----------------------------------------------------------

def test_foreign_keys_hold():
    con = sqlite3.connect(erp.DB_PATH)
    assert con.execute("SELECT count(*) FROM parts WHERE supplier_id NOT IN (SELECT supplier_id FROM suppliers)").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM inventory WHERE part_id NOT IN (SELECT part_id FROM parts)").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM purchase_orders WHERE part_id NOT IN (SELECT part_id FROM parts)").fetchone()[0] == 0


def test_every_supplier_makes_at_least_one_part():
    con = sqlite3.connect(erp.DB_PATH)
    assert con.execute("SELECT count(*) FROM suppliers WHERE supplier_id NOT IN (SELECT supplier_id FROM parts)").fetchone()[0] == 0


def test_answer_key_references_real_suppliers():
    con = sqlite3.connect(erp.DB_PATH)
    known = {r[0] for r in con.execute("SELECT supplier_id FROM suppliers")}
    for nid, case in TRUTH["news"].items():
        assert set(case["expected_suppliers"]) <= known, nid
        assert set(case["acceptable_extra"]) <= known, nid
    for sid, b in TRUTH["backups"].items():
        assert sid in known
        assert b["backup"] is None or b["backup"] in known
        assert (ROOT / "data" / "contracts" / b["contract"]).exists()


def test_contract_backups_match_answer_key():
    for sid, b in TRUTH["backups"].items():
        text = (ROOT / "data" / "contracts" / b["contract"]).read_text()
        if b["backup"]:
            assert b["backup"] in text, f"{sid} contract should name {b['backup']}"


# --- Map step: correct event in -> answer key out ------------------------------

IDEAL_SCANS = {
    "N01": ("place", "Hitachinaka", "fire"),
    "N02": ("place", "Ayutthaya", "flood"),
    "N03": ("place", "Texas Gulf Coast", "severe_weather"),
    "N04": ("place", "Ukraine", "conflict"),
    "N05": ("place", "Fukushima coast", "earthquake"),
    "N06": ("place", "Shanghai", "lockdown"),
    "N07": ("route", "Suez Canal", None),
    "N08": ("port", "Felixstowe", None),
    "N09": ("place", "Pohang", "typhoon"),
    "N21": ("place", "Dongguan", "export_control"),
    "N22": ("country", "China", "Rare earth magnets"),
    "N23": ("place", "Oswego", "fire"),
    "N24": ("place", "Spruce Pine", "hurricane"),
    "N25": ("place", "Paiporta", "flood"),
    "N26": ("place", "Haiphong", "typhoon"),
    "N27": ("place", "Hualien", "earthquake"),
    "N28": ("ports", ["Baltimore"], None),
    "N29": ("route", "Panama Canal", None),
    "N30": ("route", "Red Sea", None),
    "N31": ("ports", ["New York", "Baltimore", "Charleston", "Houston"], None),
    "N32": ("place", "Mandalay", "earthquake"),
}


@pytest.mark.parametrize("news_id", sorted(IDEAL_SCANS))
def test_map_step_hits_answer_key(news_id):
    kind, value, event_type = IDEAL_SCANS[news_id]
    if kind == "place":
        found = _ids(erp.suppliers_near(value, event_type))
    elif kind == "route":
        found = _ids(erp.suppliers_on_route(value))
    elif kind == "country":
        found = _ids(erp.suppliers_in_country(value, event_type))
    elif kind == "ports":
        found = sorted({sid for port in value for sid in _ids(erp.suppliers_using_port(port))})
    else:
        found = _ids(erp.suppliers_using_port(value))
    case = TRUTH["news"][news_id]
    expected = set(case["expected_suppliers"])
    allowed = expected | set(case["acceptable_extra"])
    assert expected <= set(found), f"{news_id} missed {expected - set(found)}"
    assert set(found) <= allowed, f"{news_id} false positives {set(found) - allowed}"


def test_fire_radius_does_not_catch_neighbouring_prefecture():
    # Iwaki (S005) is ~75 km from Hitachinaka: a factory fire must not reach it.
    assert "S005" not in _ids(erp.suppliers_near("Hitachinaka", "fire"))


def test_unknown_place_returns_nothing():
    assert erp.suppliers_near("Atlantis", "flood") == []


def test_days_of_cover_is_computed():
    rows = erp.parts_at_risk("S001")
    assert rows and all(r["days_of_cover"] > 0 for r in rows)


def test_original_30_suppliers_unchanged_by_expansion():
    """Adding suppliers must not change earlier data, or old results stop being comparable."""
    con = sqlite3.connect(erp.DB_PATH)
    row = con.execute("SELECT on_hand_units, daily_usage_units FROM inventory WHERE part_id='P001' ORDER BY plant_id").fetchall()
    assert erp.parts_at_risk("S001")[0]["days_of_cover"] == 6.0  # matches the 2026-10-09 Gemini run
    assert row  # P001 still exists


def test_red_sea_is_the_suez_lane():
    assert _ids(erp.suppliers_on_route("Red Sea")) == _ids(erp.suppliers_on_route("Suez Canal"))


def test_country_tool_filters_by_category():
    assert _ids(erp.suppliers_in_country("China", "Rare earth magnets")) == ["S032", "S033"]
    assert len(erp.suppliers_in_country("China")) > 2


def test_baltimore_needs_the_port_tool_not_a_radius():
    assert erp.suppliers_near("Baltimore", "port_closure") == []
    assert _ids(erp.suppliers_using_port("Baltimore")) == ["S039"]
