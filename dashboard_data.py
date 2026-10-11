"""
dashboard_data.py - Read-only queries over history.db that return plain Python data
(lists / dicts / numbers). No HTML and no charting in here.

This is the layer both front-ends share: dashboard.py (static HTML) uses it today, and a
Streamlit app can import the same functions later and just draw the results differently.

Conventions
    * "run day": one entry per market (New York) date. Several runs may be saved per day; every
      per-day number is the AVERAGE of that day's runs (history.daily_observations).
    * Missing values are None (a ticker that wasn't in that day's report, no price, ...).
    * Sentiment ("NET", "composite") is on a -1..+1 scale; see history.py / combined_report.py.
"""
import json
import math
import os
import sqlite3
from pathlib import Path

import flow
import history
from history import _consensus_direction, _direction, _mean, _pearson, _wilson

DEAD_ZONE = 0.10


# ----------------------------------------------------------------------------
# connection / runs / observations
# ----------------------------------------------------------------------------
def connect(path):
    """Open the database read-only (never creates or modifies anything)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"No database at {path!r}. Run combined_report.py first "
                                f"(or make_demo_db.py for sample data).")
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def is_demo(runs):
    return bool(runs) and bool(runs[0]["params"].get("demo"))


# ----------------------------------------------------------------------------
# leaderboard + what changed
# ----------------------------------------------------------------------------
def _pct_diff(a, b):
    return None if a is None or not b else a / b - 1


def leaderboard(latest_obs, window_obs):
    """
    Rows for the newest run day, sorted by heat. `window_obs` is a list of {ticker: obs} for the
    run days shown in the trend sparklines (oldest -> newest, ending with the latest run).
    """
    rows = []
    for t, o in latest_obs.items():
        rd_chg = _pct_diff(o["rd_mentions"], o["rd_prev_mentions"]) if o["rd_mentions"] else None
        rows.append({
            "ticker": t, "heat": o["heat"], "composite": o["composite"], "consensus": o["consensus"],
            "c4_mentions": o["c4_mentions"], "rd_mentions": o["rd_mentions"], "rd_chg": rd_chg,
            "c4_net": o["c4_net"], "st_sentiment": o["st_sentiment"], "st_net": o["st_net"],
            "price": o["price"], "price_date": o["price_date"],
            "vs_sma15": _pct_diff(o["price"], o["sma_15"]),
            "vs_sma30": _pct_diff(o["price"], o["sma_30"]),
            "vs_sma60": _pct_diff(o["price"], o["sma_60"]),
            "spark_comp": [w.get(t, {}).get("composite") for w in window_obs],
            "spark_rd": [w.get(t, {}).get("rd_mentions") for w in window_obs],
        })
    rows.sort(key=lambda r: (-(r["heat"] or 0), r["ticker"]))
    return rows


def changes(latest_obs, prev_obs, dead_zone=DEAD_ZONE):
    """What's different from the previous run day (all empty if there is no previous run)."""
    if prev_obs is None:
        return None
    new = sorted(set(latest_obs) - set(prev_obs), key=lambda t: -(latest_obs[t]["heat"] or 0))
    dropped = sorted(set(prev_obs) - set(latest_obs), key=lambda t: -(prev_obs[t]["heat"] or 0))
    flips, moves = [], []
    for t in set(latest_obs) & set(prev_obs):
        a, b = prev_obs[t]["composite"], latest_obs[t]["composite"]
        if a is None or b is None:
            continue
        moves.append((t, a, b, b - a))
        if _direction(a, dead_zone) * _direction(b, dead_zone) < 0:
            flips.append((t, a, b))
    jumps = []
    for t, o in latest_obs.items():
        chg = _pct_diff(o["rd_mentions"], o["rd_prev_mentions"]) if o["rd_mentions"] else None
        if chg is not None and o["rd_mentions"] >= 20:
            jumps.append((t, o["rd_mentions"], chg))
    return {
        "new": new[:12], "dropped": dropped[:12],
        "flips": sorted(flips, key=lambda x: -abs(x[2] - x[1]))[:10],
        "biggest_moves": sorted(moves, key=lambda x: -abs(x[3]))[:8],
        "reddit_jumps": sorted(jumps, key=lambda x: -x[2])[:8],
    }


