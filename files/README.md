# wsb-ticker-sentiment

A small personal research script, meant to be run about once a day, that compares how often stock
tickers come up across a few public sources and whether the tone is bullish or bearish.
**Personal, non-commercial. Not financial advice.**

## Sources

| Source | What is used | Sentiment from |
|---|---|---|
| 4chan `/biz/` `/smg/` threads | Public posts, via 4chan's read-only JSON API (max 1 request/sec). Text is processed in memory; only counts and scores are output and saved | [VADER](https://github.com/cjhutto/vaderSentiment) + a slang lexicon |
| r/wallstreetbets | Ticker mention counts, upvotes and ranks from [ApeWisdom](https://apewisdom.io)'s public feed. **This project does not access Reddit directly** | none (counts/rank only) |
| StockTwits | The trending-symbols list and the site's own published sentiment score per symbol. Message text and message counts are **not** used | StockTwits' score, `(score - 50) / 50` |

Credit: Reddit mention data is provided by ApeWisdom.

## Buying / selling pressure (not order counts)

Counts of buy orders vs sell orders aren't public, so the project uses free proxies instead:

- **Up-volume share (20d)** and **Chaikin Money Flow (20d)**, computed from daily prices
  (methods in `technical_analysis.py`).
- **FINRA daily short-sale volume**: short volume / total volume per ticker, from FINRA's public
  consolidated files (`flow.py` downloads them; about 20 small files on the first run, then only new
  days). It excludes exchange-reported volume and includes market-maker hedging, so a high ratio is
  not the same as bearish bets; the dashboard backtest compares each ticker with its own 20-day
  average. See the docstring in `flow.py` for how to read it.

They appear as CSV columns (`updown_vol_share_20`, `cmf_20`, `short_ratio_1d/5d/20d`,
`short_vol_date`), as leaderboard columns and per-ticker charts in the dashboard, and in a new
dashboard backtest table. `--no-short` skips the FINRA download; `--short-days N` sets how much
history is kept. Check FINRA's terms of use before relying on the files.

## Dashboard

`python dashboard.py` turns `history.db` into one self-contained `dashboard.html` (no internet, no
external libraries; light/dark follows your system). Open it in a browser. It has a sortable
leaderboard with trend sparklines, a per-ticker view (click a ticker or use the picker; Esc closes)
with sentiment, mention, price/SMA and OBV charts, a "what changed since the previous run day"
panel, sentiment and attention heatmaps, and the backtest, source scorecard and Reddit-attention
tables. Add `--dashboard` to `combined_report.py` to rebuild it at the end of each run.

To preview it before you have real history: `python make_demo_db.py` then
`python dashboard.py --db demo_history.db --out demo.html`. The demo data is synthetic and the page
shows a red banner saying so.

The queries live in `dashboard_data.py` (plain Python data, no HTML), so a Streamlit front end can
reuse them later. The dashboard contains StockTwits and ApeWisdom data: check their terms before
publishing it anywhere public.

## 4chan coverage

- `--pattern` takes several title substrings (a thread matches if its title contains any), e.g.
  `--pattern /smg/ "stock market general"`. `python combined_report.py --debug-threads` lists live
  threads that look like a stock general and whether the pattern matches them.
- The script remembers the /smg/ thread IDs it has seen and re-checks them for a few days after they
  leave the catalog (archived threads stay readable for a while), so a thread that fills up and is
  replaced between two runs is not lost. Running more often helps too.
- Trade posts such as `ARM 140c 10/18 @ 2.35 -> 3.10 +32%` are no longer discarded as symbol spam.
- Tickers that are also ordinary words (ARM, NOW, OPEN, ON, LOW, KEY...) are counted in ALL CAPS when
  used like a ticker (`ARM 140c`, `shares of OPEN`, `NVDA, ARM, AMD`), but not in "buy NOW" or shouting.
- The run log shows how many posts were fetched and how many fall inside the `--days` window.

## Checking that mentions are being counted

`python combined_report.py --debug-ticker SOXL` re-scans the live /smg/ threads and prints every
post that mentions the symbol, whether it was counted, and if not, exactly why (lowercase, missing
from the SEC list, ambiguous word, filtered as junk, older than `--days`, only in a subject line...).

Ticker detection uses the SEC ticker list plus a built-in list of popular ETFs (SOXL, TQQQ, SPY,
UVXY...) that is matched in any case. Add your own symbols to `known_tickers.txt`, one per line.
Only add symbols that aren't everyday English words.

## Technical columns (CSV)

When `--csv` is used, each ticker also gets: latest daily close (`price`, with `price_date`),
15/30/60-trading-day simple moving averages, and on-balance volume (OBV, accumulated over the last
90 trading days). Methods are documented in `technical_analysis.py`. Prices come from
[Tiingo](https://www.tiingo.com) (free API key in `TIINGO_API_KEY`); `yfinance` is an unofficial
no-key fallback. Use `--no-ta` to skip. Prices are end-of-day, not live quotes.

## History and backtesting

Every run is saved to a local SQLite file (`history.db`): per-ticker mentions, ranks, sentiment,
StockTwits' score, price, moving averages and OBV, plus a cache of daily prices. No post text is
stored.

**Run as often as you like.** Every run is kept, and runs are grouped by New York calendar date. For
all comparisons (backtest, scorecard, attention study, the dashboard's trends and heatmaps) each
ticker on each day is the **average of that day's runs**. Returns are measured from that day's market
close (a weekend or holiday run uses the last close before it) over 1, 3 and 5 trading days, and the
same window for SPY to separate out market-wide moves. Nothing is scored until a later close exists.
The dashboard also draws an intraday chart (one point per saved run) for the last three days.

- **Earlier calls vs what happened**: the latest earlier day's average sentiment next to the move.
- **Backtest**: cumulative hit rates, average later returns for bullish vs bearish calls, and
  correlation, per source and for the composite.
- **Source scorecard**: each source's hit rate (judged against SPY) with a 95% confidence range. Once
  4chan and StockTwits each have 50+ calls it prints a *suggested* `--weights` line, moved only halfway
  from your current weights; it never applies it.
- **Reddit attention study**: tickers bucketed by the change in r/wallstreetbets mentions vs the prior
  24h, with later returns and move size for each bucket.

`--no-save` skips saving, `--no-backtest` skips the scoring output, `--db FILE` chooses the file.
Methods and caveats are at the top of `history.py`.

### Running more than once a day

- Each run costs requests: StockTwits and 4chan are queried every time, and ApeWisdom's counts only
  refresh about hourly. Saved prices are reused, so repeat runs make no price requests.
- Run after the close at least once a day so the day's closing prices exist for scoring.
- `--csv` accepts date placeholders, e.g. `--csv "report_%Y-%m-%d_%H%M.csv"`.
- For extra looks you don't want saved, use `--no-save`.
- Don't start a run while another is still going (they share one database file).

## Running more than once a day

Everything works, with these behaviours:

- All runs are saved, but only **one run per date** feeds the statistics: the one with the most priced
  tickers (newest wins ties), so a later partial run can't replace a complete one.
- Prices already saved for the latest completed session are reused, so repeat runs make no price
  requests (`--no-ta-cache` forces a re-download). StockTwits and 4chan are still queried every run;
  ApeWisdom's counts only refresh about hourly.
- `--csv` accepts date placeholders so runs don't overwrite each other, e.g.
  `--csv "report_%Y-%m-%d_%H%M.csv"`.
- For extra intraday looks, use `--no-save` (and `--no-ta` if you don't need prices).
- Don't start a run while another is still going (they share one database file).

## Price data limits (Tiingo free plan)

Tiingo's free plan allows 50 requests per hour, 1,000 per day and 500 unique symbols per month
(check their pricing page for current numbers). Every ticker without saved prices costs one request, so
the script spends a bounded budget per run (`--ta-budget`, default 40), fetches SPY first, and works
down the list in the dashboard's order (highest heat first, `--ta-max` tickers). Tickers over budget
show as "deferred" and are filled in on later runs, because saved prices cost nothing; a full first
coverage can take a couple of hourly windows. Symbols the provider doesn't carry are remembered for a
week instead of being re-requested. If the provider rate-limits mid-run, the script stops and the
rest are retried later. The dashboard shows a banner and per-row tooltips explaining every ticker
that has no price. On a paid plan, raise `--ta-budget`.

## What it prints

Per ticker: a weighted attention score ("HEAT") from each source's rank, per-source mentions and
sentiment (NET, -1..+1), a weighted composite sentiment, and a consensus label
(unanimous / majority / split) across the sources that provide sentiment. Optional CSV output.

## Setup and usage

```bash
pip install -r requirements.txt
python combined_report.py --csv daily.csv
python combined_report.py --sources 4chan stocktwits      # skip a source
python combined_report.py --weights 4chan=0.25 reddit=1 stocktwits=1.5
```

Before running, set a real contact address in the `USER_AGENT` constants in `smg_scraper.py`,
`stocktwits_scraper.py` and `apewisdom_scraper.py`.

## Files

| File | Purpose |
|---|---|
| `combined_report.py` | Entry point: runs the sources, analyzes, prints the report |
| `smg_scraper.py` | 4chan `/smg/` fetcher, text cleaning, spam filter, ticker extraction |
| `apewisdom_scraper.py` | r/wallstreetbets mention data via ApeWisdom |
| `stocktwits_scraper.py` | StockTwits trending list + published sentiment scores |
| `sentiment.py` | VADER scoring with a slang lexicon |
| `known_tickers.txt` | Your extra always-recognised tickers (ETFs the SEC list misses) |
| `technical_analysis.py` | Price history, SMA 15/30/60 and OBV (also runnable on its own) |
| `dashboard.py` / `dashboard_data.py` | Static HTML dashboard / the queries behind it |
| `make_demo_db.py` | Synthetic sample database for previewing the dashboard |
| `flow.py` | FINRA short-sale volume download/storage and the buying/selling-pressure metrics |
| `history.py` | Saves each run to SQLite, scores earlier signals against later prices, backtest summary |

## Limitations

- StockTwits endpoints are undocumented and may change or block automated access; the script stops
  rather than working around a block. Check StockTwits' and ApeWisdom's terms before relying on them.
- ApeWisdom's counts are computed their way (about hourly); availability depends on them.
- Reddit has no sentiment here, so consensus compares 4chan and StockTwits only.
- VADER is rule-based and struggles with sarcasm; read results as aggregate trends.
- Forum sentiment is a noisy, often contrarian signal. **Nothing here is financial advice.**
