"""Run the morning scan.

    uv run python -m agent.run --fake            # offline, free, rule-based stand-in
    uv run python -m agent.run                    # Gemini free tier (needs GEMINI_API_KEY)
    SDA_PROVIDER=ollama uv run python -m agent.run   # local model, free, offline
    uv run python -m agent.run --news N01 N07     # only some headlines
    uv run python -m agent.run --live             # today's real headlines from GDELT (not scored)

Every run appends one line per headline to logs/runs.jsonl (inputs, decisions,
tokens, cost, time), so any alert can be traced back afterwards.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone

from agent import config
from agent.graph import build_graph, run_news
from agent.llm import make_llm


def load_news(ids: list[str] | None) -> list[dict]:
    news = json.loads((config.ROOT / "data" / "news.json").read_text())
    return [n for n in news if not ids or n["news_id"] in ids]


def scan(news_items: list[dict], fake: bool, log: bool = True) -> list[dict]:
    llm = make_llm("fake" if fake else None)
    graph = build_graph(llm)
    run_id = uuid.uuid4().hex[:8]
    results = []
    for item in news_items:
        t0 = time.perf_counter()
        state = run_news(graph, item)
        tokens_in = sum(u.get("input_tokens", 0) for u in state.get("usage", []))
        tokens_out = sum(u.get("output_tokens", 0) for u in state.get("usage", []))
        record = {
            "run_id": run_id,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provider": "fake" if fake else config.PROVIDER,
            "model": llm.model,
            "prompt_version": "v1",
            "news_id": item["news_id"],
            "headline": item["headline"],
            "kind": item.get("kind"),
            "url": item.get("url"),
            "status": state.get("status"),
            "error": state.get("error"),
            "scan": state.get("scan"),
            "suppliers": state.get("suppliers", []),
            "impact": state.get("impact"),
            "impact_disagreements": state.get("impact_disagreements", []),
            "advice": state.get("advice"),
            "issues": state.get("issues", []),
            "revisions": state.get("revisions", 0),
            "usage": state.get("usage", []),
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cost_usd": round(config.cost_usd(llm.model, tokens_in, tokens_out), 6),
            "seconds": round(time.perf_counter() - t0, 2),
        }
        results.append(record)
        if log:
            config.LOG_PATH.parent.mkdir(exist_ok=True)
            with config.LOG_PATH.open("a") as f:
                f.write(json.dumps(record, default=str) + "\n")
    return results


def print_alerts(results: list[dict]) -> None:
    for r in results:
        if r["status"] in ("no_disruption", "no_exposure"):
            print(f"[{r['news_id']}] {r['status']:<13} {r['headline']}")
            continue
        print(f"\n[{r['news_id']}] {r['status'].upper()}  {r['headline']}")
        if r["error"]:
            print(f"    error: {r['error']}")
            continue
        impacts = {i["supplier_id"]: i for i in (r["impact"] or {}).get("impacts", [])}
        advice = {a["supplier_id"]: a for a in (r["advice"] or {}).get("advice", [])}
        for s in r["suppliers"]:
            i = impacts.get(s["supplier_id"], {})
            a = advice.get(s["supplier_id"], {})
            print(f"    {s['supplier_id']} {s['name']:<32} risk={i.get('risk_level', '?'):<6} "
                  f"min cover={i.get('min_days_of_cover', '?')}d  backup={a.get('backup_supplier_id')} "
                  f"({a.get('coverage', '?')})  cites={','.join(a.get('citations', []))}")
        for issue in r["issues"]:
            print(f"    ! {issue}")
        for d in r.get("impact_disagreements", []):
            print(f"    ~ impact rating corrected by rule: {d}")
    total = sum(r["cost_usd"] for r in results)
    tokens = sum(r["input_tokens"] + r["output_tokens"] for r in results)
    print(f"\n{len(results)} headlines, {tokens} tokens, cost ${total:.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="offline rule-based model, no API key, no cost")
    ap.add_argument("--news", nargs="*", help="news ids to run (default: all)")
    ap.add_argument("--live", action="store_true", help="read the last 24h of real headlines from GDELT instead")
    ap.add_argument("--max-live", type=int, default=15, help="cap on live headlines per run (protects the free quota)")
    args = ap.parse_args()
    if args.live:
        from agent.live import fetch_live_news
        news = fetch_live_news(limit=args.max_live)
        print(f"Fetched {len(news)} live headlines (saved to data/live/)")
    else:
        news = load_news(args.news)
    results = scan(news, args.fake)
    print_alerts(results)
    if args.live:
        hits = [r for r in results if r["status"] not in ("no_disruption", "no_exposure")]
        print(f"Live summary: {len(hits)} of {len(results)} headlines touch our suppliers.")


if __name__ == "__main__":
    main()