# ----------------------------------------------------------------------------
# per-ticker detail
# ----------------------------------------------------------------------------
def ticker_history(runs, obs_by_run, ticker):
    """Per run day (aligned with `runs`): that day's observation fields, or Nones."""
    keys = ("composite", "c4_net", "st_net", "st_sentiment", "c4_mentions", "rd_mentions",
            "heat", "consensus")
    out = []
    for r in runs:
        o = obs_by_run.get(r["run_id"], {}).get(ticker)
        out.append({"date": r["run_date"], **{k: (o[k] if o else None) for k in keys}})
    return out


def _rolling_sma(closes, n):
    out = []
    for i in range(len(closes)):
        out.append(sum(closes[i - n + 1:i + 1]) / n if i >= n - 1 else None)
    return out


def price_history(conn, ticker, bars=90):
    """Last `bars` daily closes with SMA 15/30/60 and OBV. SMAs use earlier history so they
    are already 'warm' at the left edge; OBV starts at 0 at the first displayed bar."""
    rows = conn.execute("SELECT date, close, volume FROM prices WHERE symbol=? "
                        "ORDER BY date DESC LIMIT ?", (ticker, bars + 59)).fetchall()
    rows = list(reversed(rows))
    if not rows:
        return None
    closes = [r["close"] for r in rows]
    vols = [r["volume"] or 0.0 for r in rows]
    smas = {n: _rolling_sma(closes, n) for n in (15, 30, 60)}
    cut = max(0, len(rows) - bars)
    obv, total = [], 0.0
    for i in range(cut, len(rows)):
        if i > cut:
            if closes[i] > closes[i - 1]:
                total += vols[i]
            elif closes[i] < closes[i - 1]:
                total -= vols[i]
        obv.append(total)
    return {"dates": [r["date"] for r in rows[cut:]], "close": closes[cut:],
            "sma15": smas[15][cut:], "sma30": smas[30][cut:], "sma60": smas[60][cut:], "obv": obv}


# ----------------------------------------------------------------------------
# heatmaps
# ----------------------------------------------------------------------------
def heatmap(runs, obs_by_run, tickers, key):
    """[(ticker, [value per run day or None])] for observation field `key`."""
    return [(t, [obs_by_run.get(r["run_id"], {}).get(t, {}).get(key) for r in runs]) for t in tickers]


# ----------------------------------------------------------------------------
# does sentiment line up with later returns? (same logic as history.print_*)
# ----------------------------------------------------------------------------
def backtest(conn, horizons, dead_zone=DEAD_ZONE):
    """{horizon: {"n", "up_rate", "rows": [predictor stats...]}} """
    data = _outcome_rows(conn)
    out = {}
    for h in horizons:
        sub = [d for d in data if d["horizon"] == h]
        if not sub:
            continue
        preds = [("composite", lambda d: _direction(d["composite"], dead_zone), "composite"),
                 ("4chan", lambda d: _direction(d["c4_net"], dead_zone), "c4_net"),
                 ("stocktwits", lambda d: _direction(d["st_net"], dead_zone), "st_net"),
                 ("unanimous", lambda d: _consensus_direction(d["consensus"]), None)]
        rows = []
        for name, dirf, col in preds:
            calls = [(dirf(d), d["ret"], d["spy_ret"]) for d in sub if dirf(d) != 0]
            hits = [(dr > 0) == (ret > 0) for dr, ret, _ in calls if ret != 0]
            ex = [(dr > 0) == (ret - spy > 0) for dr, ret, spy in calls
                  if spy is not None and ret - spy != 0]
            corr = None
            if col:
                pairs = [(d[col], d["ret"]) for d in sub if d[col] is not None]
                corr = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
            rows.append({
                "name": name, "calls": len(calls),
                "hit": sum(hits) / len(hits) if hits else None,
                "hit_ex": sum(ex) / len(ex) if ex else None,
                "avg_bull": _mean([ret for dr, ret, _ in calls if dr > 0]),
                "avg_bear": _mean([ret for dr, ret, _ in calls if dr < 0]),
                "corr": corr})
        out[h] = {"n": len(sub), "up_rate": sum(1 for d in sub if d["ret"] > 0) / len(sub),
                  "rows": rows}
    return out


