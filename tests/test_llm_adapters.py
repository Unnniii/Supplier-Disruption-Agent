"""Adapters and the cap, tested without any network or key.

The Gemini and Ollama adapters are driven by stand-in clients so we can check
the translation both ways: our neutral messages -> their API, their reply -> ours.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from agent import agents
from agent.fake import FakeLLM
from agent.llm import CapExceeded, CappedLLM, GeminiLLM, OllamaLLM
from agent.run import load_news, scan
from agent.schemas import MapResult
from agent.tools import MAPPER_TOOLS

pytest.importorskip("google.genai")
from google.genai import types  # noqa: E402


# --- the cap --------------------------------------------------------------------

def test_cap_stops_calls_after_limit():
    llm = CappedLLM(FakeLLM(), max_calls=2)
    msg = [{"role": "user", "content": '<input>\n{"headline": "x", "body": "y", "event_date": "z"}\n</input>'}]
    llm.complete(agent="scanner", system="", messages=msg)
    llm.complete(agent="scanner", system="", messages=msg)
    with pytest.raises(CapExceeded):
        llm.complete(agent="scanner", system="", messages=msg)


def test_cap_hit_mid_batch_stops_without_logging_unfinished(monkeypatch):
    monkeypatch.setattr("agent.config.MAX_LLM_CALLS_PER_RUN", 30)
    results = scan(load_news(None), fake=True, log=False)
    assert 0 < len(results) < len(load_news(None))       # stopped early
    assert not any(r["status"] == "error" for r in results)  # unfinished ones are not scored as wrong


def test_resume_reruns_only_unfinished_headlines_under_same_run_id(tmp_path, monkeypatch):
    from agent import run as run_mod
    monkeypatch.setattr("agent.config.LOG_PATH", tmp_path / "runs.jsonl")
    first = scan(load_news(["N01", "N02", "N03"]), fake=True)
    rid = first[0]["run_id"]
    # pretend N02 errored and N04.. were never reached
    rows = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    rows[1]["status"] = "error"
    (tmp_path / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    got_id, todo = run_mod.unfinished()
    assert got_id == rid
    assert "N02" in todo and "N01" not in todo and "N03" not in todo and "N04" in todo
    scan(load_news(["N02"]), fake=True, run_id=rid)
    from agent.evaluate import load_run
    merged = {r["news_id"]: r for r in load_run(rid)}
    assert merged["N02"]["status"] != "error"            # last attempt wins


# --- Gemini ---------------------------------------------------------------------

class FakeGeminiClient:
    """Records requests; replies with a scripted list of Gemini Content objects."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        content = self.replies.pop(0)
        return NS(candidates=[NS(content=content)],
                  usage_metadata=NS(prompt_token_count=100, candidates_token_count=20, thoughts_token_count=5))


def _call_turn():
    return types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(id="call-1", name="find_suppliers_near_place",
                                         args={"place": "Hitachinaka", "event_type": "fire"}),
        thought_signature=b"sig-123")])


def _answer_turn():
    return types.Content(role="model", parts=[types.Part.from_text(
        text=json.dumps({"suppliers": [{"supplier_id": "S001", "why": "within 10 km"}]}))])


def test_gemini_tool_loop_round_trip():
    client = FakeGeminiClient([_call_turn(), _answer_turn()])
    llm = GeminiLLM(model="gemini-test", client=client)
    run = agents.run_tool_agent(llm, agent="mapper", system="sys", payload={"places": ["Hitachinaka"]},
                                tools=MAPPER_TOOLS, schema=MapResult)
    assert [s.supplier_id for s in run.output.suppliers] == ["S001"]
    assert run.tool_calls[0]["tool"] == "find_suppliers_near_place" and not run.tool_calls[0]["is_error"]
    assert run.input_tokens == 200 and run.output_tokens == 50  # thinking tokens counted as output

    second = client.requests[1]["contents"]
    # model turn sent back unchanged, thought signature intact
    assert second[1].parts[0].thought_signature == b"sig-123"
    # tool result labelled with the tool's name
    fr = second[2].parts[0].function_response
    assert fr.name == "find_suppliers_near_place"
    assert "S001" in fr.response["result"]


def test_gemini_tool_without_parameters_is_declared_without_schema():
    tools = GeminiLLM._declarations([t.spec() for t in MAPPER_TOOLS])
    by_name = {d.name: d for d in tools[0].function_declarations}
    assert by_name["list_known_locations"].parameters_json_schema is None
    assert by_name["find_suppliers_on_route"].parameters_json_schema["required"] == ["route"]


def test_gemini_disables_automatic_function_calling():
    client = FakeGeminiClient([_answer_turn()])
    GeminiLLM(model="m", client=client).complete(agent="mapper", system="s",
                                                 messages=[{"role": "user", "content": "hi"}], tools=None)
    assert client.requests[0]["config"].automatic_function_calling.disable is True


