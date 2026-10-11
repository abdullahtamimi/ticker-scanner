#!/usr/bin/env python3
"""
make_demo_db.py - Create demo_history.db filled with SYNTHETIC data, so you can preview the
dashboard before you have weeks of real history:

    python make_demo_db.py
    python dashboard.py --db demo_history.db --out demo.html

Everything here is invented (random walks; a planted 4chan 'edge' so the backtest has something
to find). The dashboard shows a red SYNTHETIC DEMO DATA banner for this database.
"""
import argparse
import os
import random
from datetime import datetime, timedelta, timezone

import history
import technical_analysis as ta
from technical_analysis import Bar

TICKERS = ("NVDA TSLA AMD PLTR SMCI AAPL MSFT AMZN META GOOGL NFLX COIN MSTR HOOD SOFI RIVN LCID NIO "
           "BABA INTC MU AVGO TSM SOXL TQQQ SPY QQQ GME AMC DIS BA UBER SNOW CRWD SHOP PYPL ARM RBLX CVNA").split()


def weekdays_ending(n):
    d, out = datetime.now().date(), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return list(reversed(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="demo_history.db")
    ap.add_argument("--days", type=int, default=24, help="run days to simulate")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    rnd = random.Random(args.seed)
    if os.path.exists(args.out):
        os.remove(args.out)

    n_total = args.days + 140
    dates = weekdays_ending(n_total)
    run_idx = list(range(n_total - args.days, n_total))
    pop = {t: rnd.lognormvariate(4.2, 0.9) for t in TICKERS}
    vol = {t: rnd.uniform(0.012, 0.03) for t in TICKERS}
    mkt = [rnd.gauss(0.0004, 0.007) for _ in range(n_total)]

    # latent per-day signals (only on run days); the 4chan one genuinely leads returns, StockTwits doesn't
    sig4, sigs, chg = {}, {}, {}
    for t in TICKERS:
        for i in run_idx:
            sig4[t, i] = max(-1, min(1, rnd.gauss(0, 0.6)))
            sigs[t, i] = max(-1, min(1, rnd.gauss(0.1, 0.55)))
            chg[t, i] = rnd.uniform(-0.5, 1.6)

    # buying pressure (closes near the high) and short-sale deviation, for every day;
    # both genuinely, weakly, lead next-day returns (a planted edge for the backtest to find)
    pres = {t: [max(-1.0, min(1.0, rnd.gauss(0, 0.8))) for _ in range(n_total)] for t in TICKERS}
    sdev = {}
    for t in TICKERS:
        x, arr = 0.0, []
        for _ in range(n_total):
            x = 0.6 * x + 0.8 * rnd.gauss(0, 1)
            arr.append(x)
        sdev[t] = arr
    closes = {t: [rnd.uniform(20, 400)] for t in TICKERS}
    spy = [450.0]
    for i in range(1, n_total):
        spy.append(spy[-1] * (1 + mkt[i]))
        for t in TICKERS:
            prev_i = i - 1
            edge = 0.011 * sig4[t, prev_i] if (t, prev_i) in sig4 else 0.0
            surge = 1 + 1.6 * (chg.get((t, prev_i), 0) >= 0.5)
            flow_edge = 0.004 * pres[t][prev_i] - 0.003 * sdev[t][prev_i]
            closes[t].append(closes[t][-1] * (1 + mkt[i] + edge + flow_edge + rnd.gauss(0, vol[t] * surge)))
    def make_bar(d, c, v, p):
        # high/low placed so that days with positive pressure close nearer the high
        return Bar(d, c, v, c * (1 + rnd.uniform(0, 0.02) * (1 - 0.6 * p)),
                   c * (1 - rnd.uniform(0, 0.02) * (1 + 0.6 * p)))

    bars = {t: [make_bar(d, c, rnd.uniform(1e6, 5e7), pres[t][k])
                for k, (d, c) in enumerate(zip(dates, closes[t]))] for t in TICKERS}
    bars["SPY"] = [make_bar(d, c, rnd.uniform(3e7, 9e7), 0.0) for d, c in zip(dates, spy)]

    conn = history.connect(args.out)
    weights = {"4chan": 0.5, "reddit": 1.0, "stocktwits": 1.0}
    prev_rd = {}
    for k, i in enumerate(run_idx):
        # which tickers made today's report (popular ones nearly always, others sometimes)
        today = [t for t in TICKERS if rnd.random() < min(0.97, 0.25 + pop[t] / 120)]
        today = today[: rnd.randint(22, 30)] or TICKERS[:25]
        rd = {t: max(5, int(pop[t] * (1 + chg[t, i]) * rnd.uniform(0.8, 1.2) * 4)) for t in today}
        c4 = {t: max(0, int(pop[t] * rnd.uniform(0.02, 0.12) * (1 + sig4[t, i] ** 2))) for t in today}
        rd_rank = {t: r for r, t in enumerate(sorted(today, key=lambda x: -rd[x]), 1)}
        c4_rank = {t: r for r, t in enumerate(sorted(today, key=lambda x: -c4[x]), 1)}
        trending = set(rnd.sample(today, min(len(today), 12)))
        ta_res = {t: ta.analyze_bars(bars[t][: i + 1]) for t in today}
        ta_res["SPY"] = ta.analyze_bars(bars["SPY"][: i + 1])      # the real pipeline also fetches SPY
        # several runs per day (the last one after the close); earlier ones are noisier and see
        # smaller counts, because counts build up through the day
        for hh in [14, 17, 21][-rnd.choice([1, 2, 2, 3]):]:
            noise = 0.0 if hh == 21 else 0.22
            scale = 0.6 + 0.4 * (hh - 14) / 7
            rows = []
            for t in today:
                n4 = max(-1.0, min(1.0, sig4[t, i] + rnd.gauss(0, noise)))
                ns = max(-1.0, min(1.0, sigs[t, i] + rnd.gauss(0, noise)))
                rd_t = max(5, int(rd[t] * scale * rnd.uniform(0.92, 1.08)))
                c4_t = max(0, int(c4[t] * scale * rnd.uniform(0.9, 1.1)))
                has4 = c4_t >= 3
                comp = (0.5 * n4 * has4 + ns) / (0.5 * has4 + 1.0)
                dirs = {"4ch": (n4 > 0.1) - (n4 < -0.1) if has4 else None, "stw": (ns > 0.1) - (ns < -0.1)}
                vals = [v for v in dirs.values() if v is not None]
                if len(vals) == 2 and vals[0] == vals[1] and vals[0] != 0:
                    cons = f"UNANIMOUS {'BULLISH' if vals[0] > 0 else 'BEARISH'} (2/2)"
                elif len(vals) == 2 and vals[0] != vals[1]:
                    cons = "SPLIT (4ch:%s, stw:%s)" % tuple({1: "bull", -1: "bear", 0: "flat"}[v] for v in vals)
                else:
                    cons = "only stw"
                heat = (0.5 * (1 - (rd_rank[t] - 1) / 30) + 0.3 * max(0, 1 - (c4_rank[t] - 1) / 30)
                        + 0.2 * (t in trending))
                rows.append({
                    "ticker": t, "heat": heat, "composite": comp, "consensus": cons, "voters": len(vals),
                    "total": rd_t + c4_t,
                    "src": {"4chan": {"mentions": c4_t, "rank": c4_rank[t], "bull": int(3 + 4 * max(n4, 0)),
                                      "bear": int(1 + 4 * max(-n4, 0)), "net": n4 if has4 else None},
                            "reddit": {"mentions": rd_t, "rank": rd_rank[t],
                                       "prev": prev_rd.get(t) or max(5, int(rd[t] / (1 + chg[t, i]))),
                                       "upvotes": rd_t * 3},
                            "stocktwits": {"rank": 1 if t in trending else None, "net": ns,
                                           "official": {"sentiment": round(50 + 50 * ns, 1),
                                                        "volume": rnd.uniform(20, 90),
                                                        "participation": rnd.uniform(20, 90)}}}})
            now = datetime.fromisoformat(dates[i]).replace(hour=hh, tzinfo=timezone.utc)
            history.save_run(conn, rows, ["4chan", "reddit", "stocktwits"], weights,
                             {t: ta_res[t] for t in today}, {"demo": True}, now=now)
        prev_rd = rd
    allbars = {t: {"bars": b} for t, b in bars.items()}
    history.save_prices(conn, allbars)
    # synthetic FINRA short-sale volume for the last ~60 trading days
    sv, have = [], []
    for t in TICKERS:
        base = rnd.uniform(0.38, 0.52)
        for k in range(n_total - 60, n_total):
            ratio = max(0.15, min(0.85, base + 0.06 * sdev[t][k]))
            total = bars[t][k].volume * rnd.uniform(0.3, 0.5)
            sv.append((t, dates[k], ratio * total, 0.0, total))
            have.append((t, dates[k]))
    conn.executemany("INSERT OR REPLACE INTO short_volume VALUES (?,?,?,?,?)", sv)
    conn.executemany("INSERT OR REPLACE INTO short_have VALUES (?,?)", have)
    conn.commit()
    added = history.resolve_outcomes(conn, [1, 3, 5])
    conn.close()
    n_runs = history.connect(args.out).execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    print(f"Wrote {args.out}: {args.days} synthetic days ({n_runs} runs, 1-3 per day), "
          f"{len(TICKERS)} tickers, {added} outcomes")


if __name__ == "__main__":
    main()
