# Supplier Disruption Agent, explained simply

## The whole idea in one breath

A car factory needs parts from many shops around the world.
If something bad happens near one of those shops (a fire, a flood, a blocked canal), the factory can run out of parts.
This project is a team of robot helpers that read the news every morning and shout early: "This shop is in trouble, here is what we will run out of, and here is who can help us instead."

## The story, like you are 5

1. **The newspaper arrives.** We have 20 news stories. Some are scary (a fire at a chip factory). Some are boring (a football match).
2. **Robot 1, the Scanner, reads each story.** It asks: "Is this something that could stop a factory or a ship?" If no, it stops there.
3. **Robot 2, the Mapper, looks at the map.** It asks: "Which of our shops are close to the trouble, or use that canal or port?"
4. **Robot 3, the Impact checker, opens the cupboard.** It asks: "How many days of parts do we have left from that shop?"
5. **Robot 4, the Contract advisor, reads the agreements.** It asks: "Does our contract name a backup shop? How fast can we switch?"
6. **The Verifier (not a robot, just a strict rule-checker)** asks: "Did Robot 4 only say things the contract really says?" If not, Robot 4 tries once more.
7. **The alert comes out.** For example: "Fire near Hitachinaka. Chip shop S001 hit. 6 days of chips left. Backup shop S011 can help in 6 weeks."

## The 3 big ideas (the words interviewers use)

| Big word | Simple meaning | Where it is here |
|---|---|---|
| **Agents** | Robots that each have one job and their own tools | Scanner, Mapper, Impact checker, Contract advisor |
| **Orchestration** | The boss who decides which robot goes next | LangGraph, in `graph.py` |
| **RAG** | Before answering, look up the right page in the book first | The Contract advisor searching contracts |

## Two golden rules of this project

- **Robots do the thinking, the calculator does the maths.** Distances and days of stock are worked out by plain code, never guessed by AI.
- **Every answer is checked against an answer key.** We wrote down the right answers before the robots ran, so we can score them.

---

## Every file, in simple words

### Top of the folder

| File | What it is |
|---|---|
| `README.md` | The front page of the project. What it does, how to run it, what is not perfect yet. |
| `pyproject.toml` | The shopping list. Tells `uv` which Python libraries to install. |
| `uv.lock` | The exact receipt for that shopping list, so everyone gets the same versions. |
| `.env.example` | A template for your secret settings (like your Gemini key). Copy it to `.env`. |
| `.env` | Your real secrets. Lives only on your laptop, never uploaded to GitHub. |
| `.gitignore` | A "do not upload" list for GitHub (secrets, big generated files, logs). |

### `data/`: the pretend world

| File | What it is |
|---|---|
| `data/erp.db` | The pretend company's database: shops, parts, stock, orders. Made by `seed_erp.py`. |
| `data/news.json` | The 20 news stories. 9 are real past disasters, 11 are noise or tricks. |
| `data/contracts/contract_SXXX.txt` | 15 pretend agreements with shops. They name backup shops, stock and rules. |
| `data/gazetteer.json` | A tiny map book: place name to map position (e.g. "Lviv" to its coordinates). |
| `data/chroma/` | The search index for contracts. Built automatically on the first run. |

### `scripts/`

| File | What it is |
|---|---|
| `scripts/seed_erp.py` | Builds `erp.db` from scratch. Same result every time, so tests are fair. |

### `src/agent/`: the brain

| File | What it is |
|---|---|
| `run.py` | The start button. Reads the news, runs the robots, prints alerts, saves a log. |
| `graph.py` | The boss (orchestration). Decides which robot goes next and when to stop early. |
| `agents.py` | The four robots' instructions, plus the Verifier and the maths double-check. |
| `tools.py` | The tools each robot is allowed to use. Each robot only gets its own tools. |
| `erp.py` | Plain code that reads the database: "which shops are near here?", "how many days of stock?" |
| `rag.py` | Contract search. Splits contracts into clauses and finds the best matching ones. |
| `llm.py` | The phone line to the AI. Can call Gemini (free), Ollama (on your laptop) or Claude (paid). Also has the cap that stops a run after 200 calls. |
| `schemas.py` | The forms each robot must fill in. If the answer is not in the right shape, it is rejected. |
| `config.py` | All the settings in one place: which AI, the cap, file locations. |
| `fake.py` | A pretend AI made of simple rules. Lets you test everything for free with no internet. |
| `__init__.py` | An empty file that tells Python "this folder is a package". |

### `eval/`: the answer key

| File | What it is |
|---|---|
| `eval/ground_truth.json` | The right answers, written by hand: which shops each story should flag, and the traps. |
| `eval/rag_questions.json` | 10 questions about contracts, with the clause that should be found. Tests the RAG. |

### `tests/`: the safety checks

| File | What it is |
|---|---|
| `tests/test_data_and_map.py` | Checks the pretend data makes sense and the map step finds the right shops. |
| `tests/test_agents_graph.py` | Checks the boss sends robots in the right order and handles mistakes. |
| `tests/test_llm_adapters.py` | Checks the phone lines to Gemini and Ollama, the cap, and the fixes from the first real run. |
| `tests/conftest.py` | Setup for tests: build the database if missing, never use the internet. |
| `tests/__init__.py` | Empty file so tests can share helpers. |

### `docs/`: the notebook

| File | What it is |
|---|---|
| `docs/corrections.md` | The "what went wrong and how I fixed it" diary. Interviewers love this. |
| `docs/explained-simply.md` | This file. |

### Made when you run it (not uploaded to GitHub)

| File | What it is |
|---|---|
| `logs/runs.jsonl` | One line per news story per run: what each robot decided, how long, how many tokens. |
| `.venv/` | The box where `uv` installs Python and the libraries. |

---

## The order things happen when you press start

```
run.py
  -> graph.py (the boss)
       -> Scanner  (agents.py)            no tools
       -> Mapper   (agents.py + erp.py)   map tools
       -> Impact   (agents.py + erp.py)   stock tools, then maths double-check
       -> Advisor  (agents.py + rag.py)   contract search
       -> Verifier (agents.py)            rule checks, may send Advisor back once
  -> prints the alert and writes logs/runs.jsonl
```

## Say it in an interview (30 seconds)

"I built a multi-agent system that reads supply chain news, finds which suppliers are hit, checks days of stock in an ERP, and pulls the backup plan from supplier contracts using RAG. LangGraph orchestrates four agents, each with only its own tools. The maths is plain code, not AI, and a verifier rejects any claim the contracts do not support. I score it against a hand-written answer key built from real disruptions like the Suez blockage, and it runs for £0 on Gemini's free tier with a hard cap on calls."
