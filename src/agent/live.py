"""Live news: GDELT first, Google News RSS as the fallback. Neither needs a key.

    uv run python -m agent.run --live              # last 24 hours, up to 15 headlines
    uv run python -m agent.run --live --max-live 30
    uv run python -m agent.live                    # just fetch and print, no model calls
    $env:SDA_LIVE_SOURCE="google_news"; uv run python -m agent.live   # test the fallback (PowerShell)

Headlines are de-duplicated, capped, and saved to data/live/<date>.json so every
alert can be traced back to what was read.

Why two sources: GDELT rate-limits by IP address (one request per 5 seconds).
From a laptop that is fine. From Cloud Run, the outgoing IP is shared with other
Google Cloud users, so GDELT often answers 429 before we have asked anything.
Google News RSS tolerates that, so it takes over when GDELT refuses.

Limits, stated plainly:
- Both sources give titles, not article text, so the Scanner judges from the headline.
- Live headlines have no answer key, so they are not scored. The replay in
  data/news.json is how accuracy is measured; live mode is how it is used.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from typing import Any, Callable

from agent import config

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
TERMS = ('("factory fire" OR "plant fire" OR "port strike" OR "port closed" OR "export ban" OR "export controls" '
         'OR "production halted" OR "supply chain" OR earthquake OR typhoon OR hurricane OR flood)')
# The language filter syntax is not confirmed in GDELT's public docs; if GDELT
# rejects the query, fetch_live_news retries without it.
QUERIES = [TERMS + " sourcelang:english", TERMS]
LIVE_DIR = config.ROOT / "data" / "live"

RSS_URL = "https://news.google.com/rss/search"
RSS_TERMS = ('"factory fire" OR "plant fire" OR "port strike" OR "port closed" OR "export ban" OR '
             '"export controls" OR "production halted" OR "supply chain disruption" OR earthquake OR '
             'typhoon OR hurricane OR flood when:1d')


def _default_get(params: dict[str, Any]) -> str:
    import httpx
    resp = httpx.get(GDELT_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.text


def _default_rss_get() -> str:
    import httpx
    resp = httpx.get(RSS_URL, params={"q": RSS_TERMS, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"},
                     headers={"User-Agent": "supplier-disruption-agent/0.1 (portfolio project)"},
                     timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def parse_rss(raw: str) -> list[dict]:
    """Google News RSS -> the same article shape GDELT returns."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise RuntimeError(f"Google News did not return RSS: {raw.strip()[:200]}") from e
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        src = item.find("source")
        name = (src.text or "").strip() if src is not None else ""
        if name and title.endswith(f" - {name}"):  # Google appends " - Publisher"
            title = title[: -len(name) - 3].strip()
        seen = ""
        if item.findtext("pubDate"):
            try:
                seen = parsedate_to_datetime(item.findtext("pubDate")).strftime("%Y%m%dT%H%M%SZ")
            except (TypeError, ValueError):
                pass
        site = urlparse(src.get("url", "")).netloc if src is not None else ""
        out.append({"title": title, "url": item.findtext("link"), "seendate": seen,
                    "domain": site or name or "?", "sourcecountry": "?"})
    return out


def parse_articles(raw: str) -> list[dict]:
    """GDELT answers JSON, or plain text when it rejects a query or rate-limits."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GDELT did not return JSON: {raw.strip()[:200]}") from e
    return data.get("articles", [])


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def to_news(articles: list[dict], limit: int, source: str = "gdelt") -> list[dict]:
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
            "source": source,
            "headline": title,
            "body": f"{title} (source: {a.get('domain', '?')}, {a.get('sourcecountry', '?')}). Headline only; no article text available.",
            "url": a.get("url"),
        })
        if len(out) >= limit:
            break
    return out


def fetch_live_news(limit: int = 15, get: Callable[[dict], str] | None = None,
                    sleep: Callable[[float], None] | None = None,
                    rss_get: Callable[[], str] | None = None) -> list[dict]:
    import time
    get, sleep, rss_get = get or _default_get, sleep or time.sleep, rss_get or _default_rss_get
    errors: list[str] = []
    articles: list[dict] | None = None
    source = "gdelt"
    queries = [] if os.getenv("SDA_LIVE_SOURCE") == "google_news" else QUERIES
    if not queries:
        errors.append("GDELT: skipped (SDA_LIVE_SOURCE=google_news)")
    for i, query in enumerate(queries):
        if i:
            sleep(10)  # GDELT allows one request every 5 seconds per IP
        params = {"query": query, "mode": "artlist", "format": "json", "timespan": "1d",
                  "maxrecords": 75, "sort": "datedesc"}
        try:
            articles = parse_articles(get(params))
            break
        except Exception as e:  # rejected query, rate limit, network
            errors.append(f"GDELT: {e}")
    if articles is None:
        print(f"GDELT unavailable ({errors[-1][:120]}); using Google News RSS instead.", flush=True)
        try:
            articles, source = parse_rss(rss_get()), "google_news"
        except Exception as e:
            errors.append(f"Google News: {e}")
            raise RuntimeError("Live fetch failed from every source: " + " | ".join(errors)) from e
    news = to_news(articles, limit, source)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    (LIVE_DIR / f"{stamp}.json").write_text(json.dumps(news, indent=2, ensure_ascii=False))
    return news


if __name__ == "__main__":
    items = fetch_live_news()
    print(f"{len(items)} headlines from {items[0]['source'] if items else '-'}:")
    for n in items:
        print(f"  {n['news_id']}  {n['headline'][:100]}")
