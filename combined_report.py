#!/usr/bin/env python3
"""
combined_report.py - Ticker counts + sentiment across 4chan /smg/ and r/wallstreetbets.

Requires smg_scraper.py, reddit_scraper.py, sentiment.py in the same folder.

    pip install requests praw vaderSentiment

Examples:
    python combined_report.py                          # both sources, last 7 days
    python combined_report.py --days 3 --csv out.csv   # last 3 days, save CSV
    python combined_report.py --sources reddit         # Reddit only
    python combined_report.py --sources 4chan          # /smg/ only (no Reddit creds needed)
"""
import argparse
import csv
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import smg_scraper as smg
import sentiment

BULL, BEAR = 0.05, -0.05  # per-post thresholds


# ----------------------------------------------------------------------------
# Source adapters -> normalized records
# ----------------------------------------------------------------------------
def fetch_4chan_records(pattern, days):
    cutoff = time.time() - days * 86400
    threads = smg.find_smg_threads(pattern)
    print(f"[4chan] {len(threads)} thread(s) matching '{pattern}'", file=sys.stderr)
    records = []
    for t in threads:
        posts = smg.fetch_posts(t["no"])
        print(f"[4chan] #{t['no']}: {len(posts)} posts", file=sys.stderr)
        for p in posts:
            if p.get("time", 0) < cutoff:
                continue
            records.append({"source": "4chan", "id": p["no"], "ts": p["time"],
                            "text": smg.clean_comment(p.get("com", "")),
                            "score": None, "kind": "post"})
    return records, len(threads)


# ----------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------
def analyze(records, valid, max_attr):
    """
    Returns (stats, source_info).
      stats[ticker][source] = {"mentions": int, "sents": [floats]}
    Sentiment is only attributed when a post names <= max_attr tickers, because
    one comment praising NVDA and trashing AMD can't be split by a post-level scorer.
    """
    stats = defaultdict(lambda: defaultdict(lambda: {"mentions": 0, "sents": []}))
    info = defaultdict(lambda: {"total": 0, "analyzed": 0, "with_tickers": 0,
                                "skipped": Counter()})
    seen = defaultdict(set)  # per-source duplicate detection

    for r in records:
        src = r["source"]
        info[src]["total"] += 1
        reason = smg.is_junk(r["text"], seen[src])
        if reason:
            info[src]["skipped"][reason] += 1
            continue
        info[src]["analyzed"] += 1

        hits = smg.extract_tickers(r["text"], valid)
        if not hits:
            continue
        info[src]["with_tickers"] += 1
        s = sentiment.score(r["text"], mask=hits.keys())
        for sym in hits:
            stats[sym][src]["mentions"] += 1
            if len(hits) <= max_attr:
                stats[sym][src]["sents"].append(s)
    return stats, info


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def summarize(stats):
    rows = []
    for sym, by_src in stats.items():
        all_sents = [x for d in by_src.values() for x in d["sents"]]
        rows.append({
            "ticker": sym,
            "total": sum(d["mentions"] for d in by_src.values()),
            "4chan": by_src["4chan"]["mentions"] if "4chan" in by_src else 0,
            "reddit": by_src["reddit"]["mentions"] if "reddit" in by_src else 0,
            "sent_4chan": mean(by_src["4chan"]["sents"]) if "4chan" in by_src else None,
            "sent_reddit": mean(by_src["reddit"]["sents"]) if "reddit" in by_src else None,
            "sent_all": mean(all_sents),
            "n_scored": len(all_sents),
            "pct_bull": (sum(x >= BULL for x in all_sents) / len(all_sents)) if all_sents else None,
            "pct_bear": (sum(x <= BEAR for x in all_sents) / len(all_sents)) if all_sents else None,
        })
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def f(x, spec="+.2f"):
    return "   -  " if x is None else format(x, spec)


