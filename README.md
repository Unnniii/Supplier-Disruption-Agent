# Supplier Disruption Agent

An AI agent that reads the news, works out which suppliers a disruption hits, checks how many days of stock are left for their parts, and looks up the supplier contract for a backup plan.

It runs against the ERP of **Northgate Motors**, a fictional car maker with two UK plants. The news includes **real past disruptions** (the 2021 Suez blockage, the 2021 Renesas chip fire, the 2011 Thailand floods, the 2011 Tohoku earthquake, the 2022 Shanghai lockdown and others), replayed against today's supplier base, plus irrelevant headlines and deliberate traps.

> Status: first real run done on the Gemini free tier. Bugs it exposed are fixed and logged in [docs/corrections.md](docs/corrections.md). Next: the evaluation script over all 20 headlines.

## The agents

| Agent | Job | Tools it can use | Output |
|---|---|---|---|
| **Scanner** | Is this headline a disruption? What type, where, which route or port? | none | `ScanResult` |
| **Mapper** | Which of our suppliers are exposed? | ERP lookups: suppliers near a place, on a route, using a port | `MapResult` |
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

- Early exits keep cost down: in the answer key, 11 of the 20 headlines should stop after the Scanner or Mapper.
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

`scripts/seed_erp.py` builds `data/erp.db` (SQLite) with a fixed random seed.

| Table | Rows | What it holds |
|---|---|---|
| `plants` | 2 | Midlands Assembly, Merseyside Assembly |
| `suppliers` | 30 | location (lat/lon), category, tier, origin port, shipping route, UK port of entry |
| `parts` | 72 | part, supplier, cost, lead time, critical flag, vehicle models it goes into |
| `inventory` | 112 | stock on hand, daily usage, safety stock, per part per plant |
| `purchase_orders` | 69 | open orders with route, port and ETA |

Other inputs:

- `data/news.json`: 20 headlines, 9 real past disruptions and 11 non-events or traps
- `data/contracts/`: 15 supplier contracts (backup supplier, force majeure notice, buffer stock, freight terms)
- `data/gazetteer.json`: place name to coordinates (stands in for a geocoding API in v1)

## The answer key

`eval/ground_truth.json` is written by hand, not generated by the agent. For every headline it lists which suppliers must be flagged, which are acceptable extras, and why. Traps it tests:

- A factory fire in Hitachinaka must not flag the pigment plant 75 km away
- The Shanghai lockdown must not flag the Ningbo supplier (different city, different port)
- The Suez blockage must not flag the Penang supplier (ships via the Cape)
- "Nakagawa" is not "Hitachinaka"
- A heatwave in Valencia with "no closures" is not a disruption

`eval/rag_questions.json` holds 10 contract questions with the contract that should be retrieved and the facts the answer must contain.

## Run it

```bash
uv sync
uv run python scripts/seed_erp.py
uv run pytest                                   # 40 tests, offline, free
uv run python -m agent.run --fake               # whole pipeline with a rule-based stand-in model
cp .env.example .env                            # add GEMINI_API_KEY (free, aistudio.google.com/apikey)
uv run python -m agent.run --news N01 N07       # try two headlines first
uv run python -m agent.run                      # full scan, Gemini free tier
SDA_PROVIDER=ollama uv run python -m agent.run  # local model instead (install Ollama, `ollama pull qwen3:8b`)
```

Contract search uses Gemini embeddings when `GEMINI_API_KEY` is set, otherwise Chroma's local embedding model, otherwise keyword (BM25) search, and prints which one it fell back to.

**What the tests cover:** the cap, the Gemini and Ollama adapters (with stand-in clients, no network), data integrity, the Map step reaching the answer key, routing and early exits, the tool loop's turn limit and unknown-tool handling, the verifier's checks, the advisor revise loop, and failure isolation.

**`--fake` mode is a floor, not a result.** It is keyword matching that never reads the answer key. It exists so the pipeline can be tested without cost. Real accuracy numbers come from the evaluation with Claude.

## Known gaps (honest list)

- The verifier checks that a cited clause names the backup, not that the backup is in the right direction. For example, Taichung's contract names Hitachinaka because Taichung is Hitachinaka's backup. A model could misread that as "Hitachinaka is Taichung's backup" and pass the check.
- When the Scanner names both a country and a city, a wide radius (war: 600 km) from the city can catch suppliers in neighbouring countries. The evaluation will show how often this happens.
- The Ollama path is tested with stand-ins only. The Gemini path has had one real 3-headline run (see the corrections log).
- Gemini model names change; if `gemini-3.1-flash-lite` returns 404, set `SDA_MODEL` to the current Flash-Lite name shown in AI Studio.

## Roadmap

- [x] Day 1: fake ERP, news, contracts, answer key, deterministic tools, tests
- [x] Day 1: RAG over contracts, four agents + verifier, LangGraph orchestration, run logs
- [ ] Day 2: evaluation script (precision, recall, contract retrieval hit rate, cost per run) with the real model
- [ ] Day 3: Streamlit app, live demo, demo GIF
- [x] Free model backends (Gemini free tier, Ollama) and a per-run call cap
- [ ] GCP deployment at £0: Cloud Run Job + Cloud Scheduler + BigQuery, Terraform, budget alert, max 1 instance
- [ ] v2: live news (GDELT), real geocoding
