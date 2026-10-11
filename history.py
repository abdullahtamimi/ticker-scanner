"""
history.py - Save every run to a local SQLite file and check how earlier sentiment lined up
with what the stock did afterwards.

What is stored (all local, in one .db file; no post text is stored):
    runs          one row per run (timestamp, market date, active sources, weights, settings)
    observations  one row per ticker per run: mentions, ranks, sentiment (NET) per source,
                  StockTwits' score, composite/consensus, plus price / SMAs / OBV
    prices        daily adjusted closes + volume for every ticker we've fetched (a cache)
    outcomes_daily forward returns per (market date, ticker): 1, 3 and 5 trading days later, and
                  the same window for SPY so market-wide moves can be separated out
    (plus price_hl, short_volume/short_have/short_files, price_status, smg_threads: see the modules
     that fill them)

MANY RUNS PER DAY, ONE DAILY AVERAGE
    Run as often as you like: every run is saved. Runs are grouped by NEW YORK calendar date (so a
    US evening run belongs to that evening's trading day even after UTC midnight). For every
    comparison - the backtest, scorecard, attention study and the dashboard's trends - each
    (date, ticker) is represented by the AVERAGE of that day's runs (daily_observations): sentiment,
    mentions, scores and heat are means; price fields are the latest value that day; the consensus
    label is recomputed from the averaged nets.

HOW A SIGNAL IS JUDGED
    Base price      : the close of the last session ON OR BEFORE the market date. So every run on a
                      day is judged against the same move, a Saturday run is anchored to Friday's
                      close, and a weekend's sentiment is scored on Monday's move.
    Forward return  : close H trading days after the base close / base close - 1.
                      Excess return = that, minus SPY's return over the same two dates.
    No premature scoring: nothing is scored until a LATER bar exists, so a morning run is never
                      scored against yesterday's close while today's close isn't saved yet.
    Direction call  : the day's average composite (or one source's NET) >= +DEAD_ZONE -> bullish,
                      <= -DEAD_ZONE -> bearish, otherwise neutral (not scored).
    "Matched"       : bullish call and the stock rose, or bearish call and it fell.
    Outcomes are filled in on later runs once the later closes exist, so day one can't be
    evaluated, and an H-day outcome appears H trading days after the signal day.

Prices are adjusted closes; the cache is overwritten on each fetch so a series stays internally
consistent after splits/dividends. Not financial advice.
"""
import bisect
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import technical_analysis as ta_mod

