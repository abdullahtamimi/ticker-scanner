"""
reddit_scraper.py - Pull posts + comments from a subreddit for the last N days.

Setup (one time):
  1. pip install praw
  2. Go to https://www.reddit.com/prefs/apps -> "create another app" -> type: script
     (redirect uri can be http://localhost:8080)
  3. Export credentials:
        export REDDIT_CLIENT_ID="xxxxxxxx"
        export REDDIT_CLIENT_SECRET="yyyyyyyy"
        export REDDIT_USER_AGENT="smg-ticker-scraper by u/your_username"

Use is subject to Reddit's Data API terms (free for personal/non-commercial use,
~100 requests/min; PRAW sleeps automatically to respect limits).

This module just returns normalized records; combined_report.py does the analysis.
"""
import html
import os
import re
import sys
import time

import smg_scraper as smg

# WSB-specific words that are also real tickers or just noise -> cashtag-only.
smg.AMBIGUOUS.update({
    "GUH", "RIP", "EDIT", "PUTS", "CALLS", "MOON", "FD", "FDS", "DTE", "IV",
    "OTM", "ITM", "ATH", "ATL", "LMAO", "TLDR", "HOLY", "DAMN", "FUCK", "SHIT",
    "WSB", "MOD", "MODS", "REEE", "GAINS", "LOSS", "PORN", "TENDIES", "APES",
    "APE", "RH", "ER", "AH", "PT", "YTD", "QOQ", "YOY", "TLDR", "SPAC", "EV",
    "AI", "PUT", "CALL", "LONG", "SHORT", "GREEN", "RED",
})

BOT_AUTHORS = {"AutoModerator", "VisualMod", "RemindMeBot", "WSBVoteBot"}
REMOVED = {"[deleted]", "[removed]", ""}

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def clean_reddit_text(raw):
    """Markdown -> plain text; drops quoted lines (someone else's words) and URLs."""
    if not raw:
        return ""
    text = html.unescape(raw)
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    text = " ".join(lines)
    text = MD_LINK_RE.sub(r"\1", text)
    text = smg.URL_RE.sub(" ", text)
    text = re.sub(r"[*_~`#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def fetch_records(days=7, subreddit="wallstreetbets", max_posts=1000,
                  replace_more=8, max_comments_per_post=3000, min_score=None):
    """
    Return a list of records:
        {"source": "reddit", "id", "ts", "text", "score", "kind": "post"|"comment"}

    replace_more: how many "load more comments" expansions to follow per post.
                  Each costs one API call (~100 comments). 0 = top-level only,
                  None = everything (slow on 10k-comment daily threads).
    """
    try:
        import praw
    except ImportError:
        sys.exit("praw not installed: pip install praw")

    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET (see reddit_scraper.py docstring).")

    reddit = praw.Reddit(
        client_id=cid,
        client_secret=secret,
        user_agent=os.environ.get("REDDIT_USER_AGENT", "smg-ticker-scraper/1.0"),
    )
    reddit.read_only = True
    sub = reddit.subreddit(subreddit)
    cutoff = time.time() - days * 86400

    # 1) collect submissions inside the window. .new() caps near ~1000 items, so
    #    also merge in .top() to catch popular posts the cap might have cut off.
    submissions = {}
    for s in sub.new(limit=max_posts):
        if s.created_utc < cutoff:
            break
        submissions[s.id] = s
    tf = "day" if days <= 1 else "week" if days <= 7 else "month"
    for s in sub.top(time_filter=tf, limit=200):
        if s.created_utc >= cutoff:
            submissions.setdefault(s.id, s)
    _log(f"[reddit] {len(submissions)} posts in the last {days} day(s) from r/{subreddit}")

    records = []
    for n, s in enumerate(sorted(submissions.values(), key=lambda x: x.created_utc), 1):
        author = s.author.name if s.author else None
        body = f"{s.title}. {s.selftext}" if getattr(s, "selftext", "") else s.title
        if author not in BOT_AUTHORS:
            records.append({"source": "reddit", "id": s.id, "ts": int(s.created_utc),
                            "text": clean_reddit_text(body), "score": s.score, "kind": "post"})

        if s.num_comments == 0:
            continue
        try:
            s.comment_sort = "top"
            s.comments.replace_more(limit=replace_more)
            comments = s.comments.list()
        except Exception as e:  # network hiccup, deleted post, etc.
            _log(f"[reddit] skipped comments for {s.id}: {e}")
            continue

        kept = 0
        for c in comments[:max_comments_per_post]:
            if c.created_utc < cutoff:
                continue
            if c.body in REMOVED or (c.author and c.author.name in BOT_AUTHORS):
                continue
            if min_score is not None and c.score < min_score:
                continue
            records.append({"source": "reddit", "id": c.id, "ts": int(c.created_utc),
                            "text": clean_reddit_text(c.body), "score": c.score,
                            "kind": "comment"})
            kept += 1
        _log(f"[reddit] {n}/{len(submissions)}  {s.id}  {kept:>5} comments  {s.title[:50]!r}")

    return records
