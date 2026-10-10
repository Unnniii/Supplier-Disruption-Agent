"""Orchestration: the LangGraph state machine that runs the agents.

    scanner --(not a disruption)--------------------------------> END
       |
    mapper  --(no supplier exposed)-----------------------------> END
       |
    impact -> advisor -> verifier --(issues, first time)--> advisor
                             |
                             +--(approved, or out of revisions)--> END

Each node catches its own failure and marks the run 'error' instead of crashing
the batch, so one bad news item never stops the morning scan.
"""

from __future__ import annotations

import traceback
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from agent import agents, config, erp
from agent.llm import LLM


class RunState(TypedDict, total=False):
    news: dict
    scan: dict
    suppliers: list[dict]          # [{supplier_id, name, why}]
    impact: dict
    impact_disagreements: list[str]  # agent rating vs the deterministic rule
    advice: dict
    retrieved: dict[str, str]      # chunk_id -> text the advisor actually saw
    issues: list[str]
    backup_warnings: list[dict]    # backups hit by the same event
    revisions: int
    status: str                    # running | no_disruption | no_exposure | approved | needs_review | error
    error: str
    usage: list[dict]


def build_graph(llm: LLM):
    def guarded(name, fn):
        def node(state: RunState) -> RunState:
            try:
                return fn(state)
            except Exception as e:
                return {"status": "error", "error": f"{name}: {type(e).__name__}: {e}",
                        "usage": state.get("usage", []) + [{"agent": name, "traceback": traceback.format_exc(limit=3)}]}
        return node

    def scan_node(state: RunState) -> RunState:
        run = agents.scanner(llm, state["news"])
        scan = run.output
        status = "running" if scan.is_disruption else "no_disruption"
        return {"scan": scan.model_dump(), "status": status, "usage": state.get("usage", []) + [run.usage()]}

    def map_node(state: RunState) -> RunState:
        from agent.schemas import ScanResult
        run = agents.mapper(llm, ScanResult.model_validate(state["scan"]))
        names = {s["supplier_id"]: s["name"] for s in erp.all_suppliers()}
        # The mapper may only report suppliers that exist; drop anything invented.
        mapped, seen = [], set()
        for s in run.output.suppliers:
            if s.supplier_id in names and s.supplier_id not in seen:
                mapped.append({"supplier_id": s.supplier_id, "name": names[s.supplier_id], "why": s.why})
                seen.add(s.supplier_id)
        status = "running" if mapped else "no_exposure"
        return {"suppliers": mapped, "status": status, "usage": state["usage"] + [run.usage()]}

    def impact_node(state: RunState) -> RunState:
        run = agents.impact(llm, [s["supplier_id"] for s in state["suppliers"]])
        checked, disagreements = agents.check_impact(run.output)
        return {"impact": checked, "impact_disagreements": disagreements, "usage": state["usage"] + [run.usage()]}

    def advise_node(state: RunState) -> RunState:
        run = agents.advisor(llm, state["suppliers"], state.get("issues"))
        retrieved = {**state.get("retrieved", {}), **agents.retrieved_chunks(run)}
        return {"advice": run.output.model_dump(), "retrieved": retrieved,
                "usage": state["usage"] + [run.usage()]}

    def verify_node(state: RunState) -> RunState:
        from agent.schemas import AdviceResult
        advice = AdviceResult.model_validate(state["advice"])
        exposed = [s["supplier_id"] for s in state["suppliers"]]
        issues = agents.verify(advice, exposed, state["retrieved"])
        # Warnings, not issues: the contract really does name that backup, so sending
        # the advisor back would not change the answer. The alert must carry it.
        warnings = agents.correlated_backups(advice, exposed)
        revisions = state.get("revisions", 0)
        if not issues:
            return {"issues": [], "backup_warnings": warnings, "status": "approved"}
        if revisions < config.MAX_REVISIONS:
            return {"issues": issues, "revisions": revisions + 1, "status": "revising"}
        return {"issues": issues, "backup_warnings": warnings, "status": "needs_review"}

    def after(state: RunState) -> str:
        return "continue" if state.get("status") in ("running", "revising") else "stop"

    g = StateGraph(RunState)
    g.add_node("scanner", guarded("scanner", scan_node))
    g.add_node("mapper", guarded("mapper", map_node))
    g.add_node("impact", guarded("impact", impact_node))
    g.add_node("advisor", guarded("advisor", advise_node))
    g.add_node("verifier", guarded("verifier", verify_node))
    g.set_entry_point("scanner")
    g.add_conditional_edges("scanner", after, {"continue": "mapper", "stop": END})
    g.add_conditional_edges("mapper", after, {"continue": "impact", "stop": END})
    g.add_conditional_edges("impact", after, {"continue": "advisor", "stop": END})
    g.add_conditional_edges("advisor", after, {"continue": "verifier", "stop": END})
    g.add_conditional_edges("verifier", after, {"continue": "advisor", "stop": END})
    return g.compile()


def run_news(graph, news: dict) -> dict[str, Any]:
    return graph.invoke({"news": news, "status": "running", "usage": [], "revisions": 0})
