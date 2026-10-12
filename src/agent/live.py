"""Live news from three free sources, tried in order. None needs a key.

    1. GDELT            a global news index, searched with disruption keywords
    2. Publisher feeds  RSS from BBC, Supply Chain Dive, FreightWaves, The Loadstar,
                        gCaptain, filtered to disruption-sounding headlines
    3. Google News RSS  a keyword search

    uv run python -m agent.run --live              # last 24 hours, up to 15 headlines
    uv run python -m agent.run --live --max-live 30
    uv run python -m agent.live                    # just fetch and print, no model calls
    $env:SDA_LIVE_SOURCE="feeds"; uv run python -m agent.live         # force one source (PowerShell);
                                                                       # also "google_news" or "gdelt"

Headlines are de-duplicated, capped, and saved to data/live/<date>.json so every
alert can be traced back to what was read.

Why three: GDELT and Google News both refuse traffic from cloud data centres
(GDELT answers 429, Google News 503), so from Cloud Run they fail even though
they work from a laptop. Publisher RSS feeds are meant to be read by machines,
so they are the source most likely to work from the cloud. Each headline
records which source it came from.

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


# Publisher feeds: general news for disasters and conflict, trade press for ports,
# shipping and plants. URLs as published by each site.
FEEDS = {
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "Supply Chain Dive": "https://www.supplychaindive.com/feeds/news/",
    "FreightWaves": "https://www.freightwaves.com/news/feed",
    "The Loadstar": "https://theloadstar.com/feed/",
    "gCaptain": "https://gcaptain.com/feed/",
}
# A cheap pre-filter so 15 slots go to likely disruptions, not general news.
# The Scanner agent still makes the real call.
DISRUPTION_WORDS = re.compile(
    r"\b(fires?|blaze|explosions?|blasts?|strikes?|walkouts?|flood\w*|earthquakes?|quakes?|tsunami|"
    r"typhoons?|hurricanes?|cyclones?|storms?|tornado\w*|wildfires?|exports?|bans?|banned|sanction\w*|"
    r"tariffs?|shortages?|halt\w*|shut\w*|closures?|closed|blockade\w*|attack\w*|ports?|canal|strait|"
    r"lockdowns?|outages?|cyber\w*|derail\w*|collaps\w*|disrupt\w*|stoppages?)\b",
    re.IGNORECASE)
UA = {"User-Agent": "supplier-disruption-agent/0.1 (portfolio project; github.com/Unnniii/Supplier-Disruption-Agent)"}


def _default_feed_get(url: str) -> str:
    import httpx
    resp = httpx.get(url, headers=UA, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def fetch_feeds(feed_get: Callable[[str], str], now: datetime | None = None) -> list[dict]:
    """Read every publisher feed; keep recent, disruption-sounding headlines, newest first.
    One feed failing is fine; all failing raises."""
    now = now or datetime.now(timezone.utc)
    items, failed = [], []
    for name, url in FEEDS.items():
        try:
            items += parse_rss(feed_get(url))
        except Exception as e:
            failed.append(f"{name}: {str(e)[:80]}")
    if len(failed) == len(FEEDS):
        raise RuntimeError("no publisher feed answered (" + "; ".join(failed) + ")")
    keep = []
    for a in items:
        if not DISRUPTION_WORDS.search(a["title"]):
            continue
        if a["seendate"]:
            age = now - datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            if age.total_seconds() > 36 * 3600:
                continue
        keep.append(a)
    return sorted(keep, key=lambda a: a["seendate"], reverse=True)


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
    """An RSS 2.0 feed (Google News or a publisher) -> the same article shape GDELT returns."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise RuntimeError(f"not an RSS feed: {raw.strip()[:200]}") from e
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
        link = (item.findtext("link") or "").strip()
        site = urlparse(src.get("url", "")).netloc if src is not None else ""
        out.append({"title": title, "url": link, "seendate": seen,
                    "domain": site or name or urlparse(link).netloc or "?", "sourcecountry": "?"})
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
                    rss_get: Callable[[], str] | None = None,
                    feed_get: Callable[[str], str] | None = None) -> list[dict]:
    import time
    get, sleep = get or _default_get, sleep or time.sleep
    rss_get, feed_get = rss_get or _default_rss_get, feed_get or _default_feed_get

    def gdelt() -> list[dict]:
        last: Exception | None = None
        for i, query in enumerate(QUERIES):
            if i:
                sleep(10)  # GDELT allows one request every 5 seconds per IP
            params = {"query": query, "mode": "artlist", "format": "json", "timespan": "1d",
                      "maxrecords": 75, "sort": "datedesc"}
            try:
                return parse_articles(get(params))
            except Exception as e:  # rejected query, rate limit, network
                last = e
        raise RuntimeError(str(last))

    sources = [("gdelt", gdelt), ("feeds", lambda: fetch_feeds(feed_get)),
               ("google_news", lambda: parse_rss(rss_get()))]
    forced = os.getenv("SDA_LIVE_SOURCE")
    if forced:
        sources = [s for s in sources if s[0] == forced] or sources
    errors: list[str] = []
    for name, fetch in sources:
        try:
            articles = fetch()
        except Exception as e:
            errors.append(f"{name}: {str(e)[:300]}")
            print(f"Live source {name} unavailable: {str(e)[:120]}", flush=True)
            continue
        news = to_news(articles, limit, name)
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
        (LIVE_DIR / f"{stamp}.json").write_text(json.dumps(news, indent=2, ensure_ascii=False))
        return news
    raise RuntimeError("Live fetch failed from every source: " + " | ".join(errors))


if __name__ == "__main__":
    items = fetch_live_news()
    print(f"{len(items)} headlines from {items[0]['source'] if items else '-'}:")
    for n in items:
        print(f"  {n['news_id']}  {n['headline'][:100]}")
