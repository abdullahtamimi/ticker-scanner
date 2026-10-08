# wsb-ticker-sentiment

A small personal research script that counts how often stock tickers are mentioned in public forum discussions and estimates the overall tone (bullish / bearish) of those mentions.

**This is a non-commercial, personal project.** It is not a product, service, bot, or app for other users.

## What it does

1. **Collects public text** from two sources over a short lookback window (default 7 days, configurable to 3 or 1):
   - Public posts and comments on r/wallstreetbets, via the Reddit Data API (using [PRAW](https://praw.readthedocs.io/)).
   - Public threads titled `/smg/` on 4chan's `/biz/` board, via 4chan's official read-only JSON API.
2. **Cleans and filters** the text (removes URLs, quoted text, bot accounts, deleted/removed content, duplicates, and spam).
3. **Detects tickers** using cashtags (`$NVDA`), validated bare symbols (checked against the SEC's public ticker list), and a small table of company names.
4. **Scores sentiment** per post with [VADER](https://github.com/cjhutto/vaderSentiment) plus a custom slang lexicon.
5. **Prints an aggregate report** (and optionally writes a CSV) with per-ticker mention counts and average sentiment.

## Reddit API usage

| | |
|---|---|
| **Purpose** | Personal, non-commercial analysis of aggregate ticker mentions and sentiment |
| **Access type** | Read-only (`reddit.read_only = True`) |
| **Data accessed** | Public posts and comments from r/wallstreetbets |
| **Write actions** | None. No posting, commenting, voting, messaging, or moderation actions |
| **Volume** | A few hundred requests per run, run manually and on demand (no continuous polling or scheduled scraping) |
| **Rate limiting** | Handled by PRAW, which respects Reddit's rate-limit headers |
| **Storage** | Raw post/comment text is processed in memory and **not stored or published**. Only aggregate counts and sentiment averages are output |
| **Redistribution** | Reddit content is not republished, shared, sold, or redistributed |
| **AI/ML training** | Reddit content is **not** used to train or fine-tune any machine-learning model. Sentiment uses a pre-built, rule-based lexicon scorer |
| **Deleted content** | Posts and comments marked `[deleted]` / `[removed]` are skipped |

The script authenticates as a "script" app and identifies itself with a descriptive User-Agent that includes the developer's Reddit username.

## Setup

```bash
pip install -r requirements.txt

export REDDIT_CLIENT_ID="..."
export REDDIT_CLIENT_SECRET="..."
export REDDIT_USER_AGENT="wsb-ticker-sentiment by u/YOUR_USERNAME"
```

Credentials are read from environment variables and are never stored in the repository.

## Usage

```bash
python combined_report.py                          # 4chan + Reddit, last 7 days
python combined_report.py --days 3 --csv out.csv   # last 3 days, save CSV
python combined_report.py --sources 4chan          # 4chan only (no Reddit credentials needed)
python combined_report.py --sources reddit         # Reddit only
```

## Files

| File | Purpose |
|---|---|
| `combined_report.py` | Entry point: runs the sources, analyzes, prints the report |
| `reddit_scraper.py` | Fetches posts/comments from a subreddit for the last N days |
| `smg_scraper.py` | 4chan `/smg/` fetcher, text cleaning, spam filter, and ticker extraction |
| `sentiment.py` | VADER sentiment scoring with a slang lexicon |

## Limitations

- Sentiment scoring is rule-based and struggles with sarcasm; treat results as aggregate trends, not per-post truth.
- Reddit listings are capped at roughly 1,000 recent posts, so very busy weeks may be incompletely covered.
- Forum sentiment is a noisy signal. **Nothing here is financial advice.**

## Compliance

This project follows Reddit's [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy) and Data API Terms, and 4chan's [API rules](https://github.com/4chan/4chan-API) (max 1 request/second).
