"""
flow.py - Buying/selling-PRESSURE proxies. Not order counts (those aren't public).

Two kinds of data, both free:

1. Price-based (from the daily bars we already save): up-volume share and Chaikin Money Flow.
   Methods are documented in technical_analysis.py.

2. FINRA daily short-sale volume (this module downloads it):
       https://cdn.finra.org/equity/regsho/daily/CNMSshvolYYYYMMDD.txt
   One pipe-delimited row per symbol per trading day: Date|Symbol|ShortVolume|ShortExemptVolume|
   TotalVolume|Market. It is FINRA's "consolidated" file (already summed across the reporting
   facilities). FINRA posts each day's file by about 6 pm New York time.

   short ratio = ShortVolume / TotalVolume (as FINRA reports them)

   HOW TO READ IT (FINRA's own caveats):
     * It covers trades reported to FINRA facilities (mostly off-exchange), not exchange-reported
       volume, so it is not total market volume.
     * It omits some offsetting buy activity, which can make the ratio look higher than the true
       share of short selling.
     * Market-maker hedging, ETF arbitrage and directional shorting are all counted together, so
       a high ratio is NOT the same as bearish bets. A ratio near 50% is typical for many stocks;
       what can be informative is a ticker's change versus its own recent average.
     * It is daily trading activity, not short interest (the outstanding short position).
   Treat it as a flow/pressure indicator to test against returns, not a sell-order count.

Storage (tables created in history.py): short_volume (the numbers), short_have (which
symbol/date pairs were already processed, so new tickers can be backfilled without re-processing
old ones), short_files (dates FINRA has no file for, e.g. market holidays).
"""
import bisect
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

import history
import technical_analysis as ta_mod

FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt"
USER_AGENT = "wsb-ticker-sentiment/1.0 (personal research; contact: you@example.com)"
DELAY = 0.7           # seconds between file downloads
DEFAULT_DAYS = 30     # calendar days of FINRA history to keep per ticker


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def finra_symbol(sym):
    """Our ticker -> FINRA's style: class shares use a slash (BRK.B / BRK-B -> BRK/B)."""
    return sym.upper().replace(".", "/").replace("-", "/")


