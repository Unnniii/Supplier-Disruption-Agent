"""Behaviour of the multi-agent graph, offline and free.

Covers: routing (early exits), the tool-use loop's failure handling, the
verifier's checks, the advisor revise loop, and error isolation.
"""

from __future__ import annotations

import json

import re

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
    assert len(offline_results) == len(load_news(None))
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


# --- live news and evaluation ----------------------------------------------------

def test_live_news_parsing_dedupes_and_caps(tmp_path, monkeypatch):
    import json as _json
    from agent import live
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path)
    articles = {"articles": [
        {"title": "Fire halts production at chemical plant near Ludwigshafen", "seendate": "20261010T061500Z",
         "domain": "example.com", "sourcecountry": "Germany", "url": "https://example.com/a"},
        {"title": "Fire halts production at chemical plant near Ludwigshafen!", "seendate": "20261010T062000Z",
         "domain": "other.com", "sourcecountry": "Germany", "url": "https://other.com/a"},
        {"title": "Short", "seendate": "20261010T000000Z"},
        {"title": "Typhoon forces closure of factories in southern Taiwan", "seendate": "20261010T070000Z",
         "domain": "news.tw", "sourcecountry": "Taiwan", "url": "https://news.tw/b"},
    ]}
    news = live.fetch_live_news(limit=5, get=lambda params: _json.dumps(articles))
    assert [n["headline"][:4] for n in news] == ["Fire", "Typh"]   # duplicate and short title dropped
    assert news[0]["kind"] == "live" and news[0]["event_date"] == "2026-10-10"
    assert list(tmp_path.glob("*.json"))                            # saved for audit


def test_live_news_rejects_non_json_reply():
    from agent import live
    with pytest.raises(RuntimeError, match="did not return JSON"):
        live.parse_articles("Please limit requests to one every 5 seconds")


def test_evaluation_scores_a_known_run():
    from agent.evaluate import score_run
    truth = {"news": {
        "A": {"is_disruption": True, "expected_suppliers": ["S1", "S2"], "acceptable_extra": ["S9"]},
        "B": {"is_disruption": False, "expected_suppliers": [], "acceptable_extra": []},
    }, "backups": {"S1": {"backup": "S5", "coverage": "full"}}}
    records = [
        {"news_id": "A", "headline": "a", "status": "approved", "scan": {"is_disruption": True},
         "suppliers": [{"supplier_id": "S1"}, {"supplier_id": "S9"}, {"supplier_id": "S7"}],
         "advice": {"advice": [{"supplier_id": "S1", "backup_supplier_id": "S5", "coverage": "partial"}]}},
        {"news_id": "B", "headline": "b", "status": "no_disruption", "scan": {"is_disruption": False}, "suppliers": []},
        {"news_id": "LIVE-1", "headline": "not in key", "status": "approved", "suppliers": [{"supplier_id": "S1"}]},
    ]
    s = score_run(records, truth)
    assert s["headlines_scored"] == 2                   # live headline ignored
    assert (s["true_positives"], s["false_positives"], s["false_negatives"]) == (1, 1, 1)  # S9 is an allowed extra
    assert s["scanner_accuracy"] == 1.0
    assert s["backup_accuracy"] == 1.0 and s["coverage_accuracy"] == 0.0
    assert s["exact_headlines"] == 1


def test_evaluation_retrieval_metrics():
    from agent.evaluate import score_retrieval
    from agent.rag import KeywordRetriever
    import json as _json
    from agent import config as _config
    qs = _json.loads((_config.ROOT / "eval" / "rag_questions.json").read_text())
    r = score_retrieval(qs, KeywordRetriever())
    assert r["questions"] == len(qs) and 0 <= r["hit_at_1"] <= r["hit_at_4"] <= 1


def test_live_news_falls_back_to_simpler_query(tmp_path, monkeypatch):
    import json as _json
    from agent import live
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path)
    seen = []

    def get(params):
        seen.append(params["query"])
        if "sourcelang" in params["query"]:
            return "Invalid query"
        return _json.dumps({"articles": [{"title": "Port strike halts container handling at Antwerp",
                                          "seendate": "20261010T080000Z"}]})

    news = live.fetch_live_news(limit=3, get=get, sleep=lambda s: None)
    assert len(seen) == 2 and len(news) == 1