def scorecard(conn, horizon, dead_zone=DEAD_ZONE, min_calls=50, recent_days=10):
    """Per-source hit rate vs SPY with a 95% Wilson range, plus the last `recent_days` run days."""
    data = [d for d in _outcome_rows(conn) if d["horizon"] == horizon]
    dates = sorted({d["run_date"] for d in data})
    recent = set(dates[-recent_days:])
    out = []
    for name, col in (("4chan", "c4_net"), ("stocktwits", "st_net"), ("composite", "composite")):
        calls = []
        for d in data:
            dr, spy = _direction(d[col], dead_zone), d["spy_ret"]
            if dr == 0 or spy is None or d["ret"] - spy == 0:
                continue
            calls.append((d["run_date"], (dr > 0) == (d["ret"] - spy > 0)))
        n, hits = len(calls), sum(h for _, h in calls)
        rec = [h for dt, h in calls if dt in recent]
        ci = _wilson(hits, n)
        if n < min_calls:
            verdict = f"too few calls (want {min_calls}+)"
        elif ci[0] > 0.5:
            verdict = "edge above chance"
        elif ci[1] < 0.5:
            verdict = "worse than chance"
        else:
            verdict = "no clear edge"
        out.append({"name": name, "calls": n, "hit": hits / n if n else None,
                    "lo": ci[0] if ci else None, "hi": ci[1] if ci else None,
                    "recent": sum(rec) / len(rec) if rec else None,
                    "verdict": verdict if n else "no directional calls yet"})
    return out


ATTENTION_BUCKETS = [
    ("all tickers with Reddit data", lambda c, comp: True),
    ("surging (>= +50%)", lambda c, comp: c >= 0.50),
    ("rising (+10% to +50%)", lambda c, comp: 0.10 <= c < 0.50),
    ("steady (-10% to +10%)", lambda c, comp: -0.10 < c < 0.10),
    ("fading (<= -10%)", lambda c, comp: c <= -0.10),
    ("surging + bullish composite", lambda c, comp: c >= 0.50 and _direction(comp, DEAD_ZONE) > 0),
    ("surging + bearish composite", lambda c, comp: c >= 0.50 and _direction(comp, DEAD_ZONE) < 0),
]


def attention_study(conn, horizons):
    """{horizon: [bucket stats]} - later returns bucketed by the change in r/wsb mentions."""
    data = [d for d in _outcome_rows(conn)
            if d["rd_mentions"] and d["rd_prev_mentions"] and d["rd_mentions"] > 0
            and d["rd_prev_mentions"] > 0]
    out = {}
    for h in horizons:
        sub = [(d["ret"], d["spy_ret"], d["composite"], d["rd_mentions"] / d["rd_prev_mentions"] - 1)
               for d in data if d["horizon"] == h]
        if not sub:
            continue
        rows = []
        for name, pred in ATTENTION_BUCKETS:
            sel = [(ret, spy) for ret, spy, comp, chg in sub if pred(chg, comp)]
            ex = [ret - spy for ret, spy in sel if spy is not None]
            rows.append({"name": name, "n": len(sel),
                         "up": sum(1 for r, _ in sel if r > 0) / len(sel) if sel else None,
                         "avg_ret": _mean([r for r, _ in sel]), "avg_ex": _mean(ex),
                         "avg_abs": _mean([abs(r) for r, _ in sel])})
        out[h] = rows
    return out


# ----------------------------------------------------------------------------
# small helpers shared by front-ends
# ----------------------------------------------------------------------------
def short_consensus(text):
    """'UNANIMOUS BULLISH (2/2)' -> 'unanimous bull', 'SPLIT (4ch:bull, stw:bear)' -> 'split' ..."""
    if not text or text == "n/a":
        return "-"
    t = text.upper()
    kind = "unanimous" if t.startswith("UNANIMOUS") else "majority" if t.startswith("MAJORITY") \
        else "split" if t.startswith("SPLIT") else "only one source"
    if kind in ("unanimous", "majority"):
        direction = "bull" if "BULLISH" in t else "bear" if "BEARISH" in t else "neutral"
        return f"{kind} {direction}"
    return kind


def log_scale(value, vmax):
    """0..1 intensity on a log scale (for attention heatmaps)."""
    if not value or value <= 0 or not vmax:
        return 0.0
    return math.log1p(value) / math.log1p(vmax)


