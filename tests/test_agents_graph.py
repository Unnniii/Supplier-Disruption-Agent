"""Behaviour of the multi-agent graph, offline and free.

Covers: routing (early exits), the tool-use loop's failure handling, the
verifier's checks, the advisor revise loop, and error isolation.
"""

from __future__ import annotations

import json

import pytest

from agent import agents
from agent.fake import FakeLLM
from agent.graph import build_graph, run_news
from agent.llm import LLMResponse, ToolCall
from agent.run import load_news, scan
from agent.schemas import AdviceResult, MapResult


def _by_id(results):
    return {r["news_id"]: r for r in results}


@pytest.fixture(scope="module")
def offline_results():
    return _by_id(scan(load_news(None), fake=True, log=False))


# --- routing ------------------------------------------------------------------

def test_every_headline_reaches_a_terminal_status(offline_results):
    assert len(offline_results) == 20
    for r in offline_results.values():
        assert r["status"] in {"no_disruption", "no_exposure", "approved", "needs_review"}, r


def test_non_disruption_stops_after_scanner(offline_results):
    r = offline_results["N14"]  # football
    assert r["status"] == "no_disruption"
    assert [u["agent"] for u in r["usage"]] == ["scanner"]


def test_disruption_with_no_supplier_stops_after_mapper(offline_results):
    r = offline_results["N13"]  # Jakarta flood, no supplier nearby
    assert r["status"] == "no_exposure"
    assert [u["agent"] for u in r["usage"]] == ["scanner", "mapper"]


def test_full_path_runs_all_four_agents(offline_results):
    r = offline_results["N01"]
    assert [u["agent"] for u in r["usage"]] == ["scanner", "mapper", "impact", "advisor"]
    assert [s["supplier_id"] for s in r["suppliers"]] == ["S001"]


# --- scripted model for precise control ----------------------------------------

class ScriptedLLM:
    """Replays a fixed list of responses per agent."""
    model = "scripted"

    def __init__(self, script: dict[str, list[LLMResponse]]):
        self.script = {k: list(v) for k, v in script.items()}

    def complete(self, *, agent, system, messages, tools=None):
        return self.script[agent].pop(0)


def _text(obj) -> LLMResponse:
    return LLMResponse(json.dumps(obj), [])


def _tool(name, args, call_id="t1") -> LLMResponse:
    return LLMResponse("", [ToolCall(call_id, name, args)],
                       raw_content=[{"type": "tool_use", "id": call_id, "name": name, "input": args}])


SCAN_FIRE = _text({"is_disruption": True, "event_type": "fire", "places": ["Hitachinaka"]})
MAP_S001 = [_tool("find_suppliers_near_place", {"place": "Hitachinaka", "event_type": "fire"}),
            _text({"suppliers": [{"supplier_id": "S001", "why": "near"}]})]
IMPACT_S001 = [_text({"impacts": [{"supplier_id": "S001", "risk_level": "high", "min_days_of_cover": 6.0}]})]


def _advice(citation, backup="S011"):
    return [_tool("search_contracts", {"query": "second source", "supplier_id": "S001"}),
            _text({"advice": [{"supplier_id": "S001", "backup_supplier_id": backup, "coverage": "full",
                               "citations": [citation]}]})]


def _run(script):
    news = load_news(["N01"])[0]
    return run_news(build_graph(ScriptedLLM(script)), news)


def test_verifier_sends_advisor_back_once_then_approves():
    state = _run({"scanner": [SCAN_FIRE], "mapper": MAP_S001, "impact": IMPACT_S001,
                  "advisor": _advice("contract_S001.txt#99") + _advice("contract_S001.txt#2")})
    assert state["status"] == "approved"
    assert state["revisions"] == 1


def test_verifier_gives_up_after_max_revisions():
    state = _run({"scanner": [SCAN_FIRE], "mapper": MAP_S001, "impact": IMPACT_S001,
                  "advisor": _advice("made_up#1") + _advice("made_up#1")})
    assert state["status"] == "needs_review"
    assert any("never retrieved" in i for i in state["issues"])


def test_mapper_cannot_invent_suppliers():
    state = _run({"scanner": [SCAN_FIRE],
                  "mapper": [_text({"suppliers": [{"supplier_id": "S999", "why": "hallucinated"}]})]})
    assert state["status"] == "no_exposure"
    assert state["suppliers"] == []


def test_agent_failure_is_isolated_not_raised():
    state = _run({"scanner": [_text({"not": "the schema"})]})
    assert state["status"] == "error"
    assert state["error"].startswith("scanner:")


# --- tool loop ------------------------------------------------------------------

def test_unknown_tool_is_reported_to_the_model_not_raised():
    llm = ScriptedLLM({"mapper": [_tool("drop_database", {}), _text({"suppliers": []})]})
    run = agents.run_tool_agent(llm, agent="mapper", system="", payload={}, tools=[], schema=MapResult)
    assert run.tool_calls[0]["is_error"] is True


def test_tool_loop_has_a_turn_limit():
    endless = [_tool("list_known_locations", {}, f"t{i}") for i in range(20)]
    llm = ScriptedLLM({"mapper": endless})
    from agent.tools import MAPPER_TOOLS
    with pytest.raises(agents.AgentError, match="did not finish"):
        agents.run_tool_agent(llm, agent="mapper", system="", payload={}, tools=MAPPER_TOOLS, schema=MapResult)


# --- verifier unit checks -------------------------------------------------------

def test_verifier_rejects_backup_not_named_in_cited_clause():
    advice = AdviceResult.model_validate({"advice": [
        {"supplier_id": "S005", "backup_supplier_id": "S024", "coverage": "full", "citations": ["c1"]}]})
    issues = agents.verify(advice, ["S005"], {"c1": "The Supplier is the sole qualified source."})
    assert any("not named in any cited clause" in i for i in issues)


def test_verifier_rejects_missing_advice_and_uncited_claims():
    advice = AdviceResult.model_validate({"advice": [{"supplier_id": "S001", "coverage": "none"}]})
    issues = agents.verify(advice, ["S001", "S002"], {})
    assert any("S002: no advice" in i for i in issues)
    assert any("no citation" in i for i in issues)
