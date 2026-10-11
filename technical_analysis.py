#!/usr/bin/env python3
"""
technical_analysis.py - Daily price history + technical indicators for a list of tickers.

Used by combined_report.py to add columns to the CSV, and runnable on its own:

    export TIINGO_API_KEY="..."            # free key: https://www.tiingo.com
    python technical_analysis.py NVDA TSLA AAPL
    python technical_analysis.py NVDA --provider yfinance

=====================================================================================
HOW EACH METRIC IS ACQUIRED / CALCULATED
=====================================================================================
Raw data (get_bars -> fetch_bars_tiingo / fetch_bars_yfinance)
    One row per trading day, oldest -> newest: (date, close, volume).
    Close and volume are the provider's ADJUSTED values (split- and dividend-adjusted),
    so a stock split doesn't show up as a fake price crash or volume spike in the
    averages / OBV. About LOOKBACK_CALENDAR_DAYS (160) calendar days (~105 trading days)
    are fetched: enough for the 60-day average plus margin for holidays.

current price  (current_price)
    The most recent daily close in the data, plus its date in `price_date`.
    This is an END-OF-DAY price: if you run the report before the market closes, you get
    the previous completed session. It is not a live quote.

SMA 15 / 30 / 60  (sma)
    Simple moving average = arithmetic mean of the last N daily closes:
        SMA_N = (close[t] + close[t-1] + ... + close[t-N+1]) / N
    "N-day" means N TRADING days (bars), not calendar days. If fewer than N bars exist
    (e.g. a recent IPO) the value is None / blank rather than a partial average.

On-Balance Volume  (obv_series / obv)
    Cumulative volume flow (Granville):
        OBV[0] = 0
        OBV[i] = OBV[i-1] + volume[i]   if close[i] >  close[i-1]
        OBV[i] = OBV[i-1] - volume[i]   if close[i] <  close[i-1]
        OBV[i] = OBV[i-1]               if close[i] == close[i-1]
    The value reported is OBV at the latest bar, accumulated over the last OBV_BARS (90)
    trading days. IMPORTANT: OBV's absolute level depends on where the accumulation
    starts, so compare its DIRECTION and size relative to the stock's typical volume
    (rising OBV with rising price = volume confirming the move), not raw numbers across
    different tickers, and not across daily reports as the 90-day window slides.

Up-volume share, 20 days  (up_down_volume_share)  -- a "buying vs selling volume" proxy
    Over the last 20 bars, volume on days that closed UP divided by volume on up days plus volume
    on DOWN days (flat days ignored). 0.50 = balanced; above = more volume on up days. It
    classifies a whole day at once, so it is a crude stand-in for buy/sell volume.

Chaikin Money Flow, 20 days  (chaikin_money_flow)  -- accumulation vs distribution
    For each day: multiplier = ((close - low) - (high - close)) / (high - low), i.e. +1 when the
    day closes at its high, -1 at its low, 0 mid-range (0 if high == low). CMF = sum(multiplier *
    volume) / sum(volume) over 20 days, so it ranges -1..+1; positive = closes tend to land in the
    upper part of the range on volume (buying pressure), negative = the opposite.

Data providers
    tiingo    : documented API, free key (TIINGO_API_KEY), adjusted end-of-day data. Default
                when the key is set. Check Tiingo's current free-tier limits and terms.
    yfinance  : no key, but unofficial (it reads Yahoo Finance's endpoints), can break
                without notice, and Yahoo's terms may restrict automated use. Fallback only.
=====================================================================================
Not financial advice.
"""
import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests

LOOKBACK_CALENDAR_DAYS = 160
OBV_BARS = 90
SMA_WINDOWS = (15, 30, 60)
FLOW_WINDOW = 20
COLUMNS = ["price", "price_date", "sma_15", "sma_30", "sma_60", "obv",
           "updown_vol_share_20", "cmf_20"]

TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"
USER_AGENT = "wsb-ticker-sentiment/1.0 (personal research; contact: you@example.com)"


class DataError(Exception):
    """Price data couldn't be obtained for a symbol (or at all)."""


class NotFound(DataError):
    """The provider doesn't carry this symbol (e.g. some ETFs, foreign or OTC listings)."""


class RateLimited(DataError):
    """The provider answered HTTP 429; further requests would just be refused."""


@dataclass(frozen=True)
class Bar:
    date: str       # YYYY-MM-DD
    close: float    # adjusted close
    volume: float   # adjusted volume
    high: float = None   # adjusted daily high (None if the provider didn't supply it)
    low: float = None    # adjusted daily low