# ----------------------------------------------------------------------------
# download / parse / store
# ----------------------------------------------------------------------------
def fetch_day(ymd, timeout=30):
    """Text of the FINRA consolidated file for YYYYMMDD, or None if FINRA has no file (404)."""
    r = requests.get(FINRA_URL.format(ymd=ymd), headers={"User-Agent": USER_AGENT}, timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text


def parse_day(text, wanted):
    """{our_symbol: (short_vol, exempt_vol, total_vol)} for the wanted symbols in a file's text."""
    key = {finra_symbol(s): s for s in wanted}
    out = {}
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 5 or parts[0] == "Date":
            continue
        sym = key.get(parts[1])
        if sym is None:
            continue
        try:
            out[sym] = (float(parts[2]), float(parts[3] or 0), float(parts[4]))
        except ValueError:
            continue
    return out


def weekday_dates(upto, days):
    """YYYY-MM-DD weekdays from (upto - days) through upto, oldest first."""
    end = datetime.strptime(upto, "%Y-%m-%d").date()
    out, d = [], end - timedelta(days=days)
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def update_short_volume(conn, symbols, days=DEFAULT_DAYS, now=None, fetch=fetch_day, log=_log):
    """
    Make sure every symbol has FINRA short-sale data for the last `days` calendar days.
    Downloads only dates where some wanted symbol hasn't been processed yet (so a new ticker
    triggers a small backfill, and an unchanged watch-list triggers none). Returns a stats dict.
    """
    want = sorted({s.upper() for s in symbols})
    stats = {"files": 0, "rows": 0, "missing_days": 0, "error": None}
    if not want:
        return stats
    upto = history.expected_last_session(now)
    skip = {d for (d,) in conn.execute("SELECT date FROM short_files WHERE status='missing'")}
    for d in weekday_dates(upto, days):
        if d in skip:
            continue
        have = {s for (s,) in conn.execute("SELECT symbol FROM short_have WHERE date=?", (d,))}
        missing = [s for s in want if s not in have]
        if not missing:
            continue
        try:
            text = fetch(d.replace("-", ""))
        except Exception as e:                 # network trouble: stop, don't hammer FINRA
            stats["error"] = f"{type(e).__name__}: {e}"
            log(f"[flow] FINRA download failed for {d}: {stats['error']}; stopping")
            break
        stats["files"] += 1
        if text is None:
            if d < upto:                       # an older date with no file = market holiday
                conn.execute("INSERT OR REPLACE INTO short_files VALUES (?, 'missing')", (d,))
                stats["missing_days"] += 1
            continue                           # today's file may just not be posted yet
        rows = parse_day(text, missing)
        conn.executemany("INSERT OR REPLACE INTO short_volume VALUES (?,?,?,?,?)",
                         [(s, d, *v) for s, v in rows.items()])
        conn.executemany("INSERT OR REPLACE INTO short_have VALUES (?,?)", [(s, d) for s in missing])
        conn.commit()
        stats["rows"] += len(rows)
        time.sleep(DELAY)
    conn.commit()
    return stats


# ----------------------------------------------------------------------------
# per-symbol series and metrics (all computed from the saved tables)
# ----------------------------------------------------------------------------
class FlowSeries:
    """One symbol's saved daily bars (with high/low) and short-sale volume, with 'as of' metrics."""

    def __init__(self, conn, symbol):
        self.symbol = symbol
        rows = conn.execute(
            """SELECT p.date, p.close, p.volume, h.high, h.low FROM prices p
               LEFT JOIN price_hl h ON h.symbol = p.symbol AND h.date = p.date
               WHERE p.symbol = ? ORDER BY p.date""", (symbol,)).fetchall()
        self.dates = [r[0] for r in rows]
        self.close = [r[1] for r in rows]
        self.vol = [r[2] or 0.0 for r in rows]
        self.high = [r[3] for r in rows]
        self.low = [r[4] for r in rows]
        srows = conn.execute("SELECT date, short_vol, total_vol FROM short_volume WHERE symbol=? "
                             "ORDER BY date", (symbol,)).fetchall()
        self.s_dates = [r[0] for r in srows]
        self.s_short = [r[1] for r in srows]
        self.s_total = [r[2] for r in srows]

    def metrics(self, asof=None):
        """Flow metrics using only data dated <= `asof` (None = everything saved)."""
        i = len(self.dates) if asof is None else bisect.bisect_right(self.dates, asof)
        j = len(self.s_dates) if asof is None else bisect.bisect_right(self.s_dates, asof)
        c, v = self.close[:i], self.vol[:i]
        out = {
            "updown_20": ta_mod.up_down_volume_share(c, v),
            "cmf_20": ta_mod.chaikin_money_flow(self.high[:i], self.low[:i], c, v),
            "short_1d": None, "short_5d": None, "short_20d": None, "short_date": None,
        }
        sh, tt = self.s_short[:j], self.s_total[:j]
        if j:
            out["short_date"] = self.s_dates[j - 1]
            out["short_1d"] = sh[-1] / tt[-1] if tt[-1] > 0 else None
        if j >= 3 and sum(tt[-5:]) > 0:
            out["short_5d"] = sum(sh[-5:]) / sum(tt[-5:])
        if j >= 10 and sum(tt[-20:]) > 0:
            out["short_20d"] = sum(sh[-20:]) / sum(tt[-20:])
        return out

    def rolling(self, bars=90):
        """Aligned series for charts over the last `bars` price bars: dates, CMF(20), up-volume
        share(20)."""
        n = len(self.dates)
        start = max(0, n - bars)
        cmf, ud, dates = [], [], []
        for k in range(start, n):
            e = k + 1
            dates.append(self.dates[k])
            cmf.append(ta_mod.chaikin_money_flow(self.high[:e], self.low[:e], self.close[:e], self.vol[:e]))
            ud.append(ta_mod.up_down_volume_share(self.close[:e], self.vol[:e]))
        return {"dates": dates, "cmf": cmf, "updown": ud}

    def short_series(self, days=60):
        """Daily short ratio and its 5-day volume-weighted average for the last `days` files."""
        j = len(self.s_dates)
        start = max(0, j - days)
        dates, daily, avg5 = [], [], []
        for k in range(start, j):
            dates.append(self.s_dates[k])
            daily.append(self.s_short[k] / self.s_total[k] if self.s_total[k] > 0 else None)
            lo = max(0, k - 4)
            tt = sum(self.s_total[lo:k + 1])
            avg5.append(sum(self.s_short[lo:k + 1]) / tt if tt > 0 and k - lo >= 2 else None)
        return {"dates": dates, "daily": daily, "avg5": avg5}


def latest_metrics(conn, symbols):
    """{symbol: metrics dict} as of the newest saved data."""
    return {s: FlowSeries(conn, s).metrics() for s in symbols}


CSV_COLUMNS = ["short_ratio_1d", "short_ratio_5d", "short_ratio_20d", "short_vol_date"]


def csv_values(m):
    """Values for CSV_COLUMNS from a FlowSeries.metrics() dict (blank where unavailable)."""
    if not m:
        return [""] * len(CSV_COLUMNS)
    r = lambda v: "" if v is None else round(v, 4)
    return [r(m.get("short_1d")), r(m.get("short_5d")), r(m.get("short_20d")), m.get("short_date") or ""]
