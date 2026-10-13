"""Score a run against the hand-written answer key.

    uv run python -m agent.evaluate              # score the latest run in logs/runs.jsonl
    uv run python -m agent.evaluate --run-id ab12cd34
    uv run python -m agent.evaluate --no-rag     # skip the retrieval check (saves embedding calls)

If the latest run is the event-memory sequence (agent.run --sequence), it is
scored on event decisions instead: new / ongoing / escalated per headline,
whether reports of one real event got one event id, and alerts avoided.

What is scored, and how:
  Scanner     is_disruption matches the key                       -> accuracy
  Mapper      flagged suppliers vs expected                       -> precision, recall, F1
              (acceptable_extra are neither rewarded nor punished)
  Advisor     backup supplier and coverage vs the contracts       -> accuracy
  Retrieval   right contract in the top 1 / top 4 for 14 questions -> hit@1, hit@4, MRR
  Pipeline    verifier outcomes, rule corrections, repairs, errors, tokens, cost, time

Writes eval/results.md (latest, human-readable) and appends one line to
eval/history.jsonl so before/after comparisons are a diff, not a memory.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from agent import config

TRUTH_PATH = config.ROOT / "eval" / "ground_truth.json"
RAG_PATH = config.ROOT / "eval" / "rag_questions.json"
RESULTS_MD = config.ROOT / "eval" / "results.md"
SEQUENCE_PATH = config.ROOT / "eval" / "event_sequence.json"
EVENTS_MD = config.ROOT / "eval" / "results_events.md"
HISTORY = config.ROOT / "eval" / "history.jsonl"


def load_run(run_id: str | None = None) -> list[dict]:
    if not config.LOG_PATH.exists():
        raise SystemExit("No runs yet: run `uv run python -m agent.run` first.")
    rows = [json.loads(line) for line in config.LOG_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    run_id = run_id or rows[-1]["run_id"]
    picked = [r for r in rows if r["run_id"] == run_id]
    if not picked:
        raise SystemExit(f"run_id {run_id} not found in {config.LOG_PATH}")
    # If a headline appears twice in one run, keep the last attempt.
    return list({r["news_id"]: r for r in picked}.values())


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def score_run(records: list[dict], truth: dict) -> dict[str, Any]:
    tp = fp = fn = 0
    scan_ok = scan_n = exact = 0
    backup_ok = coverage_ok = backup_n = 0
    correlated = correlated_unwarned = 0
    statuses: Counter = Counter()
    per_headline, misses = [], []
    scored = [r for r in records if r["news_id"] in truth["news"]]

    for r in scored:
        case = truth["news"][r["news_id"]]
        statuses[r["status"]] += 1
        expected = set(case["expected_suppliers"])
        allowed = expected | set(case["acceptable_extra"])
        flagged = {s["supplier_id"] for s in (r.get("suppliers") or [])}
        h_tp, h_fn, h_fp = flagged & expected, expected - flagged, flagged - allowed
        tp, fn, fp = tp + len(h_tp), fn + len(h_fn), fp + len(h_fp)

        if r.get("scan") is not None and case["is_disruption"] is not None:  # None = either answer accepted
            scan_n += 1
            scan_ok += r["scan"]["is_disruption"] == case["is_disruption"]
        ok = not h_fn and not h_fp and r["status"] != "error"
        exact += ok

        warned = {(w["supplier_id"], w["backup_supplier_id"]) for w in r.get("backup_warnings", [])}
        for a in (r.get("advice") or {}).get("advice", []):
            if a.get("backup_supplier_id") in flagged and a["supplier_id"] in flagged:
                correlated += 1
                if (a["supplier_id"], a["backup_supplier_id"]) not in warned:
                    correlated_unwarned += 1
                    misses.append(f"{r['news_id']} {a['supplier_id']}: recommends backup {a['backup_supplier_id']}, "
                                  "which is hit by the same event, with no warning")
            key = truth["backups"].get(a["supplier_id"])
            if key and a["supplier_id"] in flagged:
                backup_n += 1
                backup_ok += a.get("backup_supplier_id") == key["backup"]
                coverage_ok += a.get("coverage") == key["coverage"]
                if a.get("backup_supplier_id") != key["backup"] or a.get("coverage") != key["coverage"]:
                    misses.append(f"{r['news_id']} {a['supplier_id']}: backup {a.get('backup_supplier_id')} "
                                  f"({a.get('coverage')}), key says {key['backup']} ({key['coverage']})")

        per_headline.append({"news_id": r["news_id"], "status": r["status"], "ok": ok,
                             "missed": sorted(h_fn), "false_alarms": sorted(h_fp)})
        if h_fn or h_fp or r["status"] == "error":
            note = []
            if h_fn:
                note.append(f"missed {', '.join(sorted(h_fn))}")
            if h_fp:
                note.append(f"false alarm {', '.join(sorted(h_fp))}")
            if r["status"] == "error":
                note.append(f"error: {(r.get('error') or '')[:90]}")
            misses.insert(0, f"{r['news_id']} {r['headline'][:60]}: {'; '.join(note)}")

    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f1 = round(2 * precision * recall / (precision + recall), 3) if precision and recall else None
    usage = [u for r in scored for u in r.get("usage", [])]
    tokens = sum(r.get("input_tokens", 0) + r.get("output_tokens", 0) for r in scored)
    return {
        "headlines_scored": len(scored),
        "headlines_in_key": len(truth["news"]),
        "exact_headlines": exact,
        "scanner_accuracy": _ratio(scan_ok, scan_n),
        "supplier_precision": precision, "supplier_recall": recall, "supplier_f1": f1,
        "true_positives": tp, "false_positives": fp, "false_negatives": fn,
        "backup_accuracy": _ratio(backup_ok, backup_n), "coverage_accuracy": _ratio(coverage_ok, backup_n),
        "backup_checks": backup_n,
        "backups_hit_by_same_event": correlated,
        "backups_hit_by_same_event_unwarned": correlated_unwarned,
        "statuses": dict(statuses),
        "verifier_revisions": sum(r.get("revisions", 0) for r in scored),
        "impact_rule_corrections": sum(len(r.get("impact_disagreements", [])) for r in scored),
        "json_repairs": sum(u.get("repairs", 0) for u in usage),
        "tokens": tokens,
        "tokens_per_headline": round(tokens / len(scored)) if scored else None,
        "cost_usd": round(sum(r.get("cost_usd", 0) for r in scored), 4),
        "seconds_per_headline": round(sum(r.get("seconds", 0) for r in scored) / len(scored), 1) if scored else None,
        "per_headline": per_headline,
        "misses": misses,
    }


def score_retrieval(questions: list[dict], retriever=None) -> dict[str, Any]:
    from agent.rag import get_retriever
    retriever = retriever or get_retriever()
    hit1 = hit4 = 0
    rr, misses = 0.0, []
    for q in questions:
        hits = retriever.search(q["question"], k=4)
        contracts = [h["chunk_id"].split("#")[0] for h in hits]
        if contracts[:1] == [q["expected_contract"]]:
            hit1 += 1
        if q["expected_contract"] in contracts:
            hit4 += 1
            rr += 1 / (contracts.index(q["expected_contract"]) + 1)
        else:
            misses.append(f"{q['qid']} '{q['question'][:55]}': wanted {q['expected_contract']}, got {contracts[:2]}")
    n = len(questions)
    return {"retriever": retriever.name, "questions": n, "hit_at_1": _ratio(hit1, n), "hit_at_4": _ratio(hit4, n),
            "mrr": round(rr / n, 3) if n else None, "misses": misses}


def score_events(records: list[dict], sequence: list[dict]) -> dict[str, Any]:
    """Event memory: right decision per headline, right grouping, alerts avoided."""
    by_id = {r["news_id"]: r for r in records}
    rows, ok = [], 0
    for item in sequence:
        r = by_id.get(item["news_id"], {})
        got = r.get("event_status")
        right = got == item["expected_event_status"] or got in item.get("also_ok", [])
        ok += right
        rows.append({"news_id": item["news_id"], "expected": item["expected_event_status"], "got": got,
                     "right": right, "event_id": r.get("event_id"), "status": r.get("status"),
                     "model_calls": r.get("model_calls", 0),
                     "why": item["why"]})
    pairs = same = 0
    for i, a in enumerate(sequence):
        for b in sequence[i + 1:]:
            ea, eb = by_id.get(a["news_id"], {}).get("event_id"), by_id.get(b["news_id"], {}).get("event_id")
            if ea is None or eb is None:   # a headline judged "not a disruption" has no event: skip the pair
                continue
            pairs += 1
            same += (a["group"] == b["group"]) == (ea == eb)
    alerts = sum(1 for r in rows if r["status"] in ("approved", "needs_review"))
    exposed = sum(1 for r in rows if r["status"] not in ("no_disruption", "no_exposure", None))
    full = [r["model_calls"] for r in rows if r["got"] in ("new", "escalated") and r["model_calls"]]
    quiet = [r["model_calls"] for r in rows if r["got"] == "ongoing"]
    avg_full = sum(full) / len(full) if full else 0
    return {"headlines": len(sequence), "decisions_right": ok, "decision_accuracy": _ratio(ok, len(sequence)),
            "grouping_accuracy": _ratio(same, pairs), "alerts": alerts, "alerts_without_memory": exposed,
            "model_calls": sum(r["model_calls"] for r in rows),
            "model_calls_saved": round(sum(avg_full - q for q in quiet)), "rows": rows}


def render_events(ev: dict, meta: dict) -> str:
    lines = [f"# Event memory: run `{meta['run_id']}`", "",
             f"Model `{meta['model']}` · scored {meta['scored_at']} · {ev['headlines']} headlines in order, fresh memory", "",
             "| What | Score |", "|---|---|",
             f"| Event decision right (new / ongoing / escalated) | {ev['decisions_right']} / {ev['headlines']} |",
             f"| Same real event got the same event id (pairwise) | {_pct(ev['grouping_accuracy'])} |",
             f"| Alerts sent | {ev['alerts']} (without memory: {ev['alerts_without_memory']}) |",
             f"| Model calls | {ev['model_calls']} (about {ev['model_calls_saved']} saved by stopping known events early) |",
             "", "| Headline | Expected | Got | Event | Why this case is here |", "|---|---|---|---|---|"]
    lines += [f"| {r['news_id']} | {r['expected']} | {'✓ ' if r['right'] else '✗ '}{r['got']} | "
              f"`{r['event_id']}` | {r['why']} |" for r in ev["rows"]]
    return "\n".join(lines + [""])


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def render(run: dict, rag: dict | None, meta: dict) -> str:
    lines = [
        f"# Evaluation: run `{meta['run_id']}`",
        "",
        f"Model `{meta['model']}` · prompt `{meta['prompt_version']}` · scored {meta['scored_at']} · "
        f"{run['headlines_scored']} of {run['headlines_in_key']} headlines in the answer key",
        "",
        "| What | Score |",
        "|---|---|",
        f"| Headlines fully right (no miss, no false alarm, no error) | {run['exact_headlines']} / {run['headlines_scored']} |",
        f"| Scanner: disruption or not | {_pct(run['scanner_accuracy'])} |",
        f"| Suppliers flagged: precision | {_pct(run['supplier_precision'])} ({run['false_positives']} false alarms) |",
        f"| Suppliers flagged: recall | {_pct(run['supplier_recall'])} ({run['false_negatives']} missed) |",
        f"| Backup supplier named correctly | {_pct(run['backup_accuracy'])} of {run['backup_checks']} |",
        f"| Backup coverage (full / partial / none) correct | {_pct(run['coverage_accuracy'])} of {run['backup_checks']} |",
        f"| Backups that are hit by the same event | {run['backups_hit_by_same_event']} "
        f"({run['backups_hit_by_same_event_unwarned']} recommended with no warning) |",
    ]
    if rag:
        lines += [f"| Contract search: right contract first ({rag['retriever']}) | {_pct(rag['hit_at_1'])} of {rag['questions']} |",
                  f"| Contract search: right contract in top 4 | {_pct(rag['hit_at_4'])} (MRR {rag['mrr']}) |"]
    s = run["statuses"]
    lines += [
        f"| Outcomes | {', '.join(f'{k} {v}' for k, v in sorted(s.items()))} |",
        f"| Verifier sent advice back | {run['verifier_revisions']} times |",
        f"| Impact ratings corrected by the rule | {run['impact_rule_corrections']} |",
        f"| Invalid JSON repaired | {run['json_repairs']} |",
        f"| Tokens | {run['tokens']:,} ({run['tokens_per_headline']:,} per headline) |",
        f"| Cost | ${run['cost_usd']:.4f} |",
        f"| Time | {run['seconds_per_headline']} s per headline |",
        "",
        "## What went wrong",
        "",
    ]
    misses = run["misses"] + (rag["misses"] if rag else [])
    lines += [f"- {m}" for m in misses] or ["- Nothing: every scored check passed."]
    lines += ["", "Acceptable extras (defensible but not required) are not counted as false alarms. "
              "The answer key and its reasoning are in `eval/ground_truth.json`.", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id")
    ap.add_argument("--no-rag", action="store_true", help="skip the contract retrieval check")
    args = ap.parse_args()

    truth = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    records = load_run(args.run_id)
    sequence = json.loads(SEQUENCE_PATH.read_text(encoding="utf-8"))["news"]
    if {r["news_id"] for r in records} <= {n["news_id"] for n in sequence}:
        ev = score_events(records, sequence)
        meta = {"run_id": records[0]["run_id"], "model": records[0].get("model"),
                "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        report = render_events(ev, meta)
        EVENTS_MD.write_text(report, encoding="utf-8")
        with HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**meta, "kind": "event_sequence",
                                **{k: v for k, v in ev.items() if k != "rows"}}) + "\n")
        print(report)
        print(f"Saved {EVENTS_MD.relative_to(config.ROOT)} and appended to {HISTORY.relative_to(config.ROOT)}")
        return
    run = score_run(records, truth)
    rag = None if args.no_rag else score_retrieval(json.loads(RAG_PATH.read_text(encoding="utf-8")))
    meta = {"run_id": records[0]["run_id"], "model": records[0].get("model"),
            "prompt_version": records[0].get("prompt_version"),
            "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    report = render(run, rag, meta)
    RESULTS_MD.write_text(report, encoding="utf-8")
    summary = {**meta, **{k: v for k, v in run.items() if k not in ("per_headline", "misses")}}
    if rag:
        summary.update({f"rag_{k}": v for k, v in rag.items() if k != "misses"})
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary) + "\n")
    print(report)
    print(f"Saved {RESULTS_MD.relative_to(config.ROOT)} and appended to {HISTORY.relative_to(config.ROOT)}")


if __name__ == "__main__":
    main()