# ----------------------------------------------------------------------------
# 1) DATA ACQUISITION
# ----------------------------------------------------------------------------
def fetch_bars_tiingo(symbol, lookback_days=LOOKBACK_CALENDAR_DAYS, api_key=None, timeout=20):
    """Daily bars from Tiingo's end-of-day endpoint (adjClose / adjVolume), oldest first."""
    key = api_key or os.environ.get("TIINGO_API_KEY")
    if not key:
        raise DataError("TIINGO_API_KEY is not set (free key at https://www.tiingo.com).")
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    r = requests.get(
        TIINGO_URL.format(symbol=symbol.replace(".", "-").lower()),
        params={"startDate": start, "resampleFreq": "daily"},
        headers={"Authorization": f"Token {key}", "User-Agent": USER_AGENT},
        timeout=timeout,
    )
    if r.status_code == 404:
        raise NotFound(f"{symbol}: not found at Tiingo")
    if r.status_code == 429:
        raise RateLimited("Tiingo rate limit reached (HTTP 429)")
    if r.status_code in (401, 403):
        raise DataError(f"Tiingo rejected the request (HTTP {r.status_code}) - check TIINGO_API_KEY")
    r.raise_for_status()
    bars = []
    for row in r.json():
        close = row.get("adjClose")
        if close is None:
            continue
        hi, lo = row.get("adjHigh"), row.get("adjLow")
        bars.append(Bar(str(row["date"])[:10], float(close), float(row.get("adjVolume") or 0),
                        float(hi) if hi is not None else None, float(lo) if lo is not None else None))
    bars.sort(key=lambda b: b.date)
    if not bars:
        raise DataError(f"{symbol}: Tiingo returned no price rows")
    return bars


def fetch_bars_yfinance(symbol, lookback_days=LOOKBACK_CALENDAR_DAYS):
    """Daily bars via the unofficial yfinance library (auto-adjusted), oldest first."""
    try:
        import yfinance as yf
    except ImportError:
        raise DataError("yfinance is not installed (pip install yfinance)")
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    try:
        df = yf.Ticker(symbol.replace(".", "-")).history(start=start, interval="1d",
                                                         auto_adjust=True)
    except Exception as e:
        raise DataError(f"{symbol}: yfinance error: {e}")
    if df is None or df.empty:
        raise NotFound(f"{symbol}: yfinance returned no rows")
    bars = []
    for idx, row in df.iterrows():
        close, vol = row["Close"], row["Volume"]
        if close != close:  # NaN
            continue
        hi, lo = row.get("High"), row.get("Low")
        bars.append(Bar(idx.strftime("%Y-%m-%d"), float(close), float(vol) if vol == vol else 0.0,
                        float(hi) if hi is not None and hi == hi else None,
                        float(lo) if lo is not None and lo == lo else None))
    bars.sort(key=lambda b: b.date)
    return bars


def resolve_provider(provider="auto"):
    """'auto' -> tiingo if TIINGO_API_KEY is set, else yfinance if installed."""
    if provider in ("tiingo", "yfinance"):
        return provider
    if os.environ.get("TIINGO_API_KEY"):
        return "tiingo"
    try:
        import yfinance  # noqa: F401
        return "yfinance"
    except ImportError:
        raise DataError("No price provider available: set TIINGO_API_KEY (free key at "
                        "https://www.tiingo.com) or `pip install yfinance`.")


def get_bars(symbol, provider="auto", lookback_days=LOOKBACK_CALENDAR_DAYS):
    provider = resolve_provider(provider)
    if provider == "tiingo":
        return fetch_bars_tiingo(symbol, lookback_days)
    return fetch_bars_yfinance(symbol, lookback_days)


# ----------------------------------------------------------------------------
# 2) INDICATORS (pure functions - no network, easy to test)
# ----------------------------------------------------------------------------
def current_price(bars):
    """(latest close, its date), or (None, None) if there are no bars."""
    if not bars:
        return None, None
    return bars[-1].close, bars[-1].date


def sma(values, window):
    """Simple moving average of the last `window` values; None if there aren't enough."""
    if window <= 0:
        raise ValueError("window must be positive")
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def obv_series(closes, volumes):
    """On-Balance Volume at every bar (starts at 0). See module docstring for the rule."""
    if len(closes) != len(volumes):
        raise ValueError("closes and volumes must be the same length")
    out, total = [], 0.0
    for i in range(len(closes)):
        if i > 0:
            if closes[i] > closes[i - 1]:
                total += volumes[i]
            elif closes[i] < closes[i - 1]:
                total -= volumes[i]
        out.append(total)
    return out


def obv(closes, volumes, bars=OBV_BARS):
    """OBV at the latest bar, accumulated over the last `bars` bars; None if < 2 bars."""
    c, v = closes[-bars:], volumes[-bars:]
    if len(c) < 2:
        return None
    return obv_series(c, v)[-1]


def up_down_volume_share(closes, volumes, window=FLOW_WINDOW):
    """Share of (up-day + down-day) volume that traded on up days over the last `window` bars.
    None if there aren't window+1 closes or no up/down volume."""
    if len(closes) < window + 1:
        return None
    up = down = 0.0
    for i in range(len(closes) - window, len(closes)):
        if closes[i] > closes[i - 1]:
            up += volumes[i]
        elif closes[i] < closes[i - 1]:
            down += volumes[i]
    return up / (up + down) if up + down > 0 else None


