"""Live news from GDELT, a free global news index that needs no API key.

    uv run python -m agent.run --live              # last 24 hours, up to 15 headlines
    uv run python -m agent.run --live --max-live 30

One request per run. Headlines are de-duplicated, capped, and saved to
data/live/<date>.json so every alert can be traced back to what was read.

Limits, stated plainly:
- GDELT returns titles, not article text, so the Scanner judges from the headline.
- Live headlines have no answer key, so they are not scored. The replay in
  data/news.json is how accuracy is measured; live mode is how it is used.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from agent import config

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
TERMS = ('("factory fire" OR "plant fire" OR "port strike" OR "port closed" OR "export ban" OR "export controls" '
         'OR "production halted" OR "supply chain" OR earthquake OR typhoon OR hurricane OR flood)')
# The language filter syntax is not confirmed in GDELT's public docs; if GDELT
# rejects the query, fetch_live_news retries without it.
QUERIES = [TERMS + " sourcelang:english", TERMS]
LIVE_DIR = config.ROOT / "data" / "live"


def _default_get(params: dict[str, Any]) -> str:
    import httpx
    resp = httpx.get(GDELT_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_articles(raw: str) -> list[dict]:
    """GDELT answers JSON, or plain text when it rejects a query or rate-limits."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GDELT did not return JSON: {raw.strip()[:200]}") from e
    return data.get("articles", [])


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def to_news(articles: list[dict], limit: int) -> list[dict]:
    seen, out = set(), []
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    for a in articles:
        title = (a.get("title") or "").strip()
        key = _norm(title)[:80]
        if len(title) < 25 or key in seen:
            continue
        seen.add(key)
        seen_at = a.get("seendate", "")
        out.append({
            "news_id": f"L{day}-{len(out) + 1:02d}",
            "event_date": f"{seen_at[:4]}-{seen_at[4:6]}-{seen_at[6:8]}" if len(seen_at) >= 8 else day,
            "kind": "live",
            "headline": title,
            "body": f"{title} (source: {a.get('domain', '?')}, {a.get('sourcecountry', '?')}). Headline only; no article text available.",
            "url": a.get("url"),
        })
        if len(out) >= limit:
            break
    return out


def fetch_live_news(limit: int = 15, get: Callable[[dict], str] | None = None,
                    sleep: Callable[[float], None] | None = None) -> list[dict]:
    import time
    get, sleep = get or _default_get, sleep or time.sleep
    last_error: Exception | None = None
    articles: list[dict] = []
    for i, query in enumerate(QUERIES):
        if i:
            sleep(6)  # GDELT allows one request every 5 seconds
        params = {"query": query, "mode": "artlist", "format": "json", "timespan": "1d",
                  "maxrecords": 75, "sort": "datedesc"}
        try:
            articles = parse_articles(get(params))
            break
        except Exception as e:  # rejected query, rate limit, network: try the simpler query once
            last_error = e
    else:
        raise RuntimeError(f"GDELT live fetch failed: {last_error}")
    news = to_news(articles, limit)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    (LIVE_DIR / f"{stamp}.json").write_text(json.dumps(news, indent=2, ensure_ascii=False))
    return news
