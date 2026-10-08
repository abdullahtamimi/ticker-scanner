#!/usr/bin/env python3
"""
smg_scraper.py - Find /smg/ (Stock Market General) threads on 4chan's /biz/,
scan every post, and tally stock tickers / company names mentioned.

Uses 4chan's official read-only JSON API (https://github.com/4chan/4chan-API).
The API rules: max 1 request/second, send a User-Agent. This script honors both.

Usage:
    pip install requests
    python smg_scraper.py                 # print results
    python smg_scraper.py --csv out.csv   # also save CSV
    python smg_scraper.py --top 40 --min-count 2

Ticker detection (in order of confidence):
  1. Cashtags       $TSLA, $nvda         -> always counted (unless crypto)
  2. Bare uppercase TSLA, NVDA           -> counted only if it's a real US ticker
                                            (needs the SEC ticker list) and not a
                                            common English word / slang
  3. Company names  "tesla", "nvidia"    -> via the ALIASES table below (extend it!)
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests

BOARD = "biz"
CATALOG_URL = f"https://a.4cdn.org/{BOARD}/catalog.json"
THREAD_URL = f"https://a.4cdn.org/{BOARD}/thread/{{}}.json"
HUMAN_THREAD_URL = f"https://boards.4chan.org/{BOARD}/thread/{{}}"

# SEC asks for a descriptive UA with contact info. Put your own here.
USER_AGENT = "smg-ticker-scraper/1.0 (personal research; contact: you@example.com)"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKER_CACHE = "sec_tickers_cache.json"

REQUEST_DELAY = 1.1  # seconds between 4chan requests (API rule: >= 1s)

# --------------------------------------------------------------------------
# Word lists
# --------------------------------------------------------------------------

# Real tickers that are also everyday words / slang / posting abbreviations.
# These are ONLY counted when written as a cashtag ($ALL), never as bare text.
AMBIGUOUS = {
    "A", "I", "IT", "ARE", "ALL", "ON", "FOR", "NOW", "BE", "SO", "AM", "AN",
    "AT", "BY", "DO", "GO", "HE", "IF", "IN", "IS", "ME", "MY", "NO", "OF",
    "OR", "TO", "UP", "US", "WE", "CAN", "HAS", "HAD", "HOW", "NEW", "OUT",
    "ONE", "OLD", "OWN", "SEE", "TWO", "WAY", "WHO", "YOU", "ANY", "BIG",
    "CEO", "CFO", "COO", "DD", "EPS", "ETF", "FED", "FOMO", "FUD", "GDP",
    "IMO", "IPO", "IRS", "ITM", "OTM", "ATM", "ATH", "LOL", "LMAO", "NGMI",
    "OP", "PE", "PM", "AM", "PSA", "ROI", "SEC", "TA", "USD", "USA", "WSB",
    "YOLO", "HODL", "DCA", "BTFD", "COPE", "SEETHE", "FREE", "REAL", "LOVE",
    "GOOD", "BEST", "HUGE", "HOLD", "WELL", "JUST", "LIKE", "LOW", "HIGH",
    "OPEN", "PLAY", "SAFE", "TRUE", "FAST", "FUN", "EAT", "ARM", "KEY",
    "NEXT", "CASH", "PAY", "RUN", "SAVE", "SELL", "BUY", "BULL", "BEAR",
    "DUDE", "HAPPY", "TRUE", "TURN", "GAIN", "LOSS", "WEEK", "DAY", "YEAR",
    "THE", "AND", "BUT", "NOT", "ARE", "WAS", "WERE", "THIS", "THAT", "WITH",
    "FROM", "HAVE", "WILL", "WHAT", "WHEN", "THEY", "THEM", "THAN", "THEN",
    "WTF", "FFS", "SMH", "IDK", "TBH", "IIRC", "AFAIK", "FWIW", "ELI", "TLDR",
    "NFA", "FAQ", "SMG", "BIZ", "LINK", "GOLD",
}

# Crypto tickers - /biz/ talks about these constantly; not stocks.
CRYPTO = {
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "SHIB", "DOT", "AVAX", "MATIC",
    "BNB", "LTC", "XMR", "ATOM", "NEAR", "ARB", "OP", "SUI", "APT", "PEPE",
    "TON", "TRX", "XLM", "ALGO", "FIL", "AAVE", "UNI", "MKR", "CRV", "LDO",
    "USDT", "USDC", "DAI", "BCH", "ETC", "HBAR", "ICP", "INJ", "TIA", "SEI",
}

# Company-name -> ticker. Matched case-insensitively on word boundaries.
# Extend freely. Avoid names that are also common words (e.g. "target", "block").
ALIASES = {
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "tesla": "TSLA",
    "amazon": "AMZN", "alphabet": "GOOGL", "google": "GOOGL", "facebook": "META",
    "meta platforms": "META", "netflix": "NFLX", "amd": "AMD", "intel": "INTC",
    "micron": "MU", "broadcom": "AVGO", "tsmc": "TSM", "asml": "ASML",
    "palantir": "PLTR", "snowflake": "SNOW", "crowdstrike": "CRWD",
    "coinbase": "COIN", "microstrategy": "MSTR", "robinhood": "HOOD",
    "paypal": "PYPL", "shopify": "SHOP", "uber": "UBER", "airbnb": "ABNB",
    "disney": "DIS", "boeing": "BA", "lockheed": "LMT", "walmart": "WMT",
    "costco": "COST", "starbucks": "SBUX", "mcdonalds": "MCD", "nike": "NKE",
    "coca-cola": "KO", "coca cola": "KO", "pepsi": "PEP", "pfizer": "PFE",
    "moderna": "MRNA", "eli lilly": "LLY", "novo nordisk": "NVO",
    "unitedhealth": "UNH", "jpmorgan": "JPM", "goldman sachs": "GS",
    "berkshire": "BRK.B", "exxon": "XOM", "chevron": "CVX", "gamestop": "GME",
    "amc": "AMC", "sofi": "SOFI", "rivian": "RIVN", "lucid": "LCID",
    "nio": "NIO", "super micro": "SMCI", "supermicro": "SMCI", "arm holdings": "ARM",
    "salesforce": "CRM", "oracle": "ORCL", "adobe": "ADBE", "ibm": "IBM",
    "spotify": "SPOT", "zoom": "ZM", "roblox": "RBLX", "draftkings": "DKNG",
    "carvana": "CVNA", "ford": "F", "general motors": "GM", "toyota": "TM",
    "cisco": "CSCO", "qualcomm": "QCOM", "texas instruments": "TXN",
    "applied materials": "AMAT", "lam research": "LRCX", "kla": "KLAC",
    "tencent": "TCEHY", "alibaba": "BABA", "sony": "SONY", "visa": "V",
    "mastercard": "MA", "american express": "AXP", "bank of america": "BAC",
    "wells fargo": "WFC", "citigroup": "C", "blackrock": "BLK",
}

# Index ETFs people name in prose
ALIASES.update({"spy": "SPY", "qqq": "QQQ", "voo": "VOO", "vti": "VTI",
                "s&p 500": "SPY", "nasdaq 100": "QQQ"})

PROFANITY = {"nigger", "nigga", "faggot", "retard", "kike", "tranny", "chink",
             "spic", "cunt"}  # used only for the "filtered posts" stat

# --------------------------------------------------------------------------
# Regexes
# --------------------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
BR_RE = re.compile(r"<br\s*/?>", re.I)
QUOTELINK_RE = re.compile(r">>\d+(?:\s*\(OP\))?")           # after html.unescape
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z]{1,5}(?:\.[A-Za-z])?)\b")
BARE_RE = re.compile(r"(?<![A-Za-z0-9$.])([A-Z]{2,5})(?![A-Za-z0-9])")
REPEAT_CHAR_RE = re.compile(r"(.)\1{9,}")                   # "aaaaaaaaaa"

ALIAS_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(
        re.escape(k) for k in sorted(ALIASES, key=len, reverse=True)
    ) + r")(?![a-z0-9])",
    re.I,
)


# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})
_last_request = 0.0


def polite_get(url, **kw):
    """GET with a >=1s gap between calls to 4chan's API."""
    global _last_request
    wait = REQUEST_DELAY - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    resp = session.get(url, timeout=20, **kw)
    _last_request = time.time()
    return resp


