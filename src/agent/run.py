"""Run the morning scan.

    uv run python -m agent.run --fake            # offline, free, rule-based stand-in
    uv run python -m agent.run                    # Gemini free tier (needs GEMINI_API_KEY)
    SDA_PROVIDER=ollama uv run python -m agent.run   # local model, free, offline
    uv run python -m agent.run --news N01 N07     # only some headlines
    uv run python -m agent.run --live             # today's real headlines from GDELT (not scored)
    uv run python -m agent.run --resume           # finish the latest run: redo only errored/missing headlines
    uv run python -m agent.run --sequence --fake  # scored event-memory test: repeats, escalations, a gap

Event memory (agent/events.py) is on for --live, --sequence and --memory: a
known event with nothing new is logged as "ongoing" and not alerted again.
Scored replays of data/news.json run without it, so every headline is judged alone.

If the call cap or the provider's daily quota is hit, the run stops at once and
the unfinished headlines are not logged; `--resume` picks them up later under
the same run id, so `agent.evaluate` scores one complete run.

Every run appends one line per headline to logs/runs.jsonl (inputs, decisions,
tokens, cost, time), so any alert can be traced back afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone

from agent import config
from agent.graph import build_graph, run_news
from agent.llm import CapExceeded, QuotaExhausted, make_llm


def load_news(ids: list[str] | None) -> list[dict]:
    news = json.loads((config.ROOT / "data" / "news.json").read_text())
    return [n for n in news if not ids or n["news_id"] in ids]


def unfinished(run_id: str | None = None) -> tuple[str, list[str]]:
    """Latest (or given) run id, and the headlines it has not finished: missing
    from the log, or whose last attempt ended in error."""
    if not config.LOG_PATH.exists():
        raise SystemExit("No runs yet: nothing to resume.")
    rows = [json.loads(line) for line in config.LOG_PATH.read_text().splitlines() if line.strip()]
    run_id = run_id or rows[-1]["run_id"]
    last = {r["news_id"]: r for r in rows if r["run_id"] == run_id}
    if not last:
        raise SystemExit(f"run_id {run_id} not found in {config.LOG_PATH}")
    if any(r.get("kind") == "live" for r in last.values()):
        raise SystemExit("Live runs cannot be resumed (the headlines were today's).")
    todo = [n["news_id"] for n in load_news(None)
            if n["news_id"] not in last or last[n["news_id"]]["status"] == "error"]
    return run_id, todo


SEQUENCE_PATH = config.ROOT / "eval" / "event_sequence.json"


def load_sequence() -> list[dict]:
    return json.loads(SEQUENCE_PATH.read_text())["news"]


def scan(news_items: list[dict], fake: bool, log: bool = True, progress: bool = False,
         run_id: str | None = None, memory=None) -> list[dict]:
    llm = make_llm("fake" if fake else None)
    graph = build_graph(llm, memory)
    run_id = run_id or uuid.uuid4().hex[:8]
    results = []
    if progress:
        print(f"Run {run_id}: {len(news_items)} headlines with {llm.model} "
              f"(cap {llm.max_calls} model calls). Progress prints as each headline finishes.", flush=True)
    for n, item in enumerate(news_items, 1):
        t0 = time.perf_counter()
        try:
            state = run_news(graph, item)
        except (CapExceeded, QuotaExhausted) as e:
            left = len(news_items) - n + 1
            print(f"\nStopped: {e}. {left} headline(s) not run and not logged.\n"
                  f"Finish later with:  uv run python -m agent.run --resume", flush=True)
            break
        event = state.get("event") or {}
        if memory is not None and event and state.get("status") != "error":
            memory.commit(event, state["scan"], [x["supplier_id"] for x in state.get("suppliers", [])], item)
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
            "event_id": event.get("event_id"),
            "event_status": event.get("event_status"),
            "event_new_suppliers": event.get("new_suppliers", []),
            "event_matched_on": event.get("matched_on"),
            "model_calls": sum(u.get("model_calls", 0) for u in state.get("usage", [])),
            "scan": state.get("scan"),
            "suppliers": state.get("suppliers", []),
            "impact": state.get("impact"),
            "impact_disagreements": state.get("impact_disagreements", []),
            "advice": state.get("advice"),
            "issues": state.get("issues", []),
            "backup_warnings": state.get("backup_warnings", []),
            "revisions": state.get("revisions", 0),
            "usage": state.get("usage", []),
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cost_usd": round(config.cost_usd(llm.model, tokens_in, tokens_out), 6),
            "seconds": round(time.perf_counter() - t0, 2),
        }
        results.append(record)
        if progress:
            tag = f" [{record['event_status']} {record['event_id']}]" if record["event_status"] else ""
            print(f"  [{n}/{len(news_items)}] {item['news_id']} {record['status']:<13}{tag} "
                  f"{record['seconds']:>5.1f}s  {len(record['suppliers'])} suppliers  "
                  f"(model calls so far: {llm.calls})", flush=True)
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
        if r["status"] == "ongoing":
            print(f"[{r['news_id']}] ongoing       {r['event_id']} ({r['event_matched_on']}) {r['headline']}")
            continue
        label = f"  {r['event_status'].upper()} {r['event_id']}" if r.get("event_status") else ""
        print(f"\n[{r['news_id']}] {r['status'].upper()}{label}  {r['headline']}")
        if r.get("event_status") == "escalated":
            print(f"    ↑ escalated: {r['event_matched_on']}")
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
        for w in r.get("backup_warnings", []):
            print(f"    ⚠ {w['warning']}")
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
    ap.add_argument("--resume", nargs="?", const="latest", metavar="RUN_ID",
                    help="redo only the errored or missing headlines of the latest (or given) run")
    ap.add_argument("--sequence", action="store_true",
                    help="run the scored event-memory sequence (eval/event_sequence.json) with fresh memory")
    ap.add_argument("--memory", action="store_true",
                    help="use persistent event memory for a replay too (on by default for --live)")
    args = ap.parse_args()
    run_id = None
    memory = None
    if args.sequence:
        from agent.events import EventMemory, MemoryStore
        memory = EventMemory(MemoryStore())
    elif args.live or args.memory:
        from agent.events import EventMemory
        memory = EventMemory()
        print(f"Event memory: {len(memory.open_events())} open event(s) loaded.")
    if args.sequence:
        news = load_sequence()
    elif args.resume:
        run_id, todo = unfinished(None if args.resume == "latest" else args.resume)
        if not todo:
            print(f"Run {run_id} is already complete. Score it with: uv run python -m agent.evaluate")
            return
        print(f"Resuming run {run_id}: {len(todo)} headline(s) left: {' '.join(todo)}")
        news = load_news(todo)
    elif args.live:
        from agent.live import fetch_live_news
        news = fetch_live_news(limit=args.max_live)
        print(f"Fetched {len(news)} live headlines (saved to data/live/)")
    else:
        news = load_news(args.news)
    results = scan(news, args.fake, progress=True, run_id=run_id, memory=memory)
    if memory is not None:
        dates = [n.get("event_date", "") for n in news if n.get("event_date")]
        closed = memory.sweep(max(dates) if dates else None)
        memory.save()
        print(f"Event memory: {len(memory.open_events())} open, {len(closed)} closed this run"
              + (f" ({', '.join(closed)})" if closed else "") + ".")
    if os.getenv("SDA_BQ_TABLE"):
        from agent.sinks import write_bigquery
        n = write_bigquery(results)  # raises on failure, so a cloud job shows as failed
        print(f"Wrote {n} rows to BigQuery table {os.getenv('SDA_BQ_TABLE')}")
    print("\n===== Alerts =====")
    print_alerts(results)
    if args.live:
        hits = [r for r in results if r["status"] not in ("no_disruption", "no_exposure")]
        quiet = sum(r["status"] == "ongoing" for r in results)
        print(f"Live summary: {len(hits)} of {len(results)} headlines touch our suppliers; "
              f"{len(hits) - quiet} alerted, {quiet} already-known event(s) not re-alerted.")


if __name__ == "__main__":
    main()
