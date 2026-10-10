# Supplier Disruption Agent

An AI agent that reads the news, works out which suppliers a disruption hits, checks how many days of stock are left for their parts, and looks up the supplier contract for a backup plan.

It runs against the ERP of **Northgate Motors**, a fictional car maker with two UK plants and 41 suppliers. It has two modes:

- **Replay (scored):** 34 headlines, 21 of them real disruptions from 2011 to 2025 (Suez blockage, Renesas chip fire, Thailand floods, Tohoku earthquake, Shanghai lockdown, Red Sea attacks, Panama Canal drought, Baltimore bridge collapse, Hurricane Helene at Spruce Pine, Valencia floods, Typhoon Yagi, Hualien earthquake, China's rare earth magnet licences, the Nexperia chip export ban, the Oswego aluminium mill fire), plus non-events and deliberate traps. Every headline has a hand-written answer key, so accuracy is measured, not claimed.
- **Live (daily):** today's real headlines from GDELT, a free global news index, run through the same agents. Live results cannot be scored, because nobody has written the answer key for today.

> Status: agents, evaluation and live mode are built and tested offline (61 tests). A first 3-headline run on Gemini found bugs, now fixed and logged in [docs/corrections.md](docs/corrections.md). Next: the full scored run on Gemini, and its results in this README.

## The agents

| Agent | Job | Tools it can use | Output |
|---|---|---|---|
| **Scanner** | Is this headline a disruption? What type, where, which route or port? | none | `ScanResult` |
| **Mapper** | Which of our suppliers are exposed? | ERP lookups: suppliers near a place, on a shipping route, using a port, or in a country and category (for export controls) | `MapResult` |
| **Impact analyst** | How bad is it? Days of stock cover, critical parts, vehicle models | parts and stock, open purchase orders | `ImpactResult` |
| **Contract advisor** | What does the contract say? Backup supplier, notice period, buffer stock | contract search (RAG) | `AdviceResult` with citations |
| **Verifier** | Are the advisor's claims backed by clauses it actually retrieved? | plain code, no LLM | approve, send back once, or flag for review |

Each agent has its own instructions, its own tools and its own output schema. An agent only sees the tools it needs, so the Scanner cannot query the ERP and the Mapper cannot read contracts.

## Orchestration (LangGraph)

```text
scanner --(not a disruption)------------------------------> END
   |
mapper  --(no supplier exposed)---------------------------> END
   |
impact -> advisor -> verifier --(issues, first time)--> advisor
                         |
                         +--(approved, or out of revisions)--> END
```

- Early exits keep cost down: in the answer key, 14 of the 34 headlines should stop after the Scanner or Mapper.
- The Mapper can only report suppliers the ERP tools returned; invented IDs are dropped.
- The math is not done by the LLM. Distances, radii and days of cover are plain Python and SQL. The Impact agent's risk rating is re-checked against the rule in code; the rule wins and disagreements are logged.
- If an agent returns invalid JSON it gets one repair turn with its own error message before the headline is marked `error`.
- A failing agent marks that headline `error` and the batch carries on.
- Every headline is logged to `logs/runs.jsonl` with each agent's tokens, tool calls, time and cost.

## Models and cost: built to run for £0

| `SDA_PROVIDER` | Model | Cost | Use it for |
|---|---|---|---|
| `gemini` (default) | Gemini 3.1 Flash-Lite + Gemini Embedding 2, via Google AI Studio | free tier | normal runs and evaluation |
| `ollama` | any local model, e.g. `qwen3:8b` | free, runs on your laptop | no-quota, fully offline runs |
| `claude` | Claude Haiku 5.5 | paid | comparison only |
| `fake` / `--fake` | rule-based stand-in | free | tests and CI |

Switching provider is a setting, not a code change: every agent talks to one `LLM` interface and each adapter translates to its own API (`src/agent/llm.py`). The tool-use loop is ours on every provider, so logging, the cap and error handling behave the same everywhere.

**The cap.** Every run is wrapped in `CappedLLM`. After `SDA_MAX_LLM_CALLS` model calls (default 200; the offline 20-headline scan makes 80, and a real model takes extra tool turns) the run stops, the remaining headlines are logged as `error: call cap reached`, and nothing else is spent. Free-tier `429` rate limits are retried with backoff. Contract embeddings are computed once and stored in Chroma, so re-runs do not spend embedding quota.

**Free-tier caveats, stated plainly.** Google may use free-tier inputs to improve its products; all data in this project is fictional, so that is acceptable here. Google's terms restrict the free tier for apps *serving* users in the UK and EEA; this repo is a personal demo, and `ollama` is the fully local alternative.

## The data

`scripts/seed_erp.py` builds `data/erp.db` (SQLite) with fixed random seeds. Suppliers S031 to S041 were added later from their own random stream, so the data for S001 to S030 is unchanged and earlier runs stay comparable (a test checks this).

| Table | Rows | What it holds |
|---|---|---|
| `plants` | 2 | Midlands Assembly, Merseyside Assembly |
| `suppliers` | 41 | location (lat/lon), category, tier, origin port, shipping route, UK port of entry |
| `parts` | 98 | part, supplier, cost, lead time, critical flag, vehicle models it goes into |
| `inventory` | 148 | stock on hand, daily usage, safety stock, per part per plant |
| `purchase_orders` | 98 | open orders with route, port and ETA |

Other inputs:

- `data/news.json`: 34 headlines (21 real past disruptions, 13 non-events or traps)
- `data/contracts/`: 23 supplier contracts (backup supplier, force majeure notice, buffer stock, freight terms)
- `data/gazetteer.json`: place names to coordinates, shipping routes, route aliases (the Red Sea is the Suez lane), ports

## The answer key

`eval/ground_truth.json` is written by hand, not generated by the agent. For every headline it lists which suppliers must be flagged, which are acceptable extras, and why. Traps it tests:

- A factory fire in Hitachinaka must not flag the pigment plant 75 km away
- The Shanghai lockdown must not flag the Ningbo supplier (different city, different port)
- The Suez blockage and Red Sea attacks must not flag the Penang supplier (ships via the Cape)
- China's rare earth magnet licences hit the two magnet suppliers, not every supplier in China
- The Baltimore bridge collapse is found through the port, not by distance: the bearing supplier is 75 km away in Pennsylvania
- The Hualien earthquake hits Hsinchu Silicon **and its own backup** in Taichung
- "Nakagawa" is not "Hitachinaka"; a heatwave in Valencia with "no closures" is not a disruption, but the 2024 Valencia floods are

`eval/rag_questions.json` holds 14 contract questions with the contract that should be retrieved.

## Evaluation

```bash
uv run python -m agent.run            # full replay, 34 headlines
uv run python -m agent.evaluate       # score it
```

`agent.evaluate` scores the latest run and writes `eval/results.md`, and appends one line to `eval/history.jsonl` so before/after comparisons are a diff, not a memory.

| Check | How it is scored |
|---|---|
| Scanner | disruption or not, against the key |
| Mapper | precision and recall of flagged suppliers; acceptable extras are neither rewarded nor punished |
| Advisor | backup supplier and coverage (full / partial / none) against the contracts |
| Contract search | right contract ranked first, and in the top 4, for 14 questions (hit@1, hit@4, MRR) |
| Pipeline | verifier send-backs, rule corrections, JSON repairs, errors, tokens, cost, time |

**Results:** not yet run on Gemini for all 34 headlines. The offline keyword stand-in (`--fake`) gets 32 of 34 headlines fully right; that is the floor a real model has to beat, not a result.

## Live mode

```bash
uv run python -m agent.run --live                 # last 24 hours, up to 15 headlines
uv run python -m agent.run --live --max-live 30
```

One request to GDELT per run; headlines are de-duplicated, capped and saved to `data/live/` so every alert can be traced to what was read. GDELT returns titles, not article text, so the Scanner judges from the headline. Most days nothing touches 41 fictional suppliers, so "no exposure" is the normal answer.

**Daily on Windows:** `scripts/run_daily.ps1` runs the live scan and appends to `logs/daily.log`. Register it with Task Scheduler using the command at the top of that file.

## Run it

```bash
uv sync
uv run python scripts/seed_erp.py
uv run pytest                                   # 61 tests, offline, free
uv run python -m agent.run --fake               # whole pipeline with a rule-based stand-in model
cp .env.example .env                            # add GEMINI_API_KEY (free, aistudio.google.com/apikey)
uv run python -m agent.run --news N01 N07       # try two headlines first
uv run python -m agent.run                      # full scan, Gemini free tier
SDA_PROVIDER=ollama uv run python -m agent.run  # local model instead (install Ollama, `ollama pull qwen3:8b`)
```

Contract search uses Gemini embeddings when `GEMINI_API_KEY` is set, otherwise Chroma's local embedding model, otherwise keyword (BM25) search, and prints which one it fell back to.

**What the tests cover:** the evaluation maths, live news parsing and fallback, the new country and route-alias tools, that adding suppliers left old data unchanged, the cap, the Gemini and Ollama adapters (with stand-in clients, no network), data integrity, the Map step reaching the answer key, routing and early exits, the tool loop's turn limit and unknown-tool handling, the verifier's checks, the advisor revise loop, and failure isolation.

**`--fake` mode is a floor, not a result.** It is keyword matching that never reads the answer key. It exists so the pipeline can be tested without cost. Real accuracy numbers come from the evaluation on Gemini.

## Known gaps (honest list)

- The verifier checks that a cited clause names the backup, not that the backup is in the right direction. For example, Taichung's contract names Hitachinaka because Taichung is Hitachinaka's backup. A model could misread that as "Hitachinaka is Taichung's backup" and pass the check.
- When the Scanner names both a country and a city, a wide radius (war: 600 km) from the city can catch suppliers in neighbouring countries. The evaluation will show how often this happens.
- The Ollama path is tested with stand-ins only. The Gemini path has had one real 3-headline run (see the corrections log).
- Live mode is tested against recorded GDELT replies, not the live service (the build sandbox cannot reach it). GDELT's language filter syntax is not confirmed in its public docs, so live mode retries without it if the first query is rejected.
- The answer key was written by the same person who built the system. An independent second annotator would make the scores more trustworthy.
- Gemini model names change; if `gemini-3.1-flash-lite` returns 404, set `SDA_MODEL` to the current Flash-Lite name shown in AI Studio.

## Roadmap

- [x] Day 1: fake ERP, news, contracts, answer key, deterministic tools, tests
- [x] Day 1: RAG over contracts, four agents + verifier, LangGraph orchestration, run logs
- [x] Free model backends (Gemini free tier, Ollama) and a per-run call cap
- [x] Evaluation script: precision, recall, backup accuracy, retrieval hit rate, cost
- [x] 11 more suppliers and 14 more headlines from real 2023 to 2025 disruptions
- [x] Live mode (GDELT) and a daily Windows schedule
- [ ] Full scored run on Gemini; results in this README
- [ ] Fix the worst miss and re-run (before/after numbers)
- [ ] Streamlit app, demo GIF
- [ ] GCP deployment at £0: Cloud Run Job + Cloud Scheduler + BigQuery, Terraform, budget alert, max 1 instance
- [ ] Real geocoding instead of the gazetteer