# ----------------------------------------------------------------------------
# buying / selling pressure proxies (see flow.py for what each one means)
# ----------------------------------------------------------------------------
def flow_latest(conn, tickers):
    """{ticker: metrics} (updown_20, cmf_20, short_1d, short_5d, short_20d, short_date)."""
    return flow.latest_metrics(conn, tickers)


def flow_charts(conn, ticker, bars=90):
    """Series for one ticker's pressure charts: rolling CMF / up-volume share, and daily short ratio."""
    fs = flow.FlowSeries(conn, ticker)
    return {"rolling": fs.rolling(bars), "short": fs.short_series(60), "metrics": fs.metrics()}


def _bucket_stats(sel):
    """sel: [(ret, spy_ret)] -> n, share up, avg return, avg return vs SPY, avg |return|."""
    ex = [r - sp for r, sp in sel if sp is not None]
    return {"n": len(sel),
            "up": sum(1 for r, _ in sel if r > 0) / len(sel) if sel else None,
            "avg_ret": _mean([r for r, _ in sel]), "avg_ex": _mean(ex),
            "avg_abs": _mean([abs(r) for r, _ in sel])}


def flow_backtest(conn, horizons):
    """
    Do the pressure proxies line up with later returns? Each outcome is paired with the metrics
    as they stood on the signal's base date (no later data).
      pressure: CMF and up-volume share as directional calls (like the sentiment backtest)
      short:    buckets by the FINRA short ratio's 5-day average vs the ticker's own 20-day average
    """
    out_rows = history.outcome_rows(conn)
    series, cache = {}, {}

    def metrics(t, base):
        if (t, base) not in cache:
            if t not in series:
                series[t] = flow.FlowSeries(conn, t)
            cache[t, base] = series[t].metrics(base)
        return cache[t, base]

    out = {}
    for h in horizons:
        data = [(r["ret"], r["spy_ret"], metrics(r["ticker"], r["base_date"]))
                for r in out_rows if r["horizon"] == h]
        if not data:
            continue
        pressure = []
        for name, getter, thr in (("Chaikin Money Flow (20d)", lambda m: m["cmf_20"], 0.05),
                                  ("Up-volume share (20d)",
                                   lambda m: None if m["updown_20"] is None else m["updown_20"] - 0.5, 0.05)):
            vals = [(getter(m), ret, spy) for ret, spy, m in data if getter(m) is not None]
            calls = [((1 if v >= thr else -1), ret, spy) for v, ret, spy in vals if abs(v) >= thr]
            hits = [(d > 0) == (ret > 0) for d, ret, _ in calls if ret != 0]
            ex = [(d > 0) == (ret - spy > 0) for d, ret, spy in calls if spy is not None and ret - spy != 0]
            pressure.append({
                "name": name, "calls": len(calls),
                "hit": sum(hits) / len(hits) if hits else None,
                "hit_ex": sum(ex) / len(ex) if ex else None,
                "avg_bull": _mean([ret for d, ret, _ in calls if d > 0]),
                "avg_bear": _mean([ret for d, ret, _ in calls if d < 0]),
                "corr": _pearson([v for v, _, _ in vals], [ret for _, ret, _ in vals])})
        sh = [(ret, spy, m["short_5d"] - m["short_20d"], m["short_5d"]) for ret, spy, m in data
              if m["short_5d"] is not None and m["short_20d"] is not None]
        buckets = [("all with short-sale data", lambda dev, lvl: True),
                   ("5d ratio above its 20d avg by 3+ pts", lambda dev, lvl: dev >= 0.03),
                   ("steady (within 3 pts)", lambda dev, lvl: -0.03 < dev < 0.03),
                   ("5d ratio below its 20d avg by 3+ pts", lambda dev, lvl: dev <= -0.03),
                   ("high level (5d ratio >= 55%)", lambda dev, lvl: lvl >= 0.55),
                   ("low level (5d ratio <= 35%)", lambda dev, lvl: lvl <= 0.35)]
        short = [{"name": name, **_bucket_stats([(ret, spy) for ret, spy, dev, lvl in sh if pred(dev, lvl)])}
                 for name, pred in buckets]
        out[h] = {"n": len(data), "pressure": pressure, "short": short, "short_n": len(sh)}
    return out