def pct(x):
    return "  - " if x is None else f"{x:>3.0%}"


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Ticker counts + sentiment from 4chan /smg/ and r/wallstreetbets")
    ap.add_argument("--sources", nargs="+", choices=["4chan", "reddit"], default=["4chan", "reddit"])
    ap.add_argument("--days", type=float, default=7, help="lookback window in days (e.g. 3, 7)")
    ap.add_argument("--subreddit", default="wallstreetbets")
    ap.add_argument("--pattern", default="/smg/", help="4chan thread-title substring")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-count", type=int, default=3, help="hide tickers with fewer total mentions")
    ap.add_argument("--min-scored", type=int, default=1, help="hide tickers with fewer scored posts")
    ap.add_argument("--max-attr", type=int, default=3,
                    help="only attribute sentiment from posts naming <= this many tickers")
    ap.add_argument("--replace-more", type=int, default=8,
                    help="Reddit 'load more comments' expansions per post (0=fast, -1=all/slow)")
    ap.add_argument("--max-comments", type=int, default=3000, help="Reddit comment cap per post")
    ap.add_argument("--min-score", type=int, default=None, help="skip Reddit comments below this score")
    ap.add_argument("--csv", metavar="FILE")
    ap.add_argument("--no-sec", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    valid = set() if args.no_sec else smg.load_valid_tickers()
    print(f"Ticker validation list: {len(valid):,} symbols" if valid
          else "No SEC list: cashtags + company names only", file=sys.stderr)

    records, n_threads = [], 0
    if "4chan" in args.sources:
        recs, n_threads = fetch_4chan_records(args.pattern, args.days)
        records += recs
    if "reddit" in args.sources:
        import reddit_scraper
        records += reddit_scraper.fetch_records(
            days=args.days, subreddit=args.subreddit,
            replace_more=None if args.replace_more < 0 else args.replace_more,
            max_comments_per_post=args.max_comments, min_score=args.min_score)

    if not records:
        print("No records collected.")
        return

    stats, info = analyze(records, valid, args.max_attr)
    rows = [r for r in summarize(stats)
            if r["total"] >= args.min_count and r["n_scored"] >= args.min_scored]

    # ------------------------------ report ------------------------------
    W = 92
    print("\n" + "=" * W)
    print(f"TICKER CHATTER - last {args.days:g} day(s)   sources: {', '.join(args.sources)}")
    print("=" * W)
    print(f"{'TICKER':<8}{'TOTAL':>6}{'4CHAN':>7}{'REDDIT':>8} | "
          f"{'SENT':>6}{'4ch':>7}{'rdt':>7} | {'BULL':>4} {'BEAR':>4}  {'N':>5}  VERDICT")
    print("-" * W)
    for r in rows[: args.top]:
        verdict = sentiment.label(r["sent_all"]) if r["sent_all"] is not None else "n/a"
        print(f"{r['ticker']:<8}{r['total']:>6}{r['4chan']:>7}{r['reddit']:>8} | "
              f"{f(r['sent_all']):>6}{f(r['sent_4chan']):>7}{f(r['sent_reddit']):>7} | "
              f"{pct(r['pct_bull']):>4} {pct(r['pct_bear']):>4}  {r['n_scored']:>5}  {verdict}")
    print("=" * W)
    print("SENT = avg VADER score (-1..+1).  BULL/BEAR = share of posts scoring > +0.05 / < -0.05.")
    print("N = posts used for sentiment (posts naming > --max-attr tickers are counted but not scored).")

    print("\nPER-SOURCE STATS")
    for src in args.sources:
        i = info[src]
        skipped = sum(i["skipped"].values())
        print(f"  [{src}] collected={i['total']}  analyzed={i['analyzed']}  filtered={skipped} "
              f"({', '.join(f'{k}={v}' for k, v in i['skipped'].most_common()) or 'none'})  "
              f"with_tickers={i['with_tickers']}"
              + (f" ({i['with_tickers'] / i['analyzed']:.1%})" if i["analyzed"] else ""))
    if "4chan" in args.sources:
        print(f"  [4chan] threads matched: {n_threads}")

    # tickers where the two communities disagree
    both = [r for r in rows if r["sent_4chan"] is not None and r["sent_reddit"] is not None
            and r["4chan"] >= 3 and r["reddit"] >= 3]
    both.sort(key=lambda r: abs(r["sent_4chan"] - r["sent_reddit"]), reverse=True)
    if both and len(args.sources) == 2:
        print("\nBIGGEST 4CHAN-vs-REDDIT SENTIMENT GAPS (>=3 mentions each)")
        for r in both[:5]:
            print(f"  {r['ticker']:<6} 4chan {r['sent_4chan']:+.2f}  reddit {r['sent_reddit']:+.2f}  "
                  f"gap {abs(r['sent_4chan'] - r['sent_reddit']):.2f}")

    print(f"\nUnique tickers: {len(stats)}   Runtime: {time.time() - t0:.1f}s")

    if args.csv:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        all_rows = summarize(stats)
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ticker", "total_mentions", "mentions_4chan", "mentions_reddit",
                        "sentiment_all", "sentiment_4chan", "sentiment_reddit",
                        "pct_bullish", "pct_bearish", "posts_scored",
                        "window_days", "scraped_at_utc"])
            for r in all_rows:
                w.writerow([r["ticker"], r["total"], r["4chan"], r["reddit"],
                            *(("" if r[k] is None else round(r[k], 4))
                              for k in ("sent_all", "sent_4chan", "sent_reddit",
                                        "pct_bull", "pct_bear")),
                            r["n_scored"], args.days, now])
        print(f"Saved {len(all_rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
