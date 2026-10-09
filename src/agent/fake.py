"""A rule-based stand-in for Claude, so the whole multi-agent graph runs offline,
for free, in tests and in `--fake` mode.

It is deliberately naive (keyword matching) and it never reads the answer key.
Its scores are a floor to beat, not a result. It speaks the same tool-use
protocol as the real model: first turn asks for tools, next turn answers.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agent.llm import LLMResponse, ToolCall

_INPUT = re.compile(r"<input>\n(.*)\n</input>", re.DOTALL)

NOT_DISRUPTION = ["no closures", "operating normally", "accept", "no strike", "record profit", "record crowds",
                  "win", "festival", "unveil", "interest rate", "holds interest"]
EVENT_WORDS = [  # first match wins, so specific before general
    ("aground", "route_blocked"), ("blocks the suez", "route_blocked"), ("strike", "port_strike"),
    ("earthquake", "earthquake"), ("tsunami", "earthquake"), ("typhoon", "typhoon"), ("lockdown", "lockdown"),
    ("war ", "conflict"), ("invasion", "conflict"), ("winter storm", "severe_weather"), ("power cuts", "severe_weather"),
    ("flood", "flood"), ("fire", "fire"), ("blaze", "fire"), ("cyberattack", "cyberattack"), ("heatwave", "heatwave"),
]


class FakeLLM:
    model = "fake"

    def __init__(self) -> None:
        self._n = 0
        self._known: dict[str, Any] | None = None

    # -- protocol --------------------------------------------------------------
    def complete(self, *, agent, system, messages, tools=None) -> LLMResponse:
        payload = json.loads(_INPUT.search(messages[0]["content"]).group(1))
        results = self._tool_results(messages)
        handler = getattr(self, f"_{agent}")
        out = handler(payload, results)
        if isinstance(out, list):  # tool calls
            calls = []
            for name, args in out:
                self._n += 1
                calls.append(ToolCall(f"fake_{self._n}", name, args))
            raw = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in calls]
            return LLMResponse("", calls, raw_content=raw)
        return LLMResponse(json.dumps(out), [], raw_content=[{"type": "text", "text": json.dumps(out)}])

    @staticmethod
    def _tool_results(messages) -> list[tuple[str, Any]]:
        """(tool_name, parsed result) for every tool call answered so far."""
        names = {}
        out = []
        for m in messages:
            if isinstance(m["content"], list):
                for b in m["content"]:
                    if b.get("type") == "tool_use":
                        names[b["id"]] = b["name"]
                    elif b.get("type") == "tool_result" and not b.get("is_error"):
                        out.append((names[b["tool_use_id"]], json.loads(b["content"])))
        return out

    # -- agents ----------------------------------------------------------------
    def _scanner(self, p, _):
        text = f"{p['headline']} {p['body']}".lower()
        event = next((t for w, t in EVENT_WORDS if w in text), None)
        if event is None or any(re.search(rf"\b{re.escape(w)}\b", text) for w in NOT_DISRUPTION):
            return {"is_disruption": False, "event_type": event, "reason": "no disruption keywords"}
        from agent.erp import GAZETTEER_PATH
        gaz = json.loads(GAZETTEER_PATH.read_text())
        places = [pl for pl in sorted(gaz["places"], key=len, reverse=True) if pl.lower() in text]
        routes = [r for r in gaz["routes"] if r.split()[0].lower() in text and r == "Suez Canal"]
        ports = [pt for pt in gaz["ports"] if pt.lower() in text and event in ("port_strike", "lockdown")]
        if routes:
            places = []
        return {"is_disruption": True, "event_type": event, "places": places, "routes": routes,
                "ports": ports, "severity": "high", "reason": "keyword match"}

    def _mapper(self, p, results):
        if not results:
            calls = [("find_suppliers_near_place", {"place": pl, "event_type": p["event_type"]}) for pl in p["places"]]
            calls += [("find_suppliers_on_route", {"route": r}) for r in p["routes"]]
            calls += [("find_suppliers_using_port", {"port": pt}) for pt in p["ports"]]
            return calls or {"suppliers": []}
        found = {}
        for name, rows in results:
            for r in rows:
                found.setdefault(r["supplier_id"], f"{name}: {r.get('city', '')}")
        return {"suppliers": [{"supplier_id": k, "why": v} for k, v in found.items()]}

    def _impact(self, p, results):
        if not results:
            return [("get_parts_at_risk", {"supplier_id": s}) for s in p["supplier_ids"]]
        impacts = []
        for sid, (_, rows) in zip(p["supplier_ids"], results):
            covers = [r["days_of_cover"] for r in rows]
            crit_low = any(r["is_critical"] and r["days_of_cover"] < 7 for r in rows)
            level = "high" if crit_low else "medium" if covers and min(covers) < 14 else "low"
            impacts.append({"supplier_id": sid, "risk_level": level, "min_days_of_cover": min(covers) if covers else None,
                            "parts": [{k: r[k] for k in ("part_id", "part_name", "plant_id", "days_of_cover",
                                                         "is_critical", "used_in_models")} for r in rows]})
        return {"impacts": impacts}

    def _advisor(self, p, results):
        sids = [s["supplier_id"] for s in p["suppliers"]]
        if not results:
            return [("search_contracts", {"query": "second source backup supplier alternate approved",
                                          "supplier_id": s}) for s in sids]
        advice = []
        for sid, (_, hits) in zip(sids, results):
            if not hits:
                advice.append({"supplier_id": sid, "coverage": "unknown", "actions": ["No contract on file: call the supplier"]})
                continue
            top = hits[0]
            text = top["text"]
            backup = next((b for b in re.findall(r"\bS\d{3}\b", text) if b != sid), None)
            low = text.lower()
            if re.search(r"\bno (second source|approved|contracted|qualified)|sole qualified", low):
                coverage, backup = "none", None
            elif backup:
                coverage = "partial" if " only" in low else "full"
            else:
                coverage = "unknown"
            actions = [f"Contact {backup} to request surge capacity"] if backup else ["Protect remaining stock"]
            advice.append({"supplier_id": sid, "backup_supplier_id": backup, "coverage": coverage,
                           "actions": actions, "citations": [top["chunk_id"]]})
        return {"advice": advice}