def load_valid_tickers(cache_path=TICKER_CACHE):
    """Return a set of real US tickers from the SEC list (cached on disk).
    Returns an empty set if unavailable -> script falls back to cashtags+aliases."""
    data = None
    if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < 7 * 86400:
        with open(cache_path) as f:
            data = json.load(f)
    else:
        try:
            r = session.get(SEC_TICKERS_URL, timeout=20)
            r.raise_for_status()
            data = r.json()
            with open(cache_path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            print(f"[warn] Couldn't fetch SEC ticker list ({e}).", file=sys.stderr)
            if os.path.exists(cache_path):
                with open(cache_path) as f:
                    data = json.load(f)
    if not data:
        return set()
    return {row["ticker"].upper() for row in data.values()}


# --------------------------------------------------------------------------
# 4chan helpers
# --------------------------------------------------------------------------
def find_smg_threads(pattern="/smg/"):
    resp = polite_get(CATALOG_URL)
    resp.raise_for_status()
    found = []
    for page in resp.json():
        for t in page.get("threads", []):
            subject = html.unescape(t.get("sub", "") or "")
            if pattern.lower() in subject.lower():
                found.append({
                    "no": t["no"],
                    "subject": subject,
                    "replies": t.get("replies", 0),
                    "images": t.get("images", 0),
                    "url": HUMAN_THREAD_URL.format(t["no"]),
                })
    return found


def fetch_posts(thread_no):
    resp = polite_get(THREAD_URL.format(thread_no))
    if resp.status_code == 404:
        return []  # thread pruned between catalog and fetch
    resp.raise_for_status()
    return resp.json().get("posts", [])


def clean_comment(raw_html):
    """HTML post body -> plain text, minus quote links and URLs."""
    if not raw_html:
        return ""
    text = BR_RE.sub(" ", raw_html)
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    text = QUOTELINK_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    return text.strip()


# --------------------------------------------------------------------------
# Filtering + extraction
# --------------------------------------------------------------------------
def is_junk(text, seen_texts):
    """Return a reason string if the post should be skipped, else None."""
    if not text:
        return "empty"
    if len(text) < 3:
        return "too_short"
    if REPEAT_CHAR_RE.search(text):
        return "spam_repeat"
    norm = re.sub(r"\W+", "", text.lower())
    if len(norm) > 20 and norm in seen_texts:
        return "duplicate"
    seen_texts.add(norm)
    letters = [c for c in text if c.isalpha()]
    if len(text) > 30 and len(letters) < 0.3 * len(text):
        return "mostly_symbols"
    return None


def has_profanity(text):
    low = text.lower()
    return any(w in low for w in PROFANITY)


def extract_tickers(text, valid_tickers):
    """Return {ticker: set(source_types)} for a single post."""
    hits = defaultdict(set)

    # 1. cashtags
    for m in CASHTAG_RE.finditer(text):
        sym = m.group(1).upper()
        if sym in CRYPTO:
            continue
        # With SEC list, drop cashtags that aren't real tickers ($FOMO etc.)
        if valid_tickers and sym not in valid_tickers and "." not in sym:
            continue
        hits[sym].add("cashtag")

    # 2. bare ALL-CAPS tokens, validated against SEC list
    if valid_tickers:
        for m in BARE_RE.finditer(text):
            sym = m.group(1)
            if sym in AMBIGUOUS or sym in CRYPTO:
                continue
            if sym in valid_tickers:
                hits[sym].add("symbol")

    # 3. company names
    for m in ALIAS_RE.finditer(text):
        sym = ALIASES[m.group(1).lower()]
        hits[sym].add("name")

    return hits


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Tally stock tickers in 4chan /biz/ /smg/ threads")
    ap.add_argument("--pattern", default="/smg/", help="substring to look for in thread titles")
    ap.add_argument("--top", type=int, default=30, help="rows to print")
    ap.add_argument("--min-count", type=int, default=1, help="hide tickers mentioned fewer times")
    ap.add_argument("--csv", metavar="FILE", help="save full results to CSV")
    ap.add_argument("--no-sec", action="store_true",
                    help="skip SEC ticker list (cashtags + company names only)")
    args = ap.parse_args()

    started = time.time()
    valid = set() if args.no_sec else load_valid_tickers()
    mode = (f"cashtags + symbols (SEC list: {len(valid):,} tickers) + names"
            if valid else "cashtags + names only (no SEC list)")
    print(f"Detection mode: {mode}\n")

    print(f"Fetching catalog for {BOARD}...")
    threads = find_smg_threads(args.pattern)
    if not threads:
        print(f"No threads with '{args.pattern}' in the title right now.")
        return
    print(f"Found {len(threads)} matching thread(s):")
    for t in threads:
        print(f"  #{t['no']}  {t['replies']} replies  {t['subject']}\n    {t['url']}")
    print()

    mentions = Counter()                 # total mentions (1 per post per ticker)
    source_counts = defaultdict(Counter) # ticker -> {cashtag/symbol/name: n}
    posters = defaultdict(set)           # ticker -> distinct post numbers
    first_seen, last_seen = {}, {}
    skip_reasons = Counter()
    total_posts = analyzed = posts_with_hits = profane = 0

    for t in threads:
        print(f"Scanning thread #{t['no']}...")
        posts = fetch_posts(t["no"])
        seen_texts = set()
        for p in posts:
            total_posts += 1
            text = clean_comment(p.get("com", ""))
            reason = is_junk(text, seen_texts)
            if reason:
                skip_reasons[reason] += 1
                continue
            analyzed += 1
            if has_profanity(text):
                profane += 1  # still analyzed; just reported as a stat
            hits = extract_tickers(text, valid)
            if hits:
                posts_with_hits += 1
            ts = p.get("time", 0)
            for sym, sources in hits.items():
                mentions[sym] += 1
                posters[sym].add(p["no"])
                for s in sources:
                    source_counts[sym][s] += 1
                first_seen[sym] = min(first_seen.get(sym, ts), ts)
                last_seen[sym] = max(last_seen.get(sym, ts), ts)

    # ---------------- report ----------------
    def fmt(ts):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M")

    rows = [(s, c) for s, c in mentions.most_common() if c >= args.min_count]

    print("\n" + "=" * 66)
    print(f"{'TICKER':<8}{'MENTIONS':>9}  {'$tag':>5} {'SYM':>5} {'name':>5}   {'FIRST (UTC)':<12} {'LAST (UTC)'}")
    print("-" * 66)
    for sym, cnt in rows[: args.top]:
        sc = source_counts[sym]
        print(f"{sym:<8}{cnt:>9}  {sc['cashtag']:>5} {sc['symbol']:>5} {sc['name']:>5}   "
              f"{fmt(first_seen[sym]):<12} {fmt(last_seen[sym])}")
    print("=" * 66)

    skipped = sum(skip_reasons.values())
    print("\nSTATS")
    print(f"  Threads scanned          : {len(threads)}")
    print(f"  Total posts              : {total_posts}")
    print(f"  Posts analyzed           : {analyzed}")
    print(f"  Posts filtered out       : {skipped}  "
          + (", ".join(f"{k}={v}" for k, v in skip_reasons.most_common()) or ""))
    print(f"  Posts w/ profanity       : {profane}  (kept; flagged only)")
    print(f"  Posts mentioning tickers : {posts_with_hits}"
          + (f"  ({posts_with_hits / analyzed:.1%} of analyzed)" if analyzed else ""))
    print(f"  Unique tickers           : {len(mentions)}")
    print(f"  Total ticker mentions    : {sum(mentions.values())}")
    print(f"  Runtime                  : {time.time() - started:.1f}s")
    print("\nNote: 'MENTIONS' counts each ticker once per post. "
          "SYM = bare uppercase symbol, name = company name matched.")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ticker", "mentions", "cashtag", "symbol", "name",
                        "first_seen_utc", "last_seen_utc", "scraped_at_utc"])
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for sym, cnt in mentions.most_common():
                sc = source_counts[sym]
                w.writerow([sym, cnt, sc["cashtag"], sc["symbol"], sc["name"],
                            fmt(first_seen[sym]), fmt(last_seen[sym]), now])
        print(f"\nSaved {len(mentions)} rows to {args.csv}")


if __name__ == "__main__":
    main()
