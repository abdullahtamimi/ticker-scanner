"""
apewisdom_scraper.py - r/wallstreetbets ticker attention from ApeWisdom's public feed.

ApeWisdom (apewisdom.io) is a third-party service that already tracks ticker mentions in
Reddit communities and publishes them as a free, no-key JSON feed. This replaces the
PRAW-based reddit_scraper.py, which needs Reddit API approval.

What you get per ticker: mentions in the last 24h, mentions 24h earlier, upvotes, rank and
rank 24h earlier. What you do NOT get: any sentiment (that needs the comment text).

NOTES
  * Counts are ApeWisdom's own, computed their way and refreshed roughly hourly. Their
    availability depends on their continued access to Reddit; it can change or stop.
  * I haven't verified ApeWisdom's terms of use - check them (and credit them if you publish).
"""
import sys
import time

import requests

BASE = "https://apewisdom.io/api/v1.0/filter"
USER_AGENT = "wsb-ticker-sentiment/1.0 (personal research; contact: you@example.com)"


class ApeWisdomError(Exception):
    pass


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def fetch(filter_name="wallstreetbets", pages=1):
    """Return {TICKER: {"mentions","upvotes","rank","rank_24h_ago","mentions_24h_ago","name"}}.
    Each page holds up to ~100 tickers; pages=1 is the top ~100."""
    out = {}
    for page in range(1, pages + 1):
        url = f"{BASE}/{filter_name}" + (f"/page/{page}" if page > 1 else "")
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code != 200:
            raise ApeWisdomError(f"HTTP {r.status_code} from {url}")
        data = r.json()
        for item in data.get("results", []):
            sym = str(item.get("ticker", "")).upper().strip()
            if not sym:
                continue
            out[sym] = {
                "name": item.get("name"),
                "mentions": _int(item.get("mentions")) or 0,
                "upvotes": _int(item.get("upvotes")),
                "rank": _int(item.get("rank")),
                "rank_24h_ago": _int(item.get("rank_24h_ago")),
                "mentions_24h_ago": _int(item.get("mentions_24h_ago")),
            }
        if page >= (_int(data.get("pages")) or 1):
            break
        time.sleep(1.0)
    if not out:
        raise ApeWisdomError("ApeWisdom returned no results.")
    print(f"[reddit/ApeWisdom] {len(out)} tickers for r/{filter_name}", file=sys.stderr)
    return out