def price_status(conn, run_id):
    """{ticker: status} for a run (why a ticker has or lacks a price). {} for databases saved
    before this was tracked."""
    try:
        return {r["ticker"]: r["status"] for r in
                conn.execute("SELECT ticker, status FROM price_status WHERE run_id = ?", (run_id,))}
    except sqlite3.OperationalError:
        return {}


# ----------------------------------------------------------------------------
# run days (many runs per day, shown as one daily average)
# ----------------------------------------------------------------------------
def run_days(conn):
    """One entry per market date, oldest -> newest: {run_id (the day's LATEST run), run_date, run_ts,
    sources, params, n_runs (runs saved that day)}."""
    by_date = {}
    for r in conn.execute("SELECT run_id, run_date, run_ts, sources, params FROM runs ORDER BY run_id"):
        by_date.setdefault(r["run_date"], []).append(dict(r))
    out = []
    for run_date, runs in sorted(by_date.items()):
        last = runs[-1]
        try:
            params = json.loads(last["params"] or "{}")
        except ValueError:
            params = {}
        out.append({"run_id": last["run_id"], "run_date": run_date, "run_ts": last["run_ts"],
                    "sources": last["sources"], "params": params, "n_runs": len(runs)})
    return out


def daily_all(conn):
    """{run_date: {ticker: daily-average observation}} for every saved day."""
    return history.daily_observations(conn)


def observations_by_run(conn, runs, daily=None):
    """{run_id: {ticker: daily-average observation}} keyed by each day's latest run id, so callers can
    keep treating a 'run day' as one run."""
    daily = daily if daily is not None else history.daily_observations(conn)
    return {r["run_id"]: daily.get(r["run_date"], {}) for r in runs}


def database_summary(conn, runs):
    n_prices = conn.execute("SELECT COUNT(DISTINCT symbol) FROM prices").fetchone()[0]
    n_out = conn.execute("SELECT COUNT(*) FROM outcomes_daily").fetchone()[0]
    return {"run_days": len(runs), "total_runs": sum(r["n_runs"] for r in runs),
            "first_date": runs[0]["run_date"] if runs else None,
            "last_date": runs[-1]["run_date"] if runs else None,
            "priced_symbols": n_prices, "scored_outcomes": n_out}


def ticker_signals(conn, ticker, limit=12, daily=None):
    """Recent days for a ticker: that day's average composite and consensus, with the outcomes that
    have resolved so far (return and SPY return per horizon)."""
    daily = daily if daily is not None else history.daily_observations(conn)
    outs = {}
    for r in conn.execute("SELECT run_date, horizon, ret, spy_ret FROM outcomes_daily WHERE ticker = ?", (ticker,)):
        outs.setdefault(r["run_date"], {})[r["horizon"]] = (r["ret"], r["spy_ret"])
    rows = []
    for run_date in sorted(daily, reverse=True):
        o = daily[run_date].get(ticker)
        if o is None:
            continue
        o_h = outs.get(run_date, {})
        rows.append({"date": run_date, "composite": o["composite"], "consensus": o["consensus"],
                     "n_runs": o["n_runs"],
                     "ret": {h: v[0] for h, v in o_h.items()}, "spy": {h: v[1] for h, v in o_h.items()}})
        if len(rows) >= limit:
            break
    return rows


def ticker_intraday(conn, ticker, days=3):
    """Every saved run on the last `days` run dates, oldest -> newest (for the intraday chart)."""
    dates = [r[0] for r in conn.execute("SELECT DISTINCT run_date FROM runs ORDER BY run_date DESC LIMIT ?", (days,))]
    if not dates:
        return []
    q = ("SELECT r.run_ts, r.run_date, o.composite, o.c4_net, o.st_net, o.rd_mentions FROM observations o "
         "JOIN runs r ON r.run_id = o.run_id WHERE o.ticker = ? AND r.run_date IN (%s) ORDER BY r.run_id"
         % ",".join("?" * len(dates)))
    return [dict(r) for r in conn.execute(q, [ticker] + dates)]


# ----------------------------------------------------------------------------
# scored outcomes (joined with each day's average observation)
# ----------------------------------------------------------------------------
def _outcome_rows(conn):
    return history.outcome_rows(conn)


def horizons_available(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT horizon FROM outcomes_daily ORDER BY horizon")]
