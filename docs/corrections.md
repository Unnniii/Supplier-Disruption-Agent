# Corrections log

Every bug, wrong assumption and process failure, with what caught it. Newest first.

## 2026-10-12: first cloud run

| What went wrong | What caught it | Fix |
|---|---|---|
| The first Cloud Run scan crashed: GDELT answered `429 Too Many Requests` before we had asked anything. Cloud Run sends traffic from IP addresses shared with other Google Cloud users, and GDELT limits by IP. Live mode had only ever been tested against recorded replies. | The job's logs (`gcloud logging read`). | Google News RSS was added as a fallback, and it failed too (503): Google News also turns away data-centre traffic. Second fix: direct RSS feeds from six publishers (BBC World and Business, Supply Chain Dive, FreightWaves, The Loadstar, gCaptain), filtered to recent disruption-sounding headlines, tried before Google News. One feed failing is fine. Each headline records its source, and `SDA_LIVE_SOURCE` forces one source for testing. Lesson: "works from my laptop" says nothing about a public API from a cloud IP. |
| The first successful cloud run read 0 headlines and wrote 0 rows: GDELT answered "200 OK" with no articles (quiet throttling), and the code accepted that as a real answer, so the fallbacks never ran. A green tick hid it; the job's own log line "Fetched 0 live headlines" showed it. | Reading the job's logs instead of trusting the success status. | An empty answer now moves on to the next source; only when every source answers empty is it treated as a quiet day. Each run logs which source it used. |
| The first image build failed with `PERMISSION_DENIED` seconds after its service account was created. | The deploy output. | The deploy script retries the build while new permissions spread (up to 4 attempts, 90 s apart). The second attempt succeeded. |
| `gcloud auth application-default login` failed twice on the consent screen. | The user's terminal. | The deploy script falls back to a short-lived token from the normal gcloud login. |

## 2026-10-11: deploying to GCP

| What could have gone wrong | What caught it | Fix |
|---|---|---|
| The container would have been about 0.75 GB unpacked, mostly Streamlit's data libraries, which nothing uses yet. That would push it past the 0.5 GB free image storage. | Measuring the installed size before writing the Dockerfile. | Streamlit moved to an optional `ui` extra; the cloud image installs only what the scan needs (about 445 MB unpacked). |
| `gcloud builds submit` uploads the whole folder, which on the laptop includes `.env` with the API key. | Listing the files gcloud would upload (`gcloud meta list-files-for-upload`). | `.gcloudignore` excludes `.env`, and the check was repeated: only `.env.example` goes up. |
| On new GCP projects the default Cloud Build identity often cannot push images, which fails the first deploy. | Design review. | Builds run as a dedicated `sda-build` account with three narrow roles. |
| Windows PowerShell 5.1 treats gcloud's normal progress messages as errors when the script is set to stop on errors. | Known PowerShell behaviour, checked before the first real deploy. | The deploy script checks exit codes instead. It was parsed with PowerShell 7 and dry-run end to end with stand-in commands. |

## 2026-10-11: second full run hit the daily quota

| What went wrong | What caught it | Fix |
|---|---|---|
| The rerun stopped scoring at N23: the Gemini free tier allows 500 requests per model per day, and two full runs in one day used it up. The retry logic treated the daily limit like the per-minute one, so it waited about 75 seconds per headline before failing each of the last 12, and marked them `error`, which would have scored as model mistakes. | The user's run output. | A daily-quota `429` now stops the run at once and says when it resets. `503` overloads are retried. Unfinished headlines are not logged, and `agent.run --resume` finishes them later under the same run id, so the evaluation scores one complete run. The call cap stops the run the same way. |

## 2026-10-10: first full Gemini run (34 headlines)

| What went wrong | What caught it | Fix |
|---|---|---|
| The Advisor recommended backups that were hit by the same event: Taichung as Hsinchu's backup in the Hualien earthquake, Ganzhou as Baotou's backup under the same Chinese export licences, and Taichung for Hitachinaka and Hsinchu on the blocked Suez lane. Every one was "correct" against the contracts, so the score did not catch it. | Reading the alerts, not just the score. | `correlated_backups()` in code flags any backup that is itself on the exposed list, the alert prints a warning, the Advisor is told which suppliers are hit, and the evaluation now counts unwarned correlated backups. |
| A full run took about 20 minutes and printed nothing until the end, which looked like a hang. | The user's first full run. | Progress line per headline, rate-limit waits and first-time indexing are printed as they happen. |

## 2026-10-10: data expansion and evaluation

| What changed or went wrong | What caught it | Fix |
|---|---|---|
| Adding suppliers on the Suez route would silently change the right answer for the 2021 Suez replay (N07) and the Felixstowe strike (N08). | Writing the new answer key. | Stated in the key that route and port events replay against today's supplier base, and recomputed N07 and N08. |
| Adding suppliers could have reshuffled the random stock numbers for S001 to S030, making old runs incomparable. | Design check before seeding. | New suppliers use their own random stream; a test asserts the original rows are byte-identical. |
| "Flash floods disrupt traffic in Jakarta" was keyed as not a disruption, but a city flood can delay freight. A reasonable Scanner would have been marked wrong. | Reviewing the stand-in's Scanner errors in the first offline evaluation. | The key now accepts either answer for that headline; the outcome (no exposure) is still scored. |
| Country-wide events (China's magnet licences) could not be mapped: every tool was geographic, and a radius around a country centre is meaningless. | Writing the N22 answer key. | Added `find_suppliers_in_country(country, category)`. |

## 2026-10-09: first real run (Gemini free tier, 3 headlines)

| What went wrong | What caught it | Fix |
|---|---|---|
| Gemini embeddings returned 1 vector for a batch of 37 contract clauses, so Chroma refused the insert and contract search silently fell back to Chroma's local model. | The fallback warning printed by `get_retriever()`. | Send each text as its own `Content`; if the count still does not match, embed one at a time. Regression test with a client that mimics the failure. |
| On the Suez headline (10 suppliers) the Impact agent's JSON broke at character 4005, failing the headline. Cause: the schema asked the model to copy every part's numbers back, and the 1,500-token output budget is shared with Gemini's thinking. | Pydantic validation in the tool loop; the headline was marked `error` and the batch carried on. | The model now returns only the rating, at most 3 driver part ids and a one-line summary; code attaches part details from the ERP. Output budget raised to 8,192. One repair turn on invalid JSON. |
| The Impact agent's rating was trusted as-is, although the rule is plain arithmetic. | Design review after the failure above. | `check_impact()` recomputes the rating from the ERP. The rule wins; disagreements are logged so the evaluation can count them. |
| `uv run python -m agent.run` failed with `No module named 'agent'` on a clean Windows install. Tests passed only because pytest adds `src` to the path. | First run on a fresh machine. | Added a build system so `uv sync` installs the package. |

## Before the first real run

| What went wrong | What caught it | Fix |
|---|---|---|
| The rule-based stand-in treated "Coventry City **win**" style negation words as substrings, so "winter storm" was read as a non-event. | Offline run over all 20 headlines. | Whole-word matching. |
| Swapping keyword search for embeddings changed which contract clause ranked first, and the stand-in Advisor lost the backup suppliers for S003, S004 and S027. | Comparing the sandbox run (BM25) with the first laptop run (embeddings). | Not fixed yet: the RAG evaluation measures it. |
