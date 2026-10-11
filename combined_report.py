#!/usr/bin/env python3
"""
combined_report.py - Compare ticker attention + sentiment across three sources:

  4chan /biz/ /smg/   text scraped from the live threads; ticker counts + VADER sentiment
  reddit (WSB)        ticker mentions/upvotes/rank from ApeWisdom's public feed (no sentiment)
  StockTwits          trending list + StockTwits' own published sentiment score (no messages)

Needs in the same folder: smg_scraper.py apewisdom_scraper.py stocktwits_scraper.py sentiment.py
technical_analysis.py history.py flow.py
    pip install requests vaderSentiment
    export TIINGO_API_KEY=...     # free key at tiingo.com; adds price/SMA/OBV columns to the CSV
                                  # (or `pip install yfinance` as an unofficial no-key fallback)

Designed to be run once a day:
    python combined_report.py --csv daily.csv
    python combined_report.py --sort 4chan              # most -> least mentioned on 4chan
    python combined_report.py --sort 4chan --reverse    # rarely mentioned on 4chan first
    python combined_report.py --sort gap                # biggest Reddit-vs-4chan attention gaps
    python combined_report.py --sources 4chan stocktwits
    python combined_report.py --weights 4chan=0.25 reddit=1 stocktwits=1.5

A source that fails (no live /smg/ thread, StockTwits blocking, ApeWisdom down) is skipped
with a warning and the report is built from the rest.
"""
import argparse
import csv
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import smg_scraper as smg
import sentiment
import technical_analysis
import history
import flow

SOURCES = ["4chan", "reddit", "stocktwits"]
SHORT = {"4chan": "4ch", "reddit": "rdt", "stocktwits": "stw"}
DEFAULT_WEIGHTS = {"4chan": 1.0, "reddit": 1.0, "stocktwits": 1.0}
ST_DEFAULT_URL = "https://api-gw-prd.stocktwits.com/sentiment-api/v2/{symbol}/detail"
ST_WINDOWS = ("now", "24h", "15m")
BULL, BEAR = 0.05, -0.05   # per-post thresholds for VADER scores (4chan)
DEAD_ZONE = 0.10           # |net| below this counts as "neutral" when voting
CONF_FULL = 20             # opinionated posts needed for a source's full sentiment weight