def test_rate_limit_is_retried(monkeypatch):
    monkeypatch.setattr("agent.llm.time.sleep", lambda s: None)
    calls = {"n": 0}

    class Flaky(FakeGeminiClient):
        def generate_content(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return super().generate_content(**kw)

    resp = GeminiLLM(model="m", client=Flaky([_answer_turn()])).complete(
        agent="mapper", system="s", messages=[{"role": "user", "content": "hi"}])
    assert calls["n"] == 2 and "S001" in resp.text


def test_daily_quota_stops_at_once_without_retrying(monkeypatch):
    from agent.llm import QuotaExhausted
    slept = []
    monkeypatch.setattr("agent.llm.time.sleep", slept.append)

    class Daily(FakeGeminiClient):
        def generate_content(self, **kw):
            raise RuntimeError("429 RESOURCE_EXHAUSTED. Quota exceeded ... quotaId: "
                               "GenerateRequestsPerDayPerProjectPerModel-FreeTier ... Please retry in 4h44m25s.")

    with pytest.raises(QuotaExhausted, match="4h44m25s"):
        GeminiLLM(model="m", client=Daily([])).complete(agent="mapper", system="s",
                                                        messages=[{"role": "user", "content": "hi"}])
    assert slept == []                                    # no wasted waiting


def test_503_overload_is_retried(monkeypatch):
    monkeypatch.setattr("agent.llm.time.sleep", lambda s: None)
    calls = {"n": 0}

    class Busy(FakeGeminiClient):
        def generate_content(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("503 UNAVAILABLE. The model is overloaded.")
            return super().generate_content(**kw)

    resp = GeminiLLM(model="m", client=Busy([_answer_turn()])).complete(
        agent="mapper", system="s", messages=[{"role": "user", "content": "hi"}])
    assert calls["n"] == 2 and "S001" in resp.text


def test_daily_quota_mid_batch_stops_the_run(monkeypatch):
    from agent.llm import QuotaExhausted
    n = {"calls": 0}
    real = FakeLLM.complete

    def flaky(self, **kw):
        n["calls"] += 1
        if n["calls"] > 30:
            raise QuotaExhausted("daily free-tier quota for this model is used up")
        return real(self, **kw)

    monkeypatch.setattr(FakeLLM, "complete", flaky)
    results = scan(load_news(None), fake=True, log=False)
    assert 0 < len(results) < len(load_news(None))
    assert not any(r["status"] == "error" for r in results)


# --- Ollama ---------------------------------------------------------------------

def test_ollama_translation_and_tool_calls():
    sent = []

    def post(path, body):
        sent.append(body)
        if len(sent) == 1:
            return {"message": {"content": "", "tool_calls": [
                {"function": {"name": "find_suppliers_on_route", "arguments": {"route": "Suez Canal"}}}]},
                "prompt_eval_count": 50, "eval_count": 10}
        return {"message": {"content": json.dumps({"suppliers": []})}, "prompt_eval_count": 60, "eval_count": 5}

    llm = OllamaLLM(model="local", post=post)
    run = agents.run_tool_agent(llm, agent="mapper", system="sys", payload={}, tools=MAPPER_TOOLS, schema=MapResult)
    assert run.tool_calls[0]["args"] == {"route": "Suez Canal"}
    roles = [m["role"] for m in sent[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert sent[1]["messages"][3]["tool_name"] == "find_suppliers_on_route"
    assert run.input_tokens == 110


def test_claude_adapter_strips_private_blocks():
    # Private blocks from another adapter must never reach the Anthropic API.
    from agent.llm import ClaudeLLM
    llm = ClaudeLLM.__new__(ClaudeLLM)
    llm.model = "m"
    captured = {}

    class Msgs:
        def create(self, **kw):
            captured.update(kw)
            return NS(content=[NS(type="text", text="{}")], usage=NS(input_tokens=1, output_tokens=1))

    llm._client = NS(messages=Msgs())
    llm.complete(agent="a", system="s", messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "t", "input": {}},
                                          {"type": "_gemini_content", "content": object()}]}])
    assert all(not b["type"].startswith("_") for b in captured["messages"][1]["content"])


# --- fixes from the first real Gemini run (2026-10-09) ---------------------------

def test_invalid_json_gets_one_repair_turn():
    from tests.test_agents_graph import ScriptedLLM, _text  # noqa: F401
    bad = LLMResponseText('{"suppliers": [ {"supplier_id": "S001" "why": "x"} ]}')
    good = LLMResponseText('{"suppliers": [{"supplier_id": "S001", "why": "x"}]}')
    llm = ScriptedLLM({"mapper": [bad, good]})
    run = agents.run_tool_agent(llm, agent="mapper", system="", payload={}, tools=[], schema=MapResult)
    assert run.repairs == 1 and run.output.suppliers[0].supplier_id == "S001"


def test_second_invalid_json_fails_the_agent():
    from tests.test_agents_graph import ScriptedLLM
    bad = LLMResponseText("{not json")
    llm = ScriptedLLM({"mapper": [bad, bad]})
    with pytest.raises(agents.AgentError):
        agents.run_tool_agent(llm, agent="mapper", system="", payload={}, tools=[], schema=MapResult)


def LLMResponseText(text):
    from agent.llm import LLMResponse
    return LLMResponse(text, [], raw_content=[{"type": "text", "text": text}])


def test_impact_rating_is_checked_against_the_rule():
    from agent.schemas import ImpactResult
    rows = __import__("agent.erp", fromlist=["x"]).parts_at_risk("S001")
    rule_level, rule_min = agents.rule_risk(rows)
    wrong = "low" if rule_level != "low" else "high"
    checked, disagreements = agents.check_impact(ImpactResult.model_validate(
        {"impacts": [{"supplier_id": "S001", "risk_level": wrong, "min_days_of_cover": 999, "driver_part_ids": []}]}))
    i = checked["impacts"][0]
    assert i["risk_level"] == rule_level and i["agent_risk_level"] == wrong
    assert i["min_days_of_cover"] == rule_min       # numbers come from the ERP, not the model
    assert i["parts"]                               # details attached by code
    assert disagreements and "S001" in disagreements[0]


def test_gemini_embeddings_one_vector_per_text():
    from agent.rag import GeminiEmbedder

    class OneVectorPerCall:
        """Mimics the real failure: a list of texts came back as ONE vector."""
        def __init__(self):
            self.models = self

        def embed_content(self, *, model, contents, config):
            return NS(embeddings=[NS(values=[float(len(contents))])])

    vecs = GeminiEmbedder(client=OneVectorPerCall()).documents(["a", "b", "c"])
    assert len(vecs) == 3
