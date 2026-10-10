"""Tools each agent may call. Every agent gets ONLY the tools it needs; that
separation is what makes them different agents rather than one big prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from agent import erp
from agent.rag import get_retriever


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    def run(self, args: dict[str, Any]) -> str:
        return json.dumps(self.fn(**args), default=str)


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_SUPPLIER_FIELDS = ("supplier_id", "name", "city", "country", "category", "shipping_route", "port_of_entry", "distance_km")


def _slim(rows: list[dict]) -> list[dict]:
    return [{k: r[k] for k in _SUPPLIER_FIELDS if k in r} for r in rows]


def _known_places() -> dict:
    data = json.loads(erp.GAZETTEER_PATH.read_text())
    return {"places": sorted(data["places"]), "routes": data["routes"],
            "route_aliases": data.get("route_aliases", {}), "ports": data["ports"],
            "countries": erp.countries(), "supplier_categories": erp.categories(),
            "event_types": sorted(erp.EVENT_RADIUS_KM)}


MAPPER_TOOLS = [
    Tool("list_known_locations",
         "List the place names, shipping routes, ports and event types the ERP tools understand. Call this first.",
         _obj({}, []), _known_places),
    Tool("find_suppliers_near_place",
         "Suppliers inside the impact radius of an event at a named place. The radius depends on event_type "
         "(a fire is ~10 km, an earthquake ~200 km). The place must be one of list_known_locations().places.",
         _obj({"place": {"type": "string"}, "event_type": {"type": "string"}}, ["place", "event_type"]),
         lambda place, event_type: _slim(erp.suppliers_near(place, event_type))),
    Tool("find_suppliers_on_route",
         "Suppliers whose shipments to the UK use this shipping route (e.g. 'Suez Canal').",
         _obj({"route": {"type": "string"}}, ["route"]),
         lambda route: _slim(erp.suppliers_on_route(route))),
    Tool("find_suppliers_in_country",
         "Suppliers in a whole country, optionally only one supplier category. Use for country-wide events "
         "such as export controls, sanctions or war, not for a fire or flood at one place.",
         _obj({"country": {"type": "string"}, "category": {"type": "string"}}, ["country"]),
         lambda country, category=None: _slim(erp.suppliers_in_country(country, category))),
    Tool("find_suppliers_using_port",
         "Suppliers that ship from, or into, this port (e.g. 'Felixstowe', 'Shanghai').",
         _obj({"port": {"type": "string"}}, ["port"]),
         lambda port: _slim(erp.suppliers_using_port(port))),
]

IMPACT_TOOLS = [
    Tool("get_parts_at_risk",
         "Parts made by a supplier, with stock on hand, daily usage and days_of_cover per plant, "
         "whether the part is critical, and which vehicle models use it.",
         _obj({"supplier_id": {"type": "string"}}, ["supplier_id"]),
         lambda supplier_id: erp.parts_at_risk(supplier_id)),
    Tool("get_open_purchase_orders",
         "Open purchase orders from a supplier with route, port and ETA.",
         _obj({"supplier_id": {"type": "string"}}, ["supplier_id"]),
         lambda supplier_id: erp.open_purchase_orders(supplier_id)),
]

ADVISOR_TOOLS = [
    Tool("search_contracts",
         "Search supplier contracts. Returns the most relevant clauses with a chunk_id you must cite. "
         "Pass supplier_id to search only that supplier's contract.",
         _obj({"query": {"type": "string"}, "supplier_id": {"type": "string"}}, ["query"]),
         lambda query, supplier_id=None: get_retriever().search(query, k=4, supplier_id=supplier_id)),
]