def warn(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 4chan (the only source where we read and score text ourselves)
# ---------------------------------------------------------------------------
def gather_threads(pattern, conn=None, remember_days=4):
    """(live, recovered): live catalog threads matching the title pattern(s), plus threads seen in
    earlier runs that have since left the catalog (they may still be readable in the archive)."""
    live = smg.find_smg_threads(pattern)
    recovered = []
    if conn is not None:
        history.remember_threads(conn, live)
        live_ids = {t["no"] for t in live}
        for no, subject in history.recent_threads(conn, remember_days):
            if no not in live_ids:
                recovered.append({"no": no, "subject": subject, "replies": None, "images": None,
                                  "url": smg.HUMAN_THREAD_URL.format(no), "recovered": True})
    return live, recovered


def fetch_4chan_records(pattern, days, conn=None):
    cutoff = time.time() - days * 86400
    live, recovered = gather_threads(pattern, conn)
    pats = pattern if isinstance(pattern, str) else " | ".join(pattern)
    warn(f"[4chan] {len(live)} live thread(s) matching '{pats}'"
         + (f" + {len(recovered)} remembered from earlier runs" if recovered else ""))
    records, total, kept = [], 0, 0
    for t in live + recovered:
        posts = smg.fetch_posts(t["no"])
        if t.get("recovered"):
            if posts:
                warn(f"[4chan] #{t['no']}: recovered {len(posts)} posts from a thread no longer in the catalog")
            elif conn is not None:
                history.mark_thread_gone(conn, t["no"])      # pruned: stop retrying it
        else:
            warn(f"[4chan] #{t['no']}: {len(posts)} posts")
        total += len(posts)
        for p in posts:
            if p.get("time", 0) >= cutoff:
                kept += 1
                records.append({"source": "4chan", "id": p["no"], "ts": p["time"],
                                "text": smg.clean_comment(p.get("com", ""))})
    warn(f"[4chan] {total} posts fetched, {kept} inside the --days window ({days:g} days)")
    return records


def debug_threads(pattern, conn=None):
    """List catalog threads that look like a stock general and whether the title pattern matches."""
    cands = smg.catalog_candidates(pattern)
    pats = pattern if isinstance(pattern, str) else " | ".join(pattern)
    print(f"Title pattern(s): {pats}\n")
    print(f"{'MATCHED':<9}{'REPLIES':>8}  THREAD")
    for c in sorted(cands, key=lambda c: (not c["matched"], -c["no"])):
        print(f"{'yes' if c['matched'] else 'NO':<9}{c['replies']:>8}  #{c['no']}  {c['subject'] or '(no subject)'}\n"
              f"{'':<17}{c['url']}" + (f"\n{'':<17}OP: {c['snippet']}" if not c['matched'] and c['snippet'] else ""))
    skipped = [c for c in cands if not c["matched"]]
    if skipped:
        print(f"\n{len(skipped)} thread(s) look like a stock general but were NOT matched. If any is one you want, add a "
              f"word from its title, e.g.:  --pattern /smg/ \"stock market general\"")
    elif not cands:
        print("No stock-general-looking threads are live right now.")
    if conn is not None:
        live_ids = {c["no"] for c in cands if c["matched"]}
        old = [(n, s_) for n, s_ in history.recent_threads(conn, 4) if n not in live_ids]
        if old:
            print("\nRemembered from earlier runs (no longer in the catalog; re-checked each run while readable):")
            for n, s_ in old:
                print(f"  #{n}  {s_}")


def analyze(records, valid, max_attr):
    """
    stats[ticker]["4chan"] = {mentions, bull, bear, neutral}.
    Sentiment is attributed only when a post names <= max_attr tickers.
    """
    stats = defaultdict(lambda: defaultdict(lambda: {"mentions": 0, "bull": 0, "bear": 0,
                                                     "neutral": 0}))
    info = defaultdict(lambda: {"total": 0, "analyzed": 0, "with_tickers": 0,
                                "skipped": Counter()})
    seen = defaultdict(set)
    for r in records:
        src = r["source"]
        info[src]["total"] += 1
        reason = smg.is_junk(r["text"], seen[src])
        if reason:
            info[src]["skipped"][reason] += 1
            continue
        info[src]["analyzed"] += 1
        tickers = list(smg.extract_tickers(r["text"], valid).keys())
        if not tickers:
            continue
        info[src]["with_tickers"] += 1
        s = sentiment.score(r["text"], mask=tickers)
        for sym in tickers:
            cell = stats[sym][src]
            cell["mentions"] += 1
            if len(tickers) <= max_attr:
                if s >= BULL:
                    cell["bull"] += 1
                elif s <= BEAR:
                    cell["bear"] += 1
                else:
                    cell["neutral"] += 1
    return stats, info


# ---------------------------------------------------------------------------
# Ranking, consensus, summary
# ---------------------------------------------------------------------------
def compute_ranks(stats, active, ext_rank):
    """Per-source rank (1 = hottest). Reddit: ApeWisdom's rank. StockTwits: its trending
    order. 4chan: by mention count."""
    ranks = {}
    for src in active:
        if src in ext_rank:
            ranks[src] = dict(ext_rank[src])
        else:
            ordered = sorted(((t, d[src]["mentions"]) for t, d in stats.items() if src in d),
                             key=lambda x: (-x[1], x[0]))
            ranks[src] = {t: i for i, (t, _) in enumerate(ordered, 1)}
    return ranks


def consensus(nets):
    """nets: {source: net or None}. Returns (label, n_voters)."""
    votes = {s: ("bull" if n >= DEAD_ZONE else "bear" if n <= -DEAD_ZONE else "flat")
             for s, n in nets.items() if n is not None}
    n = len(votes)
    if n == 0:
        return "n/a", 0
    if n == 1:
        return f"only {SHORT[next(iter(votes))]}", 1
    top, k = Counter(votes.values()).most_common(1)[0]
    name = {"bull": "BULLISH", "bear": "BEARISH", "flat": "NEUTRAL"}[top]
    if k == n:
        return f"UNANIMOUS {name} ({n}/{n})", n
    if k > n / 2:
        return f"MAJORITY {name} ({k}/{n})", n
    return "SPLIT (" + ", ".join(f"{SHORT[s]}:{v}" for s, v in votes.items()) + ")", n


def summarize(stats, active, ranks, weights, rank_n, min_opinions, st_scores, wsb):
    """
    One row per ticker. Where each source's NET (-1..+1) comes from:
      4chan      (bullish - bearish)/(bullish + bearish) over VADER-scored posts
      reddit     none - ApeWisdom provides counts/rank only
      stocktwits StockTwits' published 0-100 score via (score - 50) / 50
    """
    total_w = sum(weights[s] for s in active) or 1.0
    rows = []
    for sym in set(stats) | set(st_scores) | set(wsb):
        by_src = stats.get(sym, {})
        row = {"ticker": sym, "src": {}}
        heat, num, den, nets = 0.0, 0.0, 0.0, {}
        for src in active:
            rk = ranks[src].get(sym)
            d = {"mentions": 0, "rank": rk, "opinions": 0, "net": None, "bull": 0, "bear": 0,
                 "official": None, "upvotes": None, "prev": None, "chg": None}
            n_eff = 0
            if src == "4chan":
                c = by_src.get("4chan")
                if c:
                    opinions = c["bull"] + c["bear"]
                    d.update(mentions=c["mentions"], opinions=opinions,
                             bull=c["bull"], bear=c["bear"])
                    if opinions >= min_opinions:
                        d["net"] = (c["bull"] - c["bear"]) / opinions
                        n_eff = opinions
            elif src == "reddit":
                w = wsb.get(sym)
                if w:
                    d["mentions"] = w["mentions"] or 0
                    d["upvotes"] = w.get("upvotes")
                    d["prev"] = w.get("mentions_24h_ago")
                    if d["prev"]:
                        d["chg"] = (d["mentions"] - d["prev"]) / d["prev"]
            elif src == "stocktwits":
                off = st_scores.get(sym)
                if off:
                    d["official"] = off
                    d["net"] = (off["sentiment"] - 50) / 50
                    n_eff = CONF_FULL
            row["src"][src] = d
            nets[src] = d["net"]
            if rk is not None and rk <= rank_n:
                heat += weights[src] * (1 - (rk - 1) / rank_n)
            if d["net"] is not None:
                w_eff = weights[src] * min(1.0, n_eff / CONF_FULL)
                num += w_eff * d["net"]
                den += w_eff
        row["heat"] = heat / total_w
        row["composite"] = num / den if den else None
        row["consensus"], row["voters"] = consensus(nets)
        row["total"] = sum(v["mentions"] for v in row["src"].values())
        row["n_ranked"] = sum(1 for s_ in active
                              if row["src"][s_]["rank"] is not None
                              and row["src"][s_]["rank"] <= rank_n)
        rows.append(row)
    rows.sort(key=lambda r: (-r["heat"], -r["total"], r["ticker"]))
    return rows


def add_gaps(rows, stats, wsb, active):
    """Attention gap: each ticker's share of ALL 4chan mentions vs its share of ALL r/wsb
    mentions (ApeWisdom top list), in percentage points. gap > 0: relatively more talked about
    on Reddit than on 4chan; gap < 0: the reverse. Shares remove the scale difference between
    platforms (thousands of Reddit mentions vs hundreds on 4chan)."""
    tot4 = sum(d["4chan"]["mentions"] for d in stats.values() if "4chan" in d)
    totr = sum((w["mentions"] or 0) for w in wsb.values())
    both = "4chan" in active and "reddit" in active and tot4 > 0 and totr > 0
    for r in rows:
        if both:
            r["share4"] = 100.0 * r["src"]["4chan"]["mentions"] / tot4
            r["sharer"] = 100.0 * r["src"]["reddit"]["mentions"] / totr
            r["gap"] = r["sharer"] - r["share4"]
        else:
            r["share4"] = r["sharer"] = r["gap"] = None


def sort_rows(rows, how, reverse):
    """how: total | 4chan | reddit | heat | gap. Default order is most -> least
    (for gap: most Reddit-heavy first); reverse flips it."""
    def m(r, src):
        return r["src"].get(src, {}).get("mentions", 0) or 0
    keys = {
        "total": lambda r: (r["total"], m(r, "4chan")),
        "4chan": lambda r: (m(r, "4chan"), m(r, "reddit")),
        "reddit": lambda r: (m(r, "reddit"), m(r, "4chan")),
        "heat": lambda r: (r["heat"], r["total"]),
        "gap": lambda r: (r["gap"] if r["gap"] is not None else float("-inf"), r["total"]),
    }
    rows.sort(key=lambda r: r["ticker"])                         # stable, deterministic ties
    rows.sort(key=keys[how], reverse=not reverse)


def _why_not(sym, text, valid):
    """Plain-English reason extract_tickers() did not count `sym` in this post."""
    import re
    if sym in smg.CRYPTO:
        return "excluded as a crypto symbol"
    forms = [m.group(0) for m in re.finditer(rf"(?<![A-Za-z0-9])\$?{re.escape(sym)}(?![A-Za-z0-9])",
                                             text, re.I)]
    cash = any(f.startswith("$") for f in forms)
    upper = any(f.lstrip("$") == sym for f in forms)
    known, in_sec = sym in smg.KNOWN_TICKERS, sym in valid
    if sym in smg.AMBIGUOUS and not cash:
        return ("also an everyday word, so ALL CAPS alone isn't enough: it counts as a cashtag ($%s) or with "
                "ticker-like context (e.g. '%s 140c', 'shares of %s', 'NVDA, %s')" % (sym, sym, sym, sym))
    if cash and valid and not in_sec and not known:
        return ("cashtag, but the symbol isn't in the SEC ticker list (many ETFs/ETPs aren't) - "
                "add it to known_tickers.txt")
    if not cash and not upper and not known:
        return ("written in lowercase/mixed case; bare tickers must be ALL CAPS unless listed in "
                "known_tickers.txt")
    if upper and not in_sec and not known:
        return "not in the SEC ticker list (many ETFs/ETPs aren't) - add it to known_tickers.txt"
    if not valid and not known and not cash:
        return "running with --no-sec: only cashtags and known tickers count"
    return "no matching rule - please report this"


def debug_ticker(symbols, pattern, days, valid, conn=None):
    """Re-scan the live /smg/ threads and show, for every post that mentions each symbol,
    whether it was counted and - if not - exactly why."""
    import re
    import html as _html
    syms = [s_.upper() for s_ in symbols]
    print(f"SEC list: {len(valid):,} symbols" if valid else "SEC list: not loaded (--no-sec)")
    for s_ in syms:
        print(f"  {s_}: in SEC list={s_ in valid} | in known list={s_ in smg.KNOWN_TICKERS} | "
              f"ambiguous word={s_ in smg.AMBIGUOUS} | crypto={s_ in smg.CRYPTO}")
    pats = {s_: re.compile(rf"(?<![A-Za-z0-9])\$?{re.escape(s_)}(?![A-Za-z0-9])", re.I) for s_ in syms}
    cutoff = time.time() - days * 86400
    live, recovered = gather_threads(pattern, conn)
    threads = live + recovered
    print(f"\n{len(live)} live thread(s) matching '{pattern}'" + (f" + {len(recovered)} remembered" if recovered else "") + ":")
    for t in threads:
        print(f"  #{t['no']}  {'(remembered)' if t.get('recovered') else str(t['replies']) + ' replies'}  {t['subject']}\n    {t['url']}")
    if not threads:
        print("  -> No matching thread right now, so nothing was scanned. "
              "(Check the title still contains the pattern, or pass --pattern.)")
        return

    seen = set()                      # mirrors analyze(): junk/duplicate detection is stateful
    tally = {s_: Counter() for s_ in syms}
    total_posts = scanned = 0
    for t in threads:
        for p in smg.fetch_posts(t["no"]):
            total_posts += 1
            text = smg.clean_comment(p.get("com", ""))
            in_window = p.get("time", 0) >= cutoff
            reason = smg.is_junk(text, seen) if in_window else None
            scanned += in_window
            extras = " ".join(filter(None, [_html.unescape(p.get("sub", "") or ""),
                                            (p.get("filename", "") or "") + (p.get("ext", "") or "")]))
            for s_ in syms:
                m = pats[s_].search(text)
                if not m:
                    if pats[s_].search(extras):
                        tally[s_]["only in subject/filename (not scanned)"] += 1
                        print(f"\n[{s_}] post {p['no']}: mentioned only in the subject/filename field, "
                              f"which isn't scanned.\n    {t['url']}#p{p['no']}")
                    continue
                snippet = text[max(0, m.start() - 60): m.end() + 60].replace("\n", " ")
                when = datetime.fromtimestamp(p.get("time", 0), timezone.utc).strftime("%m-%d %H:%M")
                if not in_window:
                    verdict = f"NOT counted: older than the --days window ({days:g} days)"
                elif reason:
                    verdict = f"NOT counted: post filtered as junk ({reason})"
                elif s_ in smg.extract_tickers(text, valid):
                    verdict = "COUNTED"
                else:
                    verdict = "NOT counted: " + _why_not(s_, text, valid)
                tally[s_][verdict.split(" (")[0].split(": ", 1)[-1] if verdict != "COUNTED" else "COUNTED"] += 1
                print(f"\n[{s_}] post {p['no']} ({when} UTC): ...{snippet}...\n    -> {verdict}\n"
                      f"    {t['url']}#p{p['no']}")
    print(f"\nScanned {total_posts} posts in total ({scanned} inside the --days window).")
    for s_ in syms:
        n = sum(tally[s_].values())
        print(f"  {s_}: {n} post(s) mention it -> " +
              (", ".join(f"{k}: {v}" for k, v in tally[s_].most_common()) or "none found"))
        if n == 0:
            print("     (If you can see it on the site: it may be in a thread the title pattern missed, "
                  "added after this scan, or inside an image.)")


def pick_extra(stats, wsb, exclude, valid, n):
    """Top-n tickers by 4chan+reddit mentions that StockTwits isn't already trending,
    so StockTwits' sentiment can be looked up for what the other forums are discussing."""
    if n <= 0:
        return []
    combined = Counter()
    for sym, d in stats.items():
        combined[sym] += d.get("4chan", {}).get("mentions", 0)
    for sym, d in wsb.items():
        combined[sym] += d.get("mentions") or 0
    out = []
    for sym, _ in combined.most_common():
        if (sym in exclude or sym in smg.CRYPTO
                or (valid and sym not in valid and sym not in smg.KNOWN_TICKERS)):
            continue
        out.append(sym)
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def fnet(x):
    return "    -" if x is None else f"{x:+.2f}"


def cell(row, active, src, key, width, fmt=str):
    if src not in active:
        return f"{'x':>{width}}"
    v = row["src"][src][key]
    return f"{('-' if v is None else fmt(v)):>{width}}"


def gap_cell(row):
    return "-" if row.get("gap") is None else f"{row['gap']:+.1f}"


def stw_score(row):
    off = row["src"].get("stocktwits", {}).get("official")
    return "-" if not off else f"{off['sentiment']:.0f}"


def print_report(rows, active, args, weights):
    W = 115
    print("\n" + "=" * W)
    print(f"CROSS-FORUM TICKER REPORT | active: "
          + ", ".join(f"{s} (w={weights[s]:g})" for s in active))
    print("=" * W)
    print(f"{'':<8}{'':>5} | {'MENTIONS':^11} | {'RANK':^15} | {'NET SENT.':^12} | "
          f"{'STW':>4} {'RDT':>6} | {'GAP':>6} |")
    print(f"{'TICKER':<8}{'HEAT':>5} | {'4ch':>5}{'rdt':>6} | {'4ch':>5}{'rdt':>5}{'stw':>5} | "
          f"{'4ch':>6}{'stw':>6} | {'0-100':>4} {'24h':>6} | {'pp':>6} | {'COMP':>5}  CONSENSUS")
    print("-" * W)
    for r in rows[: args.top]:
        print(f"{r['ticker']:<8}{r['heat']:>5.2f} | "
              f"{cell(r, active, '4chan', 'mentions', 5)}{cell(r, active, 'reddit', 'mentions', 6)} | "
              f"{cell(r, active, '4chan', 'rank', 5)}{cell(r, active, 'reddit', 'rank', 5)}"
              f"{cell(r, active, 'stocktwits', 'rank', 5)} | "
              f"{cell(r, active, '4chan', 'net', 6, fnet)}{cell(r, active, 'stocktwits', 'net', 6, fnet)} | "
              f"{stw_score(r):>4} {cell(r, active, 'reddit', 'chg', 6, lambda v: f'{v:+.0%}')} | "
              f"{gap_cell(r):>6} | {fnet(r['composite']):>5}  {r['consensus']}")
    print("=" * W)
    print(f"HEAT = weighted rank score 0-1 (rank within each source's top {args.rank_n}, scaled by weight).")
    print("NET  = -1..+1.  4chan: (bullish - bearish)/(bullish + bearish) VADER-scored posts, needs "
          f">= {args.min_opinions}.  stw: StockTwits' own score, (score - 50)/50.")
    print("rdt  = r/wallstreetbets via ApeWisdom: mentions in last 24h, rank, and mention change vs "
          "the prior 24h. It has NO sentiment, so it doesn't vote.")
    print(f"CONSENSUS compares the NET direction of the sources that have one "
          f"(|NET| < {DEAD_ZONE} counts as neutral). 'x' = source inactive, '-' = no data.")
    print("GAP  = (share of all r/wsb mentions) - (share of all 4chan mentions), in percentage points: "
          "+ = relatively more talked about on Reddit, - = more on 4chan.")
    print(f"Sorted by: {args.sort}{' (reversed)' if args.reverse else ''}")
    print_gaps(rows, args)

    lows = [r for r in rows[: args.top]
            if (r["src"].get("stocktwits", {}).get("official") or {}).get("participation") is not None
            and r["src"]["stocktwits"]["official"]["participation"] <= 25]
    if lows:
        print("\nLOW STOCKTWITS PARTICIPATION (few accounts posting a lot - possible promotion): "
              + ", ".join(r["ticker"] for r in lows))

    voters = [r for r in rows if r["voters"] >= 2]
    if voters:
        unanimous = [r for r in voters if r["consensus"].startswith("UNANIMOUS")]
        print(f"\nUNANIMOUS SENTIMENT ({len(unanimous)}):")
        for r in unanimous[:15]:
            print(f"  {r['ticker']:<7} {r['consensus']}   comp {fnet(r['composite']).strip()}")
        splits = [r for r in voters if r["consensus"].startswith("SPLIT")]
        print(f"\nDISAGREEMENTS ({len(splits)}):")
        for r in splits[:10]:
            print(f"  {r['ticker']:<7} {r['consensus']}")
    else:
        print("\n(Fewer than two sources have sentiment for any ticker, so no consensus to show.)")


def print_gaps(rows, args):
    gapped = [r for r in rows if r.get("gap") is not None]
    if not gapped:
        return
    n = args.gap_rows

    def line(r):
        return (f"  {r['ticker']:<7} 4ch {r['src']['4chan']['mentions']:>4} ({r['share4']:4.1f}%)   "
                f"rdt {r['src']['reddit']['mentions']:>5} ({r['sharer']:4.1f}%)   gap {r['gap']:+5.1f}pp")

    by_gap = sorted(gapped, key=lambda r: r["gap"], reverse=True)
    print("\nATTENTION GAPS (share of each platform's total mentions)")
    print("  Talked about on r/wsb, quiet on 4chan:")
    for r in [r for r in by_gap if r["gap"] > 0][:n]:
        print(line(r))
    print("  Talked about on 4chan, quiet on r/wsb:")
    for r in [r for r in reversed(by_gap) if r["gap"] < 0][:n]:
        print(line(r))
    print("  Caveat: the two platforms detect tickers differently, and 4chan is a small sample "
          "(a few live threads), so small gaps are noise.")


def write_csv(path, rows, active, args, weights, ta=None, flow_m=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    head = ["ticker", "heat", "composite_net", "consensus", "voters",
            "4chan_mentions", "4chan_rank", "4chan_bullish", "4chan_bearish", "4chan_net",
            "4chan_share_pct", "reddit_share_pct", "attention_gap_pp",
            "reddit_mentions", "reddit_rank", "reddit_mentions_24h_ago", "reddit_upvotes",
            "stocktwits_rank", "stocktwits_net", "stocktwits_sentiment_0_100",
            "stocktwits_volume_0_100", "stocktwits_participation_0_100",
            "active_sources", "weights", "scraped_at_utc"]
    if ta is not None:     # technical-analysis columns go just before the trailing metadata
        head[-3:-3] = technical_analysis.COLUMNS
    if flow_m is not None:  # FINRA short-sale columns follow them
        head[-3:-3] = flow.CSV_COLUMNS
    wstr = ";".join(f"{s}={weights[s]:g}" for s in active)

    def g(v, nd=4):
        return "" if v is None else (round(v, nd) if isinstance(v, float) else v)

    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        for r in rows:
            c4 = r["src"].get("4chan", {})
            rd = r["src"].get("reddit", {})
            st = r["src"].get("stocktwits", {})
            off = st.get("official") or {}
            line = [r["ticker"], g(r["heat"]), g(r["composite"]), r["consensus"], r["voters"],
                        g(c4.get("mentions")), g(c4.get("rank")), g(c4.get("bull")),
                        g(c4.get("bear")), g(c4.get("net")),
                        g(r.get("share4")), g(r.get("sharer")), g(r.get("gap")),
                        g(rd.get("mentions")), g(rd.get("rank")), g(rd.get("prev")),
                        g(rd.get("upvotes")),
                        g(st.get("rank")), g(st.get("net")), g(off.get("sentiment")),
                        g(off.get("volume")), g(off.get("participation")),
                        ",".join(active), wstr, now]
            if ta is not None:
                line[-3:-3] = technical_analysis.row_values(ta.get(r["ticker"]))
            if flow_m is not None:
                line[-3:-3] = flow.csv_values(flow_m.get(r["ticker"]))
            w.writerow(line)
    print(f"\nSaved {len(rows)} rows to {path}")


def parse_weights(items):
    w = dict(DEFAULT_WEIGHTS)
    for item in items or []:
        try:
            k, v = item.split("=")
            if k not in SOURCES:
                raise ValueError
            w[k] = float(v)
        except ValueError:
            sys.exit(f"Bad --weights entry '{item}'. Use e.g. 4chan=0.5 reddit=1 stocktwits=1")
    return w


def main():
    ap = argparse.ArgumentParser(description="Cross-forum ticker counts, sentiment and consensus")
    ap.add_argument("--sources", nargs="+", choices=SOURCES, default=SOURCES)
    ap.add_argument("--days", type=float, default=7,
                    help="4chan lookback window (days). Reddit/StockTwits are always 'right now'/24h")
    ap.add_argument("--weights", nargs="+", metavar="SRC=W", help=f"default {DEFAULT_WEIGHTS}")
    ap.add_argument("--pattern", nargs="+", default=["/smg/"],
                    help='4chan thread-title substring(s); a thread matches if its title contains ANY, '
                         'e.g. --pattern /smg/ "stock market general"')
    ap.add_argument("--debug-threads", action="store_true",
                    help="list live threads that look like a stock general and whether --pattern matches "
                         "them (to spot title variants the script would skip), then exit")
    ap.add_argument("--wsb-filter", default="wallstreetbets", help="ApeWisdom community filter")
    ap.add_argument("--wsb-pages", type=int, default=1, help="ApeWisdom pages (~100 tickers each)")
    ap.add_argument("--st-top", type=int, default=25, help="StockTwits trending symbols to score")
    ap.add_argument("--st-extra", type=int, default=20,
                    help="also look up StockTwits' score for this many of the top 4chan/Reddit "
                         "tickers that aren't StockTwits-trending (0 = off)")
    ap.add_argument("--st-window", choices=ST_WINDOWS, default="now",
                    help="StockTwits window: 'now' (default; the headline score on the site), "
                         "24h (smoother), or 15m. Ignored if you pass explicit --st-*-path values")
    ap.add_argument("--st-score-url", metavar="URL", default=os.environ.get("ST_SCORE_URL", ST_DEFAULT_URL))
    ap.add_argument("--st-score-path", metavar="PATH", default=os.environ.get("ST_SCORE_PATH"))
    ap.add_argument("--st-volume-path", metavar="PATH", default=os.environ.get("ST_VOLUME_PATH"))
    ap.add_argument("--st-participation-path", metavar="PATH",
                    default=os.environ.get("ST_PARTICIPATION_PATH"))
    ap.add_argument("--debug-ticker", nargs="+", metavar="SYMBOL",
                    help="diagnose: show every /smg/ post mentioning SYMBOL and whether (and why/why "
                         "not) the script counted it, then exit")
    ap.add_argument("--st-probe", metavar="SYMBOL",
                    help="debug: fetch --st-score-url for SYMBOL, list every 0-100 numeric field, exit")
    ap.add_argument("--sort", choices=["total", "4chan", "reddit", "heat", "gap"], default="total",
                    help="row order: total = 4chan+reddit mentions (default); 4chan / reddit = by that "
                         "platform's mentions; heat = weighted rank score; gap = Reddit-vs-4chan "
                         "attention gap (most Reddit-heavy first)")
    ap.add_argument("--reverse", action="store_true",
                    help="flip the order (e.g. --sort 4chan --reverse = least-mentioned on 4chan first)")
    ap.add_argument("--gap-rows", type=int, default=8, help="rows per side in the ATTENTION GAPS section")
    ap.add_argument("--top", type=int, default=30, help="rows to print")
    ap.add_argument("--rank-n", type=int, default=30, help="a source's 'top N' for HEAT")
    ap.add_argument("--min-count", type=int, default=1,
                    help="hide tickers with fewer 4chan+reddit mentions (unless StockTwits-trending)")
    ap.add_argument("--min-opinions", type=int, default=1,
                    help="opinionated 4chan posts needed before 4chan gets a NET / a vote")
    ap.add_argument("--max-attr", type=int, default=1,
                    help="only attribute 4chan sentiment from posts naming <= this many tickers")
    ap.add_argument("--csv", metavar="FILE",
                    help="CSV output path. Supports date placeholders so runs don't overwrite each "
                         "other, e.g. report_%%Y-%%m-%%d_%%H%%M.csv")
    ap.add_argument("--no-ta", action="store_true",
                    help="don't fetch prices (no price/SMA/OBV columns, and today's run can't be "
                         "scored against later prices)")
    ap.add_argument("--dashboard", nargs="?", const="dashboard.html", default=None, metavar="FILE",
                    help="after saving the run, (re)build the HTML dashboard from the database "
                         "(default file: dashboard.html)")
    ap.add_argument("--db", default="history.db", metavar="FILE",
                    help="SQLite file where every run is saved (default history.db)")
    ap.add_argument("--no-save", action="store_true", help="don't save this run to the database")
    ap.add_argument("--no-backtest", action="store_true",
                    help="save the run but skip scoring earlier runs / printing the backtest")
    ap.add_argument("--bt-horizons", nargs="+", type=int, default=[1, 3, 5], metavar="DAYS",
                    help="trading-day horizons to score signals at (default 1 3 5)")
    ap.add_argument("--bt-fetch-max", type=int, default=5,
                    help="max extra tickers to fetch prices for just to score older signals")
    ap.add_argument("--ta-provider", choices=["auto", "tiingo", "yfinance"], default="auto",
                    help="price data source for the technical columns (auto: Tiingo if "
                         "TIINGO_API_KEY is set, else yfinance if installed)")
    ap.add_argument("--no-short", action="store_true",
                    help="skip FINRA daily short-sale volume (no short-ratio columns / dashboard data)")
    ap.add_argument("--short-days", type=int, default=flow.DEFAULT_DAYS,
                    help="calendar days of FINRA short-sale history kept per ticker (default 30)")
    ap.add_argument("--no-ta-cache", action="store_true",
                    help="always re-download prices (default: reuse saved prices when they already "
                         "include the latest completed session)")
    ap.add_argument("--ta-max", type=int, default=100,
                    help="how many tickers to cover with price data, highest 'heat' first "
                         "(the order the dashboard shows them in)")
    ap.add_argument("--ta-budget", type=int, default=40,
                    help="max NEW price downloads per run. Tiingo's free plan allows 50 requests per "
                         "hour, so the default leaves headroom; tickers over budget are filled in on "
                         "later runs (saved prices cost nothing)")
    ap.add_argument("--no-sec", action="store_true")
    args = ap.parse_args()
    weights = parse_weights(args.weights)

    args.st_score_path = args.st_score_path or f"data.sentiment.{args.st_window}.valueNormalized"
    args.st_volume_path = args.st_volume_path or f"data.messageVolume.{args.st_window}.valueNormalized"
    args.st_participation_path = (args.st_participation_path
                                  or "data.timeframes.1D.participationScore.valueNormalized")

    if args.debug_ticker or args.debug_threads:
        dconn = history.connect(args.db) if os.path.exists(args.db) and not args.no_save else None
        if args.debug_threads:
            debug_threads(args.pattern, dconn)
        else:
            debug_ticker(args.debug_ticker, args.pattern, args.days,
                         set() if args.no_sec else smg.load_valid_tickers(), dconn)
        return

    if args.st_probe:
        import stocktwits_scraper
        try:
            data = stocktwits_scraper._get(args.st_score_url.format(symbol=args.st_probe.upper()))
        except Exception as e:
            sys.exit(f"Probe failed: {e}")
        if data is None:
            sys.exit("Probe got HTTP 404 - check the URL / symbol.")
        found = stocktwits_scraper.numeric_paths(data)
        cand = [(p_, v) for p_, v in found if 0 <= v <= 100]
        print(f"Response had {len(found)} numeric fields; {len(cand)} are between 0 and 100:")
        for p_, v in cand:
            print(f"  {p_:<60} {v}")
        return

    t0 = time.time()
    valid = set() if args.no_sec else smg.load_valid_tickers()
    warn(f"Ticker validation list: {len(valid):,} symbols" if valid
         else "No SEC list: cashtags + company names only")

    records, wsb, st_scores, ext_rank = [], {}, {}, {}
    # local history database: also remembers /smg/ thread IDs and lets repeat runs reuse prices
    conn = None if args.no_save else history.connect(args.db)

    if "4chan" in args.sources:
        try:
            records = fetch_4chan_records(args.pattern, args.days, conn)
        except Exception as e:
            warn(f"[4chan] skipped: {e}")
    if "reddit" in args.sources:
        try:
            import apewisdom_scraper
            wsb = apewisdom_scraper.fetch(args.wsb_filter, args.wsb_pages)
            ext_rank["reddit"] = {s: d["rank"] for s, d in wsb.items() if d["rank"] is not None}
        except Exception as e:
            warn(f"[reddit] skipped: {e}")

    stats, info = analyze(records, valid, args.max_attr)

    if "stocktwits" in args.sources:
        try:
            import stocktwits_scraper
            trending = stocktwits_scraper.get_trending_symbols(args.st_top)
            ext_rank["stocktwits"] = {s: i for i, s in enumerate(trending, 1)}
            extra = pick_extra(stats, wsb, set(trending), valid, args.st_extra)
            warn(f"[stocktwits] {len(trending)} trending + {len(extra)} from other forums")
            st_scores = stocktwits_scraper.fetch_scores(
                trending + extra, args.st_score_url, args.st_score_path,
                args.st_volume_path, args.st_participation_path)
            if not st_scores:
                warn("[stocktwits] no scores parsed - check the endpoint with --st-probe SYMBOL")
        except Exception as e:
            warn(f"[stocktwits] skipped: {e}")

    active = [s for s, ok in (("4chan", info["4chan"]["analyzed"] > 0), ("reddit", bool(wsb)),
                              ("stocktwits", bool(st_scores) or bool(ext_rank.get("stocktwits"))))
              if ok and s in args.sources]
    if not active:
        print("No data collected from any source.")
        return

    ranks = compute_ranks(stats, active, ext_rank)
    rows = summarize(stats, active, ranks, weights, args.rank_n, args.min_opinions, st_scores, wsb)
    rows = [r for r in rows
            if r["total"] >= args.min_count or r["src"].get("stocktwits", {}).get("rank")]

    add_gaps(rows, stats, wsb, active)
    sort_rows(rows, args.sort, args.reverse)
    print_report(rows, active, args, weights)

    print("\nPER-SOURCE STATS")
    if "4chan" in active:
        i = info["4chan"]
        skipped = sum(i["skipped"].values())
        print(f"  [4chan] collected={i['total']}  analyzed={i['analyzed']}  filtered={skipped} "
              f"({', '.join(f'{k}={v}' for k, v in i['skipped'].most_common()) or 'none'})  "
              f"with_tickers={i['with_tickers']}")
    if "reddit" in active:
        print(f"  [reddit] {len(wsb)} tickers from ApeWisdom (r/{args.wsb_filter}, their 24h counts)")
    if "stocktwits" in active:
        print(f"  [stocktwits] trending={len(ext_rank.get('stocktwits', {}))}  "
              f"scored={len(st_scores)}  window={args.st_window}")
    # ---- price data: CSV columns + material for scoring signals later ----
    ta = None
    price_status = {}
    if not args.no_ta:
        ta = {}    # stays {} on failure so the CSV schema is stable (blank columns)
        # priority = the dashboard's order (heat), with the SPY benchmark first so a rate limit
        # part-way through can never cost us the benchmark
        by_heat = sorted(rows, key=lambda r: (-(r["heat"] or 0), -r["total"], r["ticker"]))
        wanted = [history.BENCHMARK] + [r["ticker"] for r in by_heat[: args.ta_max]
                                        if r["ticker"] != history.BENCHMARK]
        cached = {}
        if conn is not None and not args.no_ta_cache:
            expected = history.expected_last_session()
            for sym in wanted:
                bars = history.cached_bars(conn, sym, expected)
                if bars:
                    res = technical_analysis.analyze_bars(bars)
                    res["bars"] = bars
                    cached[sym] = res
            if cached:
                warn(f"[ta] reused saved prices for {len(cached)}/{len(wanted)} symbols "
                     f"(already have the {expected} close)")
        known_missing = (history.recent_not_found(conn)
                         if conn is not None and not args.no_ta_cache else set())
        skip_missing = [s_ for s_ in wanted if s_ in known_missing and s_ not in cached]
        uncached = [s_ for s_ in wanted if s_ not in cached and s_ not in known_missing]
        to_fetch, deferred = uncached[: args.ta_budget], uncached[args.ta_budget:]
        fetched = {}
        if to_fetch:
            try:
                fetched = technical_analysis.analyze_many(to_fetch, provider=args.ta_provider)
            except technical_analysis.DataError as e:
                warn(f"[ta] skipped: {e}")
                price_status.update({s_: "no_provider" for s_ in to_fetch})
        ta = {**fetched, **cached}
        price_status.update({s_: "not_found" for s_ in skip_missing})
        price_status.update({s_: "cached" for s_ in cached})
        price_status.update({s_: res.get("status", "ok") for s_, res in fetched.items()})
        price_status.update({s_: "deferred" for s_ in deferred})
        if deferred:
            warn(f"[ta] {len(deferred)} ticker(s) deferred to later runs (--ta-budget {args.ta_budget}; "
                 f"Tiingo's free plan allows 50 requests/hour). They are filled in as saved prices accumulate.")
    else:
        price_status = {r["ticker"]: "disabled" for r in rows}
    for r in rows:
        price_status.setdefault(r["ticker"], "not_requested")

    # ---- FINRA daily short-sale volume (a sell-side flow proxy; see flow.py for how to read it) ----
    flow_m = None
    if not args.no_short and rows:
        fconn = conn if conn is not None else history.connect(":memory:")
        fsyms = [r["ticker"] for r in rows[:300]]      # file-based, so no per-symbol cost
        fst = flow.update_short_volume(fconn, fsyms, days=args.short_days)
        warn(f"[flow] FINRA short-sale volume: {fst['files']} file(s) fetched, {fst['rows']} rows"
             + (f"; stopped early ({fst['error']})" if fst["error"] else ""))
        flow_m = flow.latest_metrics(fconn, fsyms)
        if conn is None:
            fconn.close()

    # ---- save this run, then score earlier runs against what prices did since ----
    if conn is not None:
        try:
            params = {k: getattr(args, k) for k in (
                "days", "sort", "pattern", "wsb_filter", "st_top", "st_extra", "st_window",
                "min_count", "min_opinions", "max_attr", "ta_provider")}
            run_id = history.save_run(conn, rows, active, weights, ta, params)
            n_prices = history.save_prices(conn, ta)
            history.save_price_status(conn, run_id, price_status)
            warn(f"[history] saved run #{run_id}: {len(rows)} tickers, {n_prices} price rows -> {args.db}")
            if not any("error" not in v for v in (ta or {}).values()):
                warn("[history] no price data this run: sentiment is saved but can't be scored "
                     "later (set TIINGO_API_KEY or install yfinance)")
            if not args.no_backtest:
                need = history.tickers_needing_refresh(conn, args.bt_horizons, have=set(ta or {}))
                if need and ta:
                    warn(f"[history] fetching prices for {min(len(need), args.bt_fetch_max)} "
                         f"older tickers to score their past signals")
                    history.refresh_prices(conn, need, args.ta_provider, args.bt_fetch_max)
                history.resolve_outcomes(conn, args.bt_horizons)
                h_first, h_last = min(args.bt_horizons), max(args.bt_horizons)
                history.print_previous_run_check(conn, run_id, horizon=h_first, dead_zone=DEAD_ZONE)
                history.print_backtest(conn, args.bt_horizons, DEAD_ZONE)
                history.print_source_scorecard(conn, h_first, DEAD_ZONE, weights)
                history.print_attention_study(conn, sorted({h_first, h_last}), DEAD_ZONE)
        finally:
            conn.close()

    if args.dashboard and not args.no_save:
        try:
            import dashboard
            warn(f"[dashboard] wrote {dashboard.build(args.db, args.dashboard)}")
        except Exception as e:                      # never let the dashboard break a daily run
            warn(f"[dashboard] skipped: {type(e).__name__}: {e}")

    print(f"\nUnique tickers: {len(rows)}   Runtime: {time.time() - t0:.1f}s")

    if args.csv:
        write_csv(datetime.now().strftime(args.csv), rows, active, args, weights, ta, flow_m)

if __name__ == "__main__":
    main()
