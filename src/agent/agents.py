"""The four LLM agents and the verifier.

Each agent = its own instructions + its own tools + its own output schema.
They share one tool-use loop (`run_tool_agent`).

    Scanner   reads one news item            no tools        -> ScanResult
    Mapper    finds exposed suppliers        ERP lookups     -> MapResult
    Impact    sizes the damage               parts + stock   -> ImpactResult
    Advisor   finds the contractual fallback contract RAG    -> AdviceResult
    Verifier  checks the advisor's claims    plain code, no LLM
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from agent import config, erp
from agent.llm import LLM
from agent.schemas import AdviceResult, ImpactResult, MapResult, ScanResult
from agent.tools import ADVISOR_TOOLS, IMPACT_TOOLS, MAPPER_TOOLS, Tool

PROMPT_VERSION = "v1"


class AgentError(RuntimeError):
    pass


@dataclass
class AgentRun:
    agent: str
    output: Any = None  # the validated schema object once the agent finishes
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    repairs: int = 0

    def usage(self) -> dict[str, Any]:
        return {"agent": self.agent, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "tool_calls": len(self.tool_calls), "repairs": self.repairs, "seconds": round(self.seconds, 2)}


_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _parse(text: str, schema: type[BaseModel]) -> BaseModel:
    m = _JSON.search(text)
    if not m:
        raise AgentError(f"no JSON object in reply: {text[:200]!r}")
    try:
        return schema.model_validate(json.loads(m.group(0)))
    except (json.JSONDecodeError, ValidationError) as e:
        raise AgentError(f"reply did not match {schema.__name__}: {e}") from e


def _output_rule(schema: type[BaseModel]) -> str:
    return ("\n\nWhen you are finished, reply with ONLY one JSON object matching this JSON schema, no prose:\n"
            + json.dumps(schema.model_json_schema()))


def _input(payload: Any) -> str:
    return "<input>\n" + json.dumps(payload, default=str, indent=1) + "\n</input>"


def run_tool_agent(llm: LLM, *, agent: str, system: str, payload: Any,
                   tools: list[Tool], schema: type[BaseModel]) -> AgentRun:
    """Tool-use loop: ask, run requested tools, feed results back, repeat."""
    by_name = {t.name: t for t in tools}
    messages: list[dict[str, Any]] = [{"role": "user", "content": _input(payload)}]
    run = AgentRun(agent=agent)
    started = time.perf_counter()
    for _ in range(config.MAX_AGENT_TURNS):
        resp = llm.complete(agent=agent, system=system + _output_rule(schema), messages=messages,
                            tools=[t.spec() for t in tools] or None)
        run.input_tokens += resp.input_tokens
        run.output_tokens += resp.output_tokens
        if not resp.tool_calls:
            try:
                run.output = _parse(resp.text, schema)
            except AgentError as e:
                if run.repairs >= 1:
                    raise
                # One repair turn: show the model its own error and ask again.
                run.repairs += 1
                messages.append({"role": "assistant", "content": resp.raw_content or [{"type": "text", "text": resp.text}]})
                messages.append({"role": "user", "content": f"Your reply was not valid JSON for the schema ({e}). "
                                 "Reply again with ONLY the corrected, complete JSON object."})
                continue
            run.seconds = time.perf_counter() - started
            return run
        messages.append({"role": "assistant", "content": resp.raw_content})
        results = []
        for call in resp.tool_calls:
            tool = by_name.get(call.name)
            try:
                if tool is None:
                    raise AgentError(f"unknown tool {call.name}")
                out, is_error = tool.run(call.args), False
            except Exception as e:  # a failed tool is reported to the model, not raised
                out, is_error = f"ERROR: {type(e).__name__}: {e}", True
            run.tool_calls.append({"tool": call.name, "args": call.args, "is_error": is_error, "result": out})
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": out, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    raise AgentError(f"{agent} did not finish within {config.MAX_AGENT_TURNS} turns")


# --- Agents -------------------------------------------------------------------

SCANNER_SYSTEM = """You are the Scanner in a supply chain risk team at a UK car maker.
Read ONE news item and decide whether it describes an event that could stop or delay
industrial production or freight: fires or explosions at plants, floods, earthquakes,
storms, typhoons, hurricanes, lockdowns, wars, export controls or bans on goods,
blocked or restricted shipping routes, port strikes or closures.
Not disruptions: sport, earnings, product launches, festivals, interest rates, strikes
that were called off, weather where the article says operations continue normally.
Name places as specifically as the article allows (city before country). Use
`countries` only for country-wide events, and `products` when the event targets
one kind of product (e.g. chips, rare earth magnets)."""

MAPPER_SYSTEM = """You are the Mapper. You receive an event from the Scanner and must find
which of the company's suppliers are exposed to it, using ONLY the tools.
Steps: call list_known_locations; translate the event's places, routes, ports, countries
and products into names the tools understand (choose the closest known place, never
invent one); call the finder tools; return every supplier the tools returned, with the reason.
- Physical events (fire, flood, storm, quake): find_suppliers_near_place.
- Shipping lanes (canal, strait, Red Sea): find_suppliers_on_route.
- Port strikes or closures: find_suppliers_using_port, once per port named.
- Country-wide policy (export controls, sanctions): find_suppliers_in_country, with the
  supplier category when the policy targets one product type.
