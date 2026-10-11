"""
stocktwits_scraper.py - StockTwits trending symbols + StockTwits' own sentiment scores.

We deliberately do NOT use StockTwits message text or message counts (spam-prone).
Two things are used:
  1. the trending-symbols list (to decide which tickers to look at), and
  2. the site's published per-symbol scores (sentiment / message volume / participation,
     each 0-100) from the same endpoint the symbol's /sentiment page calls.

NOTES
  * Undocumented endpoints the website itself uses; they may change. StockTwits' terms
    may restrict automated access - read them before relying on this.
  * On HTTP 401/403 this module stops with a clear error. It does NOT try to disguise
    itself, use cookies, or log in to get around that.
"""
import sys
import time
from collections import Counter

import requests

import smg_scraper as smg

BASE = "https://api.stocktwits.com/api/2"
USER_AGENT = "wsb-ticker-sentiment/1.0 (personal research; contact: you@example.com)"
DELAY = 1.5   # seconds between requests - be gentle

_sess = requests.Session()
_sess.headers.update({"User-Agent": USER_AGENT})
_last = 0.0


class StockTwitsError(Exception):
    pass


def _get(path, params=None):
    """GET a path under BASE, or a full https:// URL. Returns parsed JSON, or None on 404."""
    global _last
    for _ in range(2):
        wait = DELAY - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        url = path if path.startswith("http") else f"{BASE}/{path}"
        r = _sess.get(url, params=params, timeout=20)
        _last = time.time()
        if r.status_code == 429:
            try:
                pause = min(int(r.headers.get("Retry-After", 30)), 120)
            except ValueError:
                pause = 30
            print(f"[stocktwits] rate limited; sleeping {pause}s", file=sys.stderr)
            time.sleep(pause)
            continue
        if r.status_code in (401, 403):
            raise StockTwitsError(
                f"HTTP {r.status_code}: StockTwits is refusing automated access to {url}. "
                "Not retrying or working around it.")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    raise StockTwitsError("Still rate limited (HTTP 429) after retry; try again later.")


def _is_crypto(sym):
    return sym.endswith(".X") or sym.upper() in smg.CRYPTO


def get_trending_symbols(limit=25):
    """Ordered list of currently trending (non-crypto) symbols."""
    data = _get("trending/symbols.json")
    syms = []
    if data and data.get("symbols"):
        syms = [s["symbol"].upper() for s in data["symbols"] if s.get("symbol")]
    else:  # fall back to counting symbols in the trending message stream
        data = _get("streams/trending.json", {"limit": 30})
        counts = Counter()
        for m in (data or {}).get("messages", []):
            for s in m.get("symbols", []):
                if s.get("symbol"):
                    counts[s["symbol"].upper()] += 1
        syms = [s for s, _ in counts.most_common()]
    syms = [s for s in syms if not _is_crypto(s)]
    return syms[:limit]


def _dig(obj, path):
    """Follow a dotted path like 'data.sentiment.now.valueNormalized' (list indexes allowed)."""
    for part in path.split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


def _score(x):
    """Accept only numbers in 0..100 so a wrong JSON path can't slip through silently."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 100 else None


def fetch_scores(symbols, url_template, score_path, volume_path=None, participation_path=None):
    """
    Fetch StockTwits' published per-symbol scores.
      url_template : e.g. ".../sentiment-api/v2/{symbol}/detail"  ({symbol} is filled in)
      score_path   : dotted path to the 0-100 sentiment score in the JSON response
      volume_path / participation_path : optional paths to the other two 0-100 scores
    Returns {symbol: {"sentiment": float, "volume": float|None, "participation": float|None}}.
    Symbols with no 0-100 value at score_path (unknown symbol / wrong path) are skipped.
    """
    out, bad = {}, 0
    for i, sym in enumerate(symbols, 1):
        data = _get(url_template.format(symbol=sym))
        sent = _score(_dig(data, score_path)) if data is not None else None
        if sent is None:
            bad += 1
        else:
            out[sym] = {
                "sentiment": sent,
                "volume": _score(_dig(data, volume_path)) if volume_path else None,
                "participation": _score(_dig(data, participation_path)) if participation_path else None,
            }
        if i % 10 == 0:
            print(f"[stocktwits] scored {i}/{len(symbols)}", file=sys.stderr)
    print(f"[stocktwits] official scores for {len(out)}/{len(symbols)} symbols", file=sys.stderr)
    if bad:
        print(f"[stocktwits] {bad} symbol(s) had no score (not on StockTwits, or wrong "
              f"path '{score_path}').", file=sys.stderr)
    return out


def numeric_paths(obj, prefix=""):
    """Every numeric leaf in a JSON response as (dotted_path, value) - for --st-probe."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += numeric_paths(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5]):
            out += numeric_paths(v, f"{prefix}.{i}" if prefix else str(i))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append((prefix, obj))
    return out