def test_backup_hit_by_the_same_event_is_warned():
    """Hualien earthquake: Hsinchu's contractual backup Taichung is in the same quake."""
    news = load_news(["N27"])[0]
    scan = _text({"is_disruption": True, "event_type": "earthquake", "places": ["Hualien"]})
    mapper = [_tool("find_suppliers_near_place", {"place": "Hualien", "event_type": "earthquake"}),
              _text({"suppliers": [{"supplier_id": "S038", "why": "quake"}, {"supplier_id": "S011", "why": "quake"}]})]
    impact = [_text({"impacts": [{"supplier_id": "S038", "risk_level": "medium", "min_days_of_cover": 9},
                                 {"supplier_id": "S011", "risk_level": "medium", "min_days_of_cover": 9}]})]
    advisor = [_tool("search_contracts", {"query": "second source", "supplier_id": "S038"}),
               _text({"advice": [{"supplier_id": "S038", "backup_supplier_id": "S011", "coverage": "full",
                                  "citations": ["contract_S038.txt#1"]},
                                 {"supplier_id": "S011", "coverage": "unknown"}]})]
    state = run_news(build_graph(ScriptedLLM({"scanner": [scan], "mapper": mapper, "impact": impact, "advisor": advisor})), news)
    assert state["status"] == "approved"
    assert [(w["supplier_id"], w["backup_supplier_id"]) for w in state["backup_warnings"]] == [("S038", "S011")]


def test_evaluation_counts_unwarned_correlated_backups():
    from agent.evaluate import score_run
    truth = {"news": {"A": {"is_disruption": True, "expected_suppliers": ["S1", "S2"], "acceptable_extra": []}}, "backups": {}}
    rec = {"news_id": "A", "headline": "a", "status": "approved", "scan": {"is_disruption": True},
           "suppliers": [{"supplier_id": "S1"}, {"supplier_id": "S2"}],
           "advice": {"advice": [{"supplier_id": "S1", "backup_supplier_id": "S2", "coverage": "full"}]}}
    before = score_run([rec], truth)
    after = score_run([{**rec, "backup_warnings": [{"supplier_id": "S1", "backup_supplier_id": "S2"}]}], truth)
    assert (before["backups_hit_by_same_event"], before["backups_hit_by_same_event_unwarned"]) == (1, 1)
    assert (after["backups_hit_by_same_event"], after["backups_hit_by_same_event_unwarned"]) == (1, 0)