Do not add suppliers the tools did not return. If nothing matches, return an empty list."""

IMPACT_SYSTEM = """You are the Impact analyst. For each exposed supplier, call get_parts_at_risk
(and get_open_purchase_orders if useful). Rate each supplier:
- high: any critical part with under 7 days of cover at any plant
- medium: any part with under 14 days of cover
- low: otherwise
min_days_of_cover is the lowest days_of_cover across that supplier's parts.
Name at most 3 driver_part_ids per supplier; do not copy part details, code attaches them.
Copy numbers from tool results; never estimate."""

ADVISOR_SYSTEM = """You are the Contract advisor. For each exposed supplier, search that supplier's
contract (search_contracts with its supplier_id) for: second source / backup supplier,
force majeure notice period, buffer or consignment stock, freight terms.
Rules:
- backup_supplier_id only if a clause you retrieved names it; otherwise null.
- coverage: full = backup covers all parts, partial = some parts only, none = contract says
  no second source, unknown = no clause found.
- Every advice item must cite the chunk_ids it relies on.
- actions: 2-4 short, concrete next steps for the buyer, grounded in the clauses."""


def scanner(llm: LLM, news: dict) -> AgentRun:
    return run_tool_agent(llm, agent="scanner", system=SCANNER_SYSTEM,
                          payload={"headline": news["headline"], "body": news["body"], "date": news["event_date"]},
                          tools=[], schema=ScanResult)


def mapper(llm: LLM, scan: ScanResult) -> AgentRun:
    return run_tool_agent(llm, agent="mapper", system=MAPPER_SYSTEM, payload=scan.model_dump(),
                          tools=MAPPER_TOOLS, schema=MapResult)


def impact(llm: LLM, supplier_ids: list[str]) -> AgentRun:
    return run_tool_agent(llm, agent="impact", system=IMPACT_SYSTEM, payload={"supplier_ids": supplier_ids},
                          tools=IMPACT_TOOLS, schema=ImpactResult)


def advisor(llm: LLM, suppliers: list[dict], issues: list[str] | None = None) -> AgentRun:
    payload: dict[str, Any] = {"suppliers": suppliers}
    if issues:
        payload["verifier_issues_to_fix"] = issues
    return run_tool_agent(llm, agent="advisor", system=ADVISOR_SYSTEM, payload=payload,
                          tools=ADVISOR_TOOLS, schema=AdviceResult)


# --- Deterministic checks (code, not an LLM) ------------------------------------

def rule_risk(rows: list[dict]) -> tuple[str, float | None]:
    """The rating rule from IMPACT_SYSTEM, in code. The source of truth."""
    if not rows:
        return "low", None
    covers = [r["days_of_cover"] for r in rows]
    if any(r["is_critical"] and r["days_of_cover"] < 7 for r in rows):
        level = "high"
    elif min(covers) < 14:
        level = "medium"
    else:
        level = "low"
    return level, min(covers)


def check_impact(impact: ImpactResult) -> tuple[dict, list[str]]:
    """Attach part details from the ERP and compare the agent's rating with the rule.
    The rule wins; disagreements are kept so the evaluation can count them."""
    out, disagreements = [], []
    for i in impact.impacts:
        rows = erp.parts_at_risk(i.supplier_id)
        level, min_cover = rule_risk(rows)
        if i.risk_level != level:
            disagreements.append(f"{i.supplier_id}: agent said {i.risk_level}, rule says {level}")
        drivers = set(i.driver_part_ids)
        parts = [r for r in rows if r["part_id"] in drivers] or sorted(rows, key=lambda r: r["days_of_cover"])[:3]
        out.append({"supplier_id": i.supplier_id, "risk_level": level, "agent_risk_level": i.risk_level,
                    "min_days_of_cover": min_cover, "summary": i.summary,
                    "parts": [{k: r[k] for k in ("part_id", "part_name", "plant_id", "days_of_cover",
                                                 "is_critical", "used_in_models")} for r in parts]})
    return {"impacts": out}, disagreements


# --- Verifier (code, not an LLM) -----------------------------------------------

def retrieved_chunks(run: AgentRun) -> dict[str, str]:
    """chunk_id -> text for every clause the advisor actually retrieved."""
    seen: dict[str, str] = {}
    for call in run.tool_calls:
        if call["tool"] == "search_contracts" and not call["is_error"]:
            for hit in json.loads(call["result"]):
                seen[hit["chunk_id"]] = hit["text"]
    return seen


def verify(advice: AdviceResult, supplier_ids: list[str], retrieved: dict[str, str]) -> list[str]:
    """Return a list of problems. Empty list = approved."""
    issues = []
    known = {s["supplier_id"] for s in erp.all_suppliers()}
    advised = {a.supplier_id for a in advice.advice}
    for sid in supplier_ids:
        if sid not in advised:
            issues.append(f"{sid}: no advice given for an exposed supplier")
    for a in advice.advice:
        if a.supplier_id not in supplier_ids:
            issues.append(f"{a.supplier_id}: advice for a supplier that was not flagged")
        if a.coverage != "unknown" and not a.citations:
            issues.append(f"{a.supplier_id}: coverage '{a.coverage}' claimed with no citation")
        for c in a.citations:
            if c not in retrieved:
                issues.append(f"{a.supplier_id}: cites {c}, which was never retrieved")
        if a.backup_supplier_id:
            if a.backup_supplier_id not in known:
                issues.append(f"{a.supplier_id}: backup {a.backup_supplier_id} does not exist in the ERP")
            elif a.backup_supplier_id == a.supplier_id:
                issues.append(f"{a.supplier_id}: backup is the same supplier")
            elif not any(a.backup_supplier_id in retrieved.get(c, "") for c in a.citations):
                issues.append(f"{a.supplier_id}: backup {a.backup_supplier_id} is not named in any cited clause")
    return issues