BENCHMARK = "SPY"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts   TEXT NOT NULL,
    run_date TEXT NOT NULL,
    sources  TEXT,
    weights  TEXT,
    params   TEXT
);
CREATE TABLE IF NOT EXISTS observations (
    run_id INTEGER NOT NULL, ticker TEXT NOT NULL,
    heat REAL, composite REAL, consensus TEXT, voters INTEGER, total_mentions INTEGER,
    c4_mentions INTEGER, c4_rank INTEGER, c4_bull INTEGER, c4_bear INTEGER, c4_net REAL,
    rd_mentions INTEGER, rd_rank INTEGER, rd_prev_mentions INTEGER, rd_upvotes INTEGER,
    st_rank INTEGER, st_net REAL, st_sentiment REAL, st_volume REAL, st_participation REAL,
    price REAL, price_date TEXT, sma_15 REAL, sma_30 REAL, sma_60 REAL, obv REAL,
    PRIMARY KEY (run_id, ticker)
);
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL, volume REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS smg_threads (
    thread_no INTEGER PRIMARY KEY, subject TEXT, first_seen TEXT, last_seen TEXT, gone INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS price_status (
    run_id INTEGER NOT NULL, ticker TEXT NOT NULL, status TEXT,
    PRIMARY KEY (run_id, ticker)
);
CREATE TABLE IF NOT EXISTS price_hl (
    symbol TEXT NOT NULL, date TEXT NOT NULL, high REAL, low REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS short_volume (
    symbol TEXT NOT NULL, date TEXT NOT NULL, short_vol REAL, exempt_vol REAL, total_vol REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS short_have (
    symbol TEXT NOT NULL, date TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS short_files (
    date TEXT PRIMARY KEY, status TEXT
);
CREATE TABLE IF NOT EXISTS outcomes_daily (
    run_date TEXT NOT NULL, ticker TEXT NOT NULL, horizon INTEGER NOT NULL,
    base_date TEXT, target_date TEXT, ret REAL, spy_ret REAL,
    PRIMARY KEY (run_date, ticker, horizon)
);
CREATE TABLE IF NOT EXISTS outcomes (
    run_id INTEGER NOT NULL, ticker TEXT NOT NULL, horizon INTEGER NOT NULL,
    base_date TEXT, target_date TEXT, ret REAL, spy_ret REAL,
    PRIMARY KEY (run_id, ticker, horizon)
);
"""

# NOTE: every run is kept. Statistics use one DAILY AVERAGE per (market date, ticker): see daily_observations().


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def connect(path="history.db"):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


# ----------------------------------------------------------------------------
# SAVING
# ----------------------------------------------------------------------------
def remember_threads(conn, threads, now=None):
    """Remember the live /smg/ thread IDs we saw, so a later run can still read them after they
    leave the catalog (a thread that fills up and is replaced between two runs would otherwise be
    lost - archived threads stay readable for a while)."""
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    for t in threads:
        conn.execute("INSERT OR IGNORE INTO smg_threads (thread_no, subject, first_seen, last_seen) "
                     "VALUES (?,?,?,?)", (t["no"], t["subject"], ts, ts))
        conn.execute("UPDATE smg_threads SET last_seen=?, subject=?, gone=0 WHERE thread_no=?",
                     (ts, t["subject"], t["no"]))
    conn.commit()


def recent_threads(conn, days=4, now=None):
    """[(thread_no, subject)] remembered within `days` days and not known to be gone."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(timespec="seconds")
    return conn.execute("SELECT thread_no, subject FROM smg_threads WHERE last_seen >= ? AND gone = 0 "
                        "ORDER BY thread_no", (cutoff,)).fetchall()


def mark_thread_gone(conn, thread_no):
    conn.execute("UPDATE smg_threads SET gone=1 WHERE thread_no=?", (thread_no,))
    conn.commit()


def save_price_status(conn, run_id, status):
    """Why each ticker does or doesn't have a price in this run: ok / cached / deferred /
    rate_limited / not_found / error / no_provider / not_requested / disabled."""
    conn.executemany("INSERT OR REPLACE INTO price_status VALUES (?,?,?)",
                     [(run_id, t, st) for t, st in status.items()])
    conn.commit()


def recent_not_found(conn, days=7):
    """Tickers the price provider reported as unknown within the last `days` days, so they aren't
    re-requested on every run (each request spends part of a small hourly allowance)."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return {t for (t,) in conn.execute(
        """SELECT DISTINCT ps.ticker FROM price_status ps JOIN runs r ON r.run_id = ps.run_id
           WHERE ps.status = 'not_found' AND r.run_date >= ?""", (cutoff,))}


def save_prices(conn, ta):
    """Upsert every fetched daily bar into the price cache."""
    n = 0
    for sym, res in (ta or {}).items():
        if not res or "error" in res:
            continue
        data = [(sym, b.date, b.close, b.volume) for b in res.get("bars", [])]
        conn.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?)", data)
        hl = [(sym, b.date, b.high, b.low) for b in res.get("bars", [])
              if b.high is not None and b.low is not None]
        if hl:
            conn.executemany("INSERT OR REPLACE INTO price_hl VALUES (?,?,?,?)", hl)
        n += len(data)
    conn.commit()
    return n


# ----------------------------------------------------------------------------
# RESOLVING OUTCOMES
# ----------------------------------------------------------------------------
def _series(conn, sym, cache):
    if sym not in cache:
        data = conn.execute("SELECT date, close FROM prices WHERE symbol=? ORDER BY date",
                            (sym,)).fetchall()
        dates = [d for d, _ in data]
        cache[sym] = (dates, [c for _, c in data], {d: i for i, d in enumerate(dates)})
    return cache[sym]


def refresh_prices(conn, symbols, provider="auto", max_fetch=20):
    """Fetch price history for extra tickers so their old signals can be scored."""
    got = {}
    for sym in symbols[:max_fetch]:
        try:
            got[sym] = {"bars": ta_mod.get_bars(sym, provider)}
        except ta_mod.RateLimited as e:
            _log(f"[history] {e}; leaving the rest pending")
            break
        except ta_mod.DataError as e:
            _log(f"[history] {sym}: {e}")
        time.sleep(0.5)
    save_prices(conn, got)
    return len(got)


# ----------------------------------------------------------------------------
# REPORTING
# ----------------------------------------------------------------------------
def _direction(val, dead_zone):
    if val is None:
        return 0
    return 1 if val >= dead_zone else -1 if val <= -dead_zone else 0


def _consensus_direction(consensus):
    if consensus and consensus.startswith("UNANIMOUS BULLISH"):
        return 1
    if consensus and consensus.startswith("UNANIMOUS BEARISH"):
        return -1
    return 0


def _pct(x):
    return "   -  " if x is None else f"{x:+.2%}"


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _pearson(x, y):
    n = len(x)
    if n < 5:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sxx * syy)


# ----------------------------------------------------------------------------
# PRICE CACHE REUSE (so repeat runs on the same day don't re-download everything)
# ----------------------------------------------------------------------------
def expected_last_session(now=None, close_hour=16, close_minute=30):
    """Date (YYYY-MM-DD) of the newest US trading session whose end-of-day bar should exist
    by `now`: today after ~16:30 New York time on a weekday, otherwise the previous weekday.
    Market holidays aren't known: on one this points at the holiday, so the cache simply won't
    match and prices are re-fetched (harmless, just extra requests)."""
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:   # e.g. Windows without the tzdata package
        et = now.astimezone(timezone(timedelta(hours=-5)))
    d = et.date()
    if d.weekday() >= 5 or (et.hour, et.minute) < (close_hour, close_minute):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def cached_bars(conn, symbol, expected_date, min_bars=61, keep=105):
    """Cached daily bars if the cache already contains the bar for `expected_date` (so a
    fetch could add nothing new) and enough history for a 60-day average; else None."""
    rows = conn.execute(
        """SELECT p.date, p.close, p.volume, h.high, h.low FROM prices p
           LEFT JOIN price_hl h ON h.symbol = p.symbol AND h.date = p.date
           WHERE p.symbol = ? ORDER BY p.date""", (symbol,)).fetchall()
    if len(rows) < min_bars or rows[-1][0] != expected_date:
        return None
    if any(r[3] is None or r[4] is None for r in rows[-21:]):
        return None      # no daily high/low saved yet: re-download once to backfill them
    return [ta_mod.Bar(d, c, v or 0.0, hi, lo) for d, c, v, hi, lo in rows[-keep:]]


# ----------------------------------------------------------------------------
# SOURCE SCORECARD (which sources have been right, and a cautious weight suggestion)
# ----------------------------------------------------------------------------
def _wilson(hits, n, z=1.96):
    """95% confidence range for a hit rate (Wilson score interval)."""
    if n == 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


# ----------------------------------------------------------------------------
# REDDIT ATTENTION STUDY
# ----------------------------------------------------------------------------


# ============================================================================
# DAILY MODEL: many runs per day, compared as one daily average
# ============================================================================
def _eastern(now):
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/New_York"))
    except Exception:   # e.g. Windows without the tzdata package
        return now.astimezone(timezone(timedelta(hours=-5)))


def market_date(now=None):
    """New York calendar date of `now`. Runs are grouped by this (not the UTC date), so an evening
    run in the US still belongs to the trading day it was made on."""
    return _eastern(now or datetime.now(timezone.utc)).date().isoformat()


def _dicts(cursor):
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def save_run(conn, rows, active, weights, ta, params=None, now=None):
    """Store one run (all report rows + their technical data). Returns run_id. Any number of runs
    per day may be saved; comparisons use the daily average (see daily_observations)."""
    now = now or datetime.now(timezone.utc)
    cur = conn.execute(
        "INSERT INTO runs (run_ts, run_date, sources, weights, params) VALUES (?,?,?,?,?)",
        (now.isoformat(timespec="seconds"), market_date(now), ",".join(active),
         json.dumps(weights), json.dumps(params or {}, default=str)))
    run_id = cur.lastrowid
    for r in rows:
        c4 = r["src"].get("4chan", {})
        rd = r["src"].get("reddit", {})
        st = r["src"].get("stocktwits", {})
        off = st.get("official") or {}
        t = (ta or {}).get(r["ticker"]) or {}
        if "error" in t:
            t = {}
        conn.execute(
            """INSERT OR REPLACE INTO observations VALUES
               (?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?,?,?,?)""",
            (run_id, r["ticker"],
             r["heat"], r["composite"], r["consensus"], r["voters"], r["total"],
             c4.get("mentions"), c4.get("rank"), c4.get("bull"), c4.get("bear"), c4.get("net"),
             rd.get("mentions"), rd.get("rank"), rd.get("prev"), rd.get("upvotes"),
             st.get("rank"), st.get("net"), off.get("sentiment"), off.get("volume"),
             off.get("participation"),
             t.get("price"), t.get("price_date"), t.get("sma_15"), t.get("sma_30"),
             t.get("sma_60"), t.get("obv")))
    conn.commit()
    return run_id


AVG_COLS = ("heat", "composite", "c4_net", "st_net", "st_sentiment", "st_volume", "st_participation",
            "total_mentions", "c4_mentions", "c4_rank", "c4_bull", "c4_bear", "rd_mentions", "rd_rank",
            "rd_prev_mentions", "rd_upvotes", "st_rank", "voters")
LAST_COLS = ("price", "price_date", "sma_15", "sma_30", "sma_60", "obv")


def consensus_label(c4_net, st_net, dead_zone=0.10):
    """Agreement between the sources that have a sentiment value (same rules as the live report)."""
    votes = {}
    for name, v in (("4ch", c4_net), ("stw", st_net)):
        if v is not None:
            votes[name] = "bull" if v >= dead_zone else "bear" if v <= -dead_zone else "flat"
    n = len(votes)
    if n == 0:
        return "n/a"
    if n == 1:
        return f"only {next(iter(votes))}"
    kinds = list(votes.values())
    top = max(set(kinds), key=kinds.count)
    k = kinds.count(top)
    word = {"bull": "BULLISH", "bear": "BEARISH", "flat": "NEUTRAL"}[top]
    if k == n:
        return f"UNANIMOUS {word} ({n}/{n})"
    if k > n / 2:
        return f"MAJORITY {word} ({k}/{n})"
    return "SPLIT (" + ", ".join(f"{s}:{v}" for s, v in votes.items()) + ")"


def daily_observations(conn):
    """
    {run_date: {ticker: obs}} - one record per ticker per market date. Numeric fields (sentiment,
    mentions, scores, heat...) are the MEAN over the runs of that date that saved the ticker; price
    fields are the latest non-empty value that day; `consensus` is recomputed from the averaged
    nets. Extra keys: n_runs (runs that included the ticker), run_id (the date's latest run).
    """
    cur = conn.execute("SELECT r.run_id AS run_id, r.run_date AS run_date, o.* FROM observations o "
                       "JOIN runs r ON r.run_id = o.run_id ORDER BY r.run_id")
    acc = {}
    for d in _dicts(cur):
        a = acc.setdefault((d["run_date"], d["ticker"]), {"vals": defaultdict(list), "last": {}, "n": 0})
        a["n"] += 1
        a["run_id"] = d["run_id"]
        for c in AVG_COLS:
            if d.get(c) is not None:
                a["vals"][c].append(d[c])
        for c in LAST_COLS:
            if d.get(c) is not None:
                a["last"][c] = d[c]
    out = {}
    for (run_date, ticker), a in acc.items():
        obs = {c: (sum(a["vals"][c]) / len(a["vals"][c]) if a["vals"][c] else None) for c in AVG_COLS}
        obs.update({c: a["last"].get(c) for c in LAST_COLS})
        obs.update(ticker=ticker, run_date=run_date, n_runs=a["n"], run_id=a["run_id"],
                   consensus=consensus_label(obs["c4_net"], obs["st_net"]))
        out.setdefault(run_date, {})[ticker] = obs
    return out


def _base_index(dates, run_date):
    """Index of the last bar dated on or before run_date = the session the day's signals belong to
    (a Saturday run -> Friday's bar)."""
    i = bisect.bisect_right(dates, run_date) - 1
    return i if i >= 0 else None


def resolve_outcomes(conn, horizons):
    """
    Fill in forward returns per (market date, ticker). The base price is the close of the last
    session on or before that date, so every run made on a day is judged against the same move
    (from that day's close to H trading days later). Nothing is scored until a LATER bar exists, so
    a morning run is never scored against the previous close. Returns the number added.
    """
    cache = {}
    pairs = conn.execute("SELECT DISTINCT r.run_date, o.ticker FROM observations o "
                         "JOIN runs r ON r.run_id = o.run_id").fetchall()
    done = {(a, b, c) for a, b, c in conn.execute("SELECT run_date, ticker, horizon FROM outcomes_daily")}
    spy_dates, spy_close, spy_idx = _series(conn, BENCHMARK, cache)
    added = 0
    for run_date, ticker in pairs:
        dates, closes, _ = _series(conn, ticker, cache)
        i0 = _base_index(dates, run_date)
        if i0 is None or i0 + 1 >= len(dates):
            continue
        base = dates[i0]
        for h in horizons:
            if (run_date, ticker, h) in done or i0 + h >= len(dates):
                continue
            target = dates[i0 + h]
            ret = closes[i0 + h] / closes[i0] - 1
            spy_ret = None
            if base in spy_idx and target in spy_idx:
                spy_ret = spy_close[spy_idx[target]] / spy_close[spy_idx[base]] - 1
            conn.execute("INSERT OR REPLACE INTO outcomes_daily VALUES (?,?,?,?,?,?,?)",
                         (run_date, ticker, h, base, target, ret, spy_ret))
            added += 1
    conn.commit()
    return added


def outcome_rows(conn):
    """Every scored outcome joined with that day's AVERAGED observation (a list of dicts with the
    observation fields plus horizon, base_date, target_date, ret, spy_ret)."""
    daily = daily_observations(conn)
    out = []
    for r in _dicts(conn.execute("SELECT run_date, ticker, horizon, base_date, target_date, ret, spy_ret "
                                 "FROM outcomes_daily")):
        o = daily.get(r["run_date"], {}).get(r["ticker"])
        if o is not None:
            out.append({**o, **r})
    return out


def tickers_needing_refresh(conn, horizons, have, max_age_days=14):
    """Previously-priced tickers from recent days whose longest-horizon outcome can't be computed
    from the cache yet and that we didn't just fetch (e.g. dropped off today's report)."""
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    rows = conn.execute("SELECT DISTINCT r.run_date, o.ticker FROM observations o "
                        "JOIN runs r ON r.run_id = o.run_id WHERE r.run_date >= ?", (cutoff,)).fetchall()
    skip = recent_not_found(conn)
    cache, need = {}, []
    for run_date, t in rows:
        if t in have or t in need or t in skip:
            continue
        dates, _, _ = _series(conn, t, cache)
        if not dates:
            continue            # never priced: the main price step handles it, not this
        i0 = _base_index(dates, run_date)
        if i0 is None or i0 + max(horizons) >= len(dates):
            need.append(t)
    return need


# ----------------------------------------------------------------------------
# REPORTS (daily averages)
# ----------------------------------------------------------------------------
def print_previous_run_check(conn, current_run_id, horizon=1, dead_zone=0.10, max_rows=25):
    """The latest EARLIER day with resolved results: that day's average sentiment vs what happened."""
    cur_date = conn.execute("SELECT run_date FROM runs WHERE run_id = ?", (current_run_id,)).fetchone()[0]
    rows = [r for r in outcome_rows(conn) if r["horizon"] == horizon and r["run_date"] < cur_date]
    print("\n" + "=" * 92)
    print(f"EARLIER CALLS vs WHAT HAPPENED ({horizon} trading day{'s' if horizon > 1 else ''} later)")
    print("=" * 92)
    if not rows:
        print("Nothing to check yet. This needs an earlier day plus a later trading day's close "
              "(so the first check appears the day after your first run, once the market has traded).")
        return
    day = max(r["run_date"] for r in rows)
    rows = sorted([r for r in rows if r["run_date"] == day],
                  key=lambda r: (-(r["total_mentions"] or 0), r["ticker"]))
    n_runs = max(r["n_runs"] for r in rows)
    print(f"{day}: average sentiment over that day's runs (up to {n_runs} per ticker) vs the move from the "
          f"{rows[0]['base_date']} close to the {rows[0]['target_date']} close")
    print(f"{'TICKER':<8}{'COMP':>6}  {'CONSENSUS':<28}{'RETURN':>8}{'vs SPY':>8}  RESULT")
    called = matched = ups = 0
    for i, r in enumerate(rows):
        comp, ret, spy = r["composite"], r["ret"], r["spy_ret"]
        d = _direction(comp, dead_zone)
        ups += ret > 0
        if d == 0:
            result = "neutral call" if comp is not None else "no sentiment"
        elif ret == 0:
            result = "flat"
        else:
            called += 1
            ok = (d > 0) == (ret > 0)
            matched += ok
            result = "matched" if ok else "missed"
        if i < max_rows:
            ex = None if spy is None else ret - spy
            print(f"{r['ticker']:<8}{('-' if comp is None else f'{comp:+.2f}'):>6}  {(r['consensus'] or '-')[:27]:<28}"
                  f"{_pct(ret):>8}{_pct(ex):>8}  {result}")
    if len(rows) > max_rows:
        print(f"... and {len(rows) - max_rows} more (all are counted in the totals below)")
    print("-" * 92)
    if called:
        print(f"Directional calls (composite): {matched} of {called} matched ({matched / called:.0%}). "
              f"For reference, {ups / len(rows):.0%} of the {len(rows)} tickers rose, so a blind "
              f"'always bullish' call would have matched about that often.")
    else:
        print("No directional (non-neutral) composite calls that day.")
    print("One day of data says very little - see the cumulative BACKTEST below as it grows.")


def _consensus_dir(consensus):
    return _consensus_direction(consensus)


def print_backtest(conn, horizons, dead_zone=0.10):
    data = outcome_rows(conn)
    n_days = conn.execute("SELECT COUNT(DISTINCT run_date) FROM runs").fetchone()[0]
    n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    print("\n" + "=" * 92)
    print(f"BACKTEST - does sentiment line up with later returns?   ({n_days} day(s), {n_runs} run(s) saved; "
          f"{len(data)} scored outcomes)")
    print("Each day's sentiment is the AVERAGE of that day's runs.")
    print("=" * 92)
    if not data:
        print("No resolved outcomes yet.")
        return
    for h in horizons:
        sub = [d for d in data if d["horizon"] == h]
        if not sub:
            continue
        ups = sum(1 for d in sub if d["ret"] > 0)
        print(f"\nHorizon: {h} trading day{'s' if h > 1 else ''}   "
              f"(all {len(sub)} observations: {ups / len(sub):.0%} rose)")
        print(f"  {'PREDICTOR':<12}{'CALLS':>6}{'HIT%':>7}{'HIT% vs SPY':>13}"
              f"{'AVG RET BULL':>14}{'AVG RET BEAR':>14}{'CORR':>7}")
        preds = [("composite", lambda d: _direction(d["composite"], dead_zone), "composite"),
                 ("4chan", lambda d: _direction(d["c4_net"], dead_zone), "c4_net"),
                 ("stocktwits", lambda d: _direction(d["st_net"], dead_zone), "st_net"),
                 ("unanimous", lambda d: _consensus_dir(d["consensus"]), None)]
        for name, dirf, col in preds:
            calls = [(dirf(d), d["ret"], d["spy_ret"]) for d in sub if dirf(d) != 0]
            hits = [(dr > 0) == (ret > 0) for dr, ret, _ in calls if ret != 0]
            ex = [(dr, ret - spy) for dr, ret, spy in calls if spy is not None and ret - spy != 0]
            ex_hits = [(dr > 0) == (e > 0) for dr, e in ex]
            bull = [ret for dr, ret, _ in calls if dr > 0]
            bear = [ret for dr, ret, _ in calls if dr < 0]
            corr = None
            if col:
                pairs = [(d[col], d["ret"]) for d in sub if d[col] is not None]
                corr = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
            hit = f"{sum(hits) / len(hits):.0%}" if hits else "-"
            xhit = f"{sum(ex_hits) / len(ex_hits):.0%}" if ex_hits else "-"
            print(f"  {name:<12}{len(calls):>6}{hit:>7}{xhit:>13}{_pct(_mean(bull)):>14}"
                  f"{_pct(_mean(bear)):>14}{('-' if corr is None else f'{corr:+.2f}'):>7}")
    print("\nHIT% = share of bullish/bearish calls that matched the stock's direction (neutral calls "
          "skipped). HIT% vs SPY uses return minus SPY's, to strip out market-wide days.")
    print("AVG RET BULL/BEAR = average later return of stocks called bullish / bearish "
          "(a useful signal has BULL > 0 and BEAR < 0). CORR = correlation of NET with return.")
    print("Compare HIT% with the 'rose' share above: in a rising market, 'always bullish' also wins. "
          "With under ~30 calls per row these numbers are noise; patterns also overlap across "
          "tickers and days, so treat them as hints, not proof. Not financial advice.")


def print_source_scorecard(conn, horizon, dead_zone, weights, min_calls=50, recent_days=10):
    """Per-source accuracy on DAILY-average sentiment, judged on return MINUS SPY's, with a confidence
    range so luck isn't mistaken for skill. Suggests weights only when there is enough data."""
    data = [d for d in outcome_rows(conn) if d["horizon"] == horizon]
    print("\n" + "=" * 92)
    print(f"SOURCE SCORECARD ({horizon} trading day{'s' if horizon > 1 else ''} ahead, "
          f"direction judged against SPY)")
    print("=" * 92)
    if not data:
        print("No resolved outcomes yet.")
        return
    dates = sorted({d["run_date"] for d in data})
    recent = set(dates[-recent_days:])
    print(f"{'SOURCE':<12}{'CALLS':>6}{'HIT%':>7}{'95% RANGE':>14}{f'LAST {recent_days}D':>10}  VERDICT")
    stats = {}
    for name, col in (("4chan", "c4_net"), ("stocktwits", "st_net"), ("composite", "composite")):
        calls = []
        for row in data:
            d, spy = _direction(row[col], dead_zone), row["spy_ret"]
            if d == 0 or spy is None or row["ret"] - spy == 0:
                continue
            calls.append((row["run_date"], (d > 0) == (row["ret"] - spy > 0)))
        n, hits = len(calls), sum(h for _, h in calls)
        stats[name] = (n, hits)
        if n == 0:
            print(f"{name:<12}{0:>6}{'-':>7}{'-':>14}{'-':>10}  no directional calls yet")
            continue
        lo, hi = _wilson(hits, n)
        rec = [h for dt, h in calls if dt in recent]
        rec_s = f"{sum(rec) / len(rec):.0%}" if rec else "-"
        if n < min_calls:
            verdict = f"too few calls (want {min_calls}+)"
        elif lo > 0.5:
            verdict = "edge above chance"
        elif hi < 0.5:
            verdict = "worse than chance"
        else:
            verdict = "no clear edge"
        print(f"{name:<12}{n:>6}{hits / n:>7.0%}{f'{lo:.0%}-{hi:.0%}':>14}{rec_s:>10}  {verdict}")
    print("  A 95% range that includes 50% means the result could be luck.")
    if all(stats[s][0] >= min_calls for s in ("4chan", "stocktwits")):
        k = 30
        edge = {s: max((stats[s][1] + k / 2) / (stats[s][0] + k) - 0.5, 0.01) for s in ("4chan", "stocktwits")}
        total = weights["4chan"] + weights["stocktwits"]
        proposed = {s: total * edge[s] / sum(edge.values()) for s in edge}
        new = {s: round(0.5 * weights[s] + 0.5 * proposed[s], 2) for s in edge}
        print(f"\n  Suggested starting point (NOT applied): --weights 4chan={new['4chan']:g} "
              f"reddit={weights['reddit']:g} stocktwits={new['stocktwits']:g}")
        print("  Built from accuracy shrunk toward 50%, then moved only halfway from your current "
              "weights. Reddit's weight is unchanged (it has no sentiment). Past accuracy often "
              "doesn't persist - treat as a nudge and re-check after more data.")
    else:
        need = max(min_calls - min(stats["4chan"][0], stats["stocktwits"][0]), 0)
        print(f"\n  Weight suggestions start once 4chan and stocktwits each have {min_calls}+ "
              f"calls (at least {need} more on the thinner one).")


def print_attention_study(conn, horizons, dead_zone=0.10):
    """Does a change in r/wallstreetbets attention come before different moves? Uses daily-average
    mentions; attention has no direction, so we look at return, return vs SPY, share up, and |return|."""
    data = [d for d in outcome_rows(conn)
            if d["rd_mentions"] and d["rd_prev_mentions"] and d["rd_mentions"] > 0 and d["rd_prev_mentions"] > 0]
    print("\n" + "=" * 92)
    print("REDDIT ATTENTION vs LATER MOVES (change in r/wsb mentions vs the prior 24h, daily average)")
    print("=" * 92)
    if not data:
        print("No resolved outcomes with Reddit data yet.")
        return
    buckets = [
        ("all tickers with Reddit data", lambda c, comp: True),
        ("surging (>= +50%)",            lambda c, comp: c >= 0.50),
        ("rising (+10% to +50%)",        lambda c, comp: 0.10 <= c < 0.50),
        ("steady (-10% to +10%)",        lambda c, comp: -0.10 < c < 0.10),
        ("fading (<= -10%)",             lambda c, comp: c <= -0.10),
        ("surging + bullish composite",  lambda c, comp: c >= 0.50 and _direction(comp, dead_zone) > 0),
        ("surging + bearish composite",  lambda c, comp: c >= 0.50 and _direction(comp, dead_zone) < 0),
    ]
    for h in horizons:
        sub = [(d["ret"], d["spy_ret"], d["composite"], d["rd_mentions"] / d["rd_prev_mentions"] - 1)
               for d in data if d["horizon"] == h]
        if not sub:
            continue
        print(f"\nHorizon: {h} trading day{'s' if h > 1 else ''}")
        print(f"  {'BUCKET':<32}{'N':>5}{'% UP':>7}{'AVG RET':>10}{'vs SPY':>9}{'AVG |RET|':>11}")
        for name, pred in buckets:
            rows = [(ret, spy) for ret, spy, comp, chg in sub if pred(chg, comp)]
            if not rows:
                print(f"  {name:<32}{0:>5}")
                continue
            ex = [ret - spy for ret, spy in rows if spy is not None]
            print(f"  {name:<32}{len(rows):>5}{sum(1 for r, _ in rows if r > 0) / len(rows):>7.0%}"
                  f"{_pct(_mean([r for r, _ in rows])):>10}{_pct(_mean(ex)):>9}"
                  f"{abs(_mean([abs(r) for r, _ in rows])):>11.2%}")
    print("\nAVG |RET| ignores direction: if surging attention shows a bigger number than the "
          "'all tickers' row, attention is flagging bigger moves, not necessarily up ones. Compare each "
          "bucket with the 'all' row, and ignore buckets with only a handful of observations.")