GOOGLE_NEWS_SAMPLE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<generator>NFE/5.0</generator><title>"port strike" when:1d - Google News</title>
<item><title>Dockworkers strike halts container traffic at Rotterdam - Reuters</title>
<link>https://news.google.com/rss/articles/CBMiabc?oc=5</link><guid isPermaLink="false">CBMiabc</guid>
<pubDate>Mon, 12 Oct 2026 08:15:00 GMT</pubDate><description>&lt;a href="x"&gt;...&lt;/a&gt;</description>
<source url="https://www.reuters.com">Reuters</source></item>
<item><title>Flooding forces closure of electronics plants near Penang - The Star</title>
<link>https://news.google.com/rss/articles/CBMidef?oc=5</link>
<pubDate>Mon, 12 Oct 2026 06:00:00 GMT</pubDate><source url="https://www.thestar.com.my">The Star</source></item>
</channel></rss>"""


def test_gdelt_rate_limit_falls_back_to_google_news(tmp_path, monkeypatch):
    from agent import live
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path)
    calls = []

    def gdelt(params):
        calls.append(params["query"])
        raise RuntimeError("Client error '429 Too Many Requests'")

    def feeds_down(url):
        raise RuntimeError("503 Service Unavailable")

    news = live.fetch_live_news(limit=5, get=gdelt, sleep=lambda s: None, rss_get=lambda: GOOGLE_NEWS_SAMPLE,
                                feed_get=feeds_down)
    assert len(calls) == 2                                  # both GDELT queries tried first
    assert [n["source"] for n in news] == ["google_news", "google_news"]
    assert news[0]["headline"] == "Dockworkers strike halts container traffic at Rotterdam"  # publisher suffix removed
    assert news[0]["event_date"] == "2026-10-12" and "www.reuters.com" in news[0]["body"]


def test_live_fetch_fails_clearly_when_every_source_fails(tmp_path, monkeypatch):
    from agent import live
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path)

    def down(*a):
        raise RuntimeError("429 Too Many Requests")

    with pytest.raises(RuntimeError, match="every source.*gdelt.*feeds.*google_news"):
        live.fetch_live_news(limit=5, get=down, sleep=lambda s: None, rss_get=lambda: "<html>blocked</html",
                             feed_get=down)


def test_gdelt_success_does_not_touch_google_news(tmp_path, monkeypatch):
    import json as _json
    from agent import live
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path)

    def never():
        raise AssertionError("RSS should not be called")

    ok = _json.dumps({"articles": [{"title": "Port strike halts container handling at Antwerp", "seendate": "20261012T080000Z"}]})
    news = live.fetch_live_news(limit=3, get=lambda p: ok, rss_get=never, feed_get=never)
    assert news[0]["source"] == "gdelt"


def test_live_source_switch_goes_straight_to_google_news(tmp_path, monkeypatch):
    from agent import live
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path)
    monkeypatch.setenv("SDA_LIVE_SOURCE", "google_news")

    def never(params):
        raise AssertionError("GDELT should be skipped")

    news = live.fetch_live_news(limit=1, get=never, rss_get=lambda: GOOGLE_NEWS_SAMPLE)
    assert len(news) == 1 and news[0]["source"] == "google_news"


BBC_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>BBC News</title>
<item><title>Strike shuts Antwerp port terminals for second day</title><link>https://www.bbc.co.uk/news/a1</link>
<pubDate>Mon, 12 Oct 2026 07:00:00 GMT</pubDate></item>
<item><title>Bank of England holds interest rates</title><link>https://www.bbc.co.uk/news/a2</link>
<pubDate>Mon, 12 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Fire at chip plant halts output</title><link>https://www.bbc.co.uk/news/a3</link>
<pubDate>Wed, 07 Oct 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

LOADSTAR_SAMPLE = """<?xml version="1.0"?><rss version="2.0"><channel><title>The Loadstar</title>
<item><title>Typhoon forces closure of Kaohsiung container terminals</title><link>https://theloadstar.com/x</link>
<pubDate>Mon, 12 Oct 2026 09:30:00 +0000</pubDate></item></channel></rss>"""


def test_publisher_feeds_filter_recent_disruptions_and_survive_one_feed_failing():
    from datetime import datetime, timezone
    from agent import live

    def feed_get(url):
        if "bbci" in url and "business" in url:
            return BBC_SAMPLE
        if "theloadstar" in url:
            return LOADSTAR_SAMPLE
        raise RuntimeError("403 Forbidden")              # the other feeds fail

    got = live.fetch_feeds(feed_get, now=datetime(2026, 10, 12, 12, tzinfo=timezone.utc))
    titles = [a["title"] for a in got]
    assert titles == ["Typhoon forces closure of Kaohsiung container terminals",   # newest first
                      "Strike shuts Antwerp port terminals for second day"]
    # "Bank of England" dropped (no disruption word; "ban" must not match "Bank"),
    # the chip-plant fire dropped (older than 36 hours)
    assert got[0]["domain"] == "theloadstar.com"


def test_feeds_are_used_when_gdelt_refuses(tmp_path, monkeypatch):
    from agent import live
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path)

    def gdelt(params):
        raise RuntimeError("429 Too Many Requests")

    def never():
        raise AssertionError("Google News should not be needed")

    news = live.fetch_live_news(limit=5, get=gdelt, sleep=lambda s: None, rss_get=never,
                                feed_get=lambda url: re.sub(r"<pubDate>.*?</pubDate>", "", LOADSTAR_SAMPLE))
    assert news and {n["source"] for n in news} == {"feeds"}     # undated items are kept, so no clock dependence


def test_empty_gdelt_answer_falls_through_to_feeds(tmp_path, monkeypatch):
    import json as _json
    from agent import live
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path)
    feed = re.sub(r"<pubDate>.*?</pubDate>", "", LOADSTAR_SAMPLE)
    news = live.fetch_live_news(limit=5, get=lambda p: _json.dumps({"articles": []}), sleep=lambda s: None,
                                feed_get=lambda url: feed, rss_get=lambda: GOOGLE_NEWS_SAMPLE)
    assert news and news[0]["source"] == "feeds"        # 200 OK with no articles is not trusted


def test_every_source_empty_is_a_quiet_day_not_a_crash(tmp_path, monkeypatch):
    import json as _json
    from agent import live
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path)
    empty_rss = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    news = live.fetch_live_news(limit=5, get=lambda p: _json.dumps({}), sleep=lambda s: None,
                                feed_get=lambda url: empty_rss, rss_get=lambda: empty_rss)
    assert news == []