def chaikin_money_flow(highs, lows, closes, volumes, window=FLOW_WINDOW):
    """Chaikin Money Flow over the last `window` bars (-1..+1); None if highs/lows are missing,
    there are too few bars, or no volume."""
    if len(closes) < window:
        return None
    hs, ls = highs[-window:], lows[-window:]
    if any(h is None or l is None for h, l in zip(hs, ls)):
        return None
    mfv = vol = 0.0
    for h, l, c, v in zip(hs, ls, closes[-window:], volumes[-window:]):
        rng = h - l
        mult = 0.0 if rng <= 0 else ((c - l) - (h - c)) / rng
        mfv += mult * v
        vol += v
    return mfv / vol if vol > 0 else None


# ----------------------------------------------------------------------------
# 3) PUTTING IT TOGETHER
# ----------------------------------------------------------------------------
def analyze_bars(bars):
    """All requested metrics from a list of Bars (oldest -> newest)."""
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    price, price_date = current_price(bars)
    return {
        "price": price,
        "price_date": price_date,
        **{f"sma_{n}": sma(closes, n) for n in SMA_WINDOWS},
        "obv": obv(closes, volumes),
        "updown_vol_share_20": up_down_volume_share(closes, volumes),
        "cmf_20": chaikin_money_flow(highs, lows, closes, volumes),
        "bars_used": len(bars),
    }


def analyze(symbol, provider="auto"):
    return analyze_bars(get_bars(symbol, provider))


def analyze_many(symbols, provider="auto", delay=0.5, log=None):
    """
    {symbol: metrics dict}. A symbol that fails gets {"error": "..."} (metrics blank in the
    CSV). Raises DataError up front only if no provider can be used at all. If the provider
    rate-limits (HTTP 429) we stop and mark the remaining symbols as skipped.
    """
    log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
    provider = resolve_provider(provider)
    if provider == "yfinance":
        log("[ta] using yfinance (unofficial; may break or be restricted by Yahoo's terms)")
    results, limited = {}, False
    for i, sym in enumerate(symbols, 1):
        if limited:
            results[sym] = {"error": "skipped after rate limit", "status": "rate_limited"}
            continue
        try:
            bars = get_bars(sym, provider)
            results[sym] = analyze_bars(bars)
            results[sym]["bars"] = bars      # kept so history.py can cache the daily prices
            results[sym]["status"] = "ok"
        except RateLimited as e:
            limited = True
            results[sym] = {"error": str(e), "status": "rate_limited"}
            log(f"[ta] {e}; skipping the remaining symbols")
        except NotFound as e:
            results[sym] = {"error": str(e), "status": "not_found"}
        except DataError as e:
            results[sym] = {"error": str(e), "status": "error"}
        except Exception as e:  # network hiccup etc.
            results[sym] = {"error": f"{type(e).__name__}: {e}", "status": "error"}
        if i % 10 == 0:
            log(f"[ta] {i}/{len(symbols)}")
        time.sleep(delay)
    ok = sum(1 for r in results.values() if "error" not in r)
    log(f"[ta] price data for {ok}/{len(symbols)} symbols ({provider})")
    for sym, r in results.items():
        if "error" in r and not r["error"].startswith("skipped"):
            log(f"[ta]   {sym}: {r['error']}")
    return results


def row_values(result):
    """Values for COLUMNS from an analyze result (blanks if missing/failed)."""
    if not result or "error" in result:
        return [""] * len(COLUMNS)
    out = []
    for col in COLUMNS:
        v = result.get(col)
        if v is None:
            out.append("")
        elif col == "obv":
            out.append(round(v))
        elif isinstance(v, float):
            out.append(round(v, 4))
        else:
            out.append(v)
    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Price, 15/30/60-day SMA and OBV for tickers")
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--provider", choices=["auto", "tiingo", "yfinance"], default="auto")
    args = ap.parse_args()
    try:
        res = analyze_many([s.upper() for s in args.symbols], args.provider)
    except DataError as e:
        sys.exit(str(e))
    print(f"\n{'SYMBOL':<8}{'PRICE':>10} {'DATE':<11}{'SMA15':>10}{'SMA30':>10}{'SMA60':>10}{'OBV':>16}"
          f"{'UP-VOL%':>9}{'CMF20':>8}")
    for sym, r in res.items():
        if "error" in r:
            print(f"{sym:<8}  (no data: {r['error']})")
            continue
        f = lambda x: "-" if x is None else f"{x:,.2f}"
        print(f"{sym:<8}{f(r['price']):>10} {r['price_date']:<11}{f(r['sma_15']):>10}"
              f"{f(r['sma_30']):>10}{f(r['sma_60']):>10}{f(r['obv']):>16}"
              f"{('-' if r['updown_vol_share_20'] is None else format(r['updown_vol_share_20'], '.0%')):>9}"
              f"{('-' if r['cmf_20'] is None else format(r['cmf_20'], '+.2f')):>8}")


if __name__ == "__main__":
    main()
