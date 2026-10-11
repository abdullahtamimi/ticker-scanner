#!/usr/bin/env python3
"""
dashboard.py - Build a single self-contained dashboard.html from history.db.

    python dashboard.py                              # history.db -> dashboard.html
    python dashboard.py --db demo_history.db --out demo.html
    python combined_report.py --dashboard            # regenerate it at the end of a daily run

The page has no external dependencies (no CDN, no network): charts are inline SVG drawn here,
and a few lines of JavaScript handle column sorting and the ticker picker. Open the file in any
browser; light/dark follows your system setting.

Data access lives in dashboard_data.py (plain Python data, no HTML) so a Streamlit app can
reuse it later. Not financial advice.
"""
import argparse
import html
import math
import sys
from datetime import datetime

import dashboard_data as D

esc = html.escape

# ----------------------------------------------------------------------------
# formatting helpers
# ----------------------------------------------------------------------------
def fnum(v, spec=",.0f", dash="-"):
    return dash if v is None else format(v, spec)


def fpct(v, digits=1, signed=True, dash="-"):
    if v is None:
        return dash
    if abs(v) < 0.5 * 10 ** -(digits + 2):
        v = 0.0                      # avoid "-0%" / "+0%" for tiny values
    return format(v, f"{'+' if signed and v != 0 else ''}.{digits}%")


def fsent(v):
    return "-" if v is None else f"{v:+.2f}"


def sign_cls(v):
    return "" if v is None or v == 0 else "pos" if v > 0 else "neg"


def sent_bg(v):
    """Diverging red/green for a -1..+1 sentiment value (always light enough for dark text)."""
    if v is None:
        return None
    v = max(-1.0, min(1.0, v))
    return f"hsl({142 if v >= 0 else 2},55%,{92 - 42 * abs(v):.0f}%)"


def attn_bg(t):
    return f"hsl(215,65%,{95 - 50 * t:.0f}%)"


def mmdd(date_str):
    return date_str[5:] if date_str and len(date_str) >= 10 else (date_str or "")


# ----------------------------------------------------------------------------
# inline SVG charts
# ----------------------------------------------------------------------------
def _segments(points):
    segs, cur = [], []
    for x, y in points:
        if y is None:
            if cur:
                segs.append(cur)
                cur = []
        else:
            cur.append((x, y))
    if cur:
        segs.append(cur)
    return segs


def nice_ticks(lo, hi, target=6, integer=False):
    """Round-number tick values inside [lo, hi] (1-2-2.5-5 steps); integers only if asked."""
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / max(target - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    norm = raw / mag
    step = mag * (1 if norm <= 1 else 2 if norm <= 2 else 2.5 if norm <= 2.5 else 5 if norm <= 5 else 10)
    if integer:
        step = max(1.0, math.ceil(step))
    start = math.ceil(lo / step - 1e-9) * step
    ticks, v = [], start
    while v <= hi + 1e-9 and len(ticks) < 12:
        ticks.append(round(v, 10))
        v += step
    return ticks if len(ticks) >= 2 else [lo, hi]


def line_chart(series, labels, width=560, height=190, fmt=None, ylim=None, zero_line=False,
               integer=False, ref=None):
    """
    series: [{"name", "values" (aligned with labels, None = gap), "color", "dash"(optional)}]
    Returns an <svg> string, or a placeholder if there is nothing to draw.
    """
    fmt = fmt or (lambda v: f"{v:,.2f}")
    vals = [v for s in series for v in s["values"] if v is not None]
    n = len(labels)
    if not vals or n == 0:
        return '<div class="empty">No data yet</div>'
    ml, mr, mt, mb = 54, 24, 10, 22
    iw, ih = width - ml - mr, height - mt - mb
    if zero_line and ref is None:
        ref = 0
    if ylim:
        lo, hi = ylim
    else:
        lo, hi = min(vals), max(vals)
        if ref is not None:
            lo, hi = min(lo, ref), max(hi, ref)
        if hi == lo:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.06
        lo, hi = lo - pad, hi + pad

    def X(i):
        return ml + (iw / 2 if n == 1 else iw * i / (n - 1))

    def Y(v):
        return mt + ih * (1 - (v - lo) / (hi - lo))

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" preserveAspectRatio="xMidYMid meet">']
    out.append('<g class="axis">')
    for v in nice_ticks(lo, hi, 6, integer):
        y = Y(v)
        out.append(f'<line x1="{ml}" x2="{width - mr}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<text x="{ml - 6}" y="{y + 3:.1f}" text-anchor="end">{esc(fmt(v))}</text>')
    step = max(1, math.ceil(n / 6))
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        if n - 1 - idx[-1] >= max(1, step // 2):
            idx.append(n - 1)
        else:
            idx[-1] = n - 1
    for i in idx:
        anchor = "end" if (i == n - 1 and n > 1 and len(labels[i]) > 6) else "middle"
        out.append(f'<text x="{X(i) + (mr - 4 if anchor == "end" else 0):.1f}" y="{height - 6}" '
                   f'text-anchor="{anchor}">{esc(labels[i])}</text>')
    out.append("</g>")
    if ref is not None and lo < ref < hi:
        out.append(f'<line class="zero" x1="{ml}" x2="{width - mr}" y1="{Y(ref):.1f}" y2="{Y(ref):.1f}"/>')
    dots = n <= 45
    for s in series:
        col = s.get("color", "var(--c1)")
        dash = f' stroke-dasharray="{s["dash"]}"' if s.get("dash") else ""
        pts = [(X(i), None if v is None else Y(v)) for i, v in enumerate(s["values"])]
        for seg in _segments(pts):
            if len(seg) >= 2:
                d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in seg)
                out.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2"{dash}/>')
        if dots:
            for i, v in enumerate(s["values"]):
                if v is not None:
                    out.append(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="3" fill="{col}">'
                               f'<title>{esc(s["name"])} {esc(labels[i])}: {esc(fmt(v))}</title></circle>')
    out.append("</svg>")
    return "".join(out)


def chart_card(title, series, labels, note=None, **kw):
    legend = "".join(f'<span class="lg"><i style="background:{s.get("color", "var(--c1)")}"></i>'
                     f'{esc(s["name"])}</span>' for s in series if len(series) > 1)
    return (f'<figure class="card"><figcaption><b>{esc(title)}</b>{legend}</figcaption>'
            f'{line_chart(series, labels, **kw)}'
            + (f'<p class="note">{esc(note)}</p>' if note else "") + "</figure>")


def spark(values, w=96, h=26, color="var(--c1)"):
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return '<span class="na">&ndash;</span>'
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    n = len(values)
    X = lambda i: 2 + (w - 4) * i / max(n - 1, 1)
    Y = lambda v: 2 + (h - 4) * (1 - (v - lo) / (hi - lo))
    path = "M" + " L".join(f"{X(i):.1f} {Y(v):.1f}" for i, v in pts)
    li, lv = pts[-1]
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}">'
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6"/>'
            f'<circle cx="{X(li):.1f}" cy="{Y(lv):.1f}" r="2.2" fill="{color}"/></svg>')


def ci_bar(hit, lo, hi, w=150, h=16):
    """0-100% track, tick at 50%, bar = 95% range, dot = observed hit rate."""
    if hit is None or lo is None:
        return "-"
    X = lambda p: 4 + (w - 8) * p
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" class="cibar">'
            f'<line x1="4" x2="{w - 4}" y1="{h / 2}" y2="{h / 2}" class="track"/>'
            f'<line x1="{X(.5)}" x2="{X(.5)}" y1="1" y2="{h - 1}" class="mid"/>'
            f'<rect x="{X(lo):.1f}" y="{h / 2 - 3}" width="{max(X(hi) - X(lo), 1):.1f}" height="6" rx="3" class="range"/>'
            f'<circle cx="{X(hit):.1f}" cy="{h / 2}" r="3.6" class="dot"/></svg>')


# ----------------------------------------------------------------------------
# page sections
# ----------------------------------------------------------------------------
def td(text, v=None, cls="", style=None, title=None):
    a = f' class="{cls}"' if cls else ""
    a += f' data-v="{v}"' if v is not None else ""
    a += f' style="{style}"' if style else ""
    a += f' title="{esc(title)}"' if title else ""
    return f"<td{a}>{text}</td>"


PRICE_REASON = {
    "deferred": "not downloaded yet: this run's price-download budget was used up (it will fill in on later runs)",
    "rate_limited": "the price provider's rate limit was hit before this ticker",
    "not_found": "the price provider doesn't carry this symbol (common for some ETFs, foreign or OTC listings)",
    "error": "the price lookup failed",
    "no_provider": "no price provider is configured (set TIINGO_API_KEY or install yfinance)",
    "not_requested": "outside the --ta-max cap",
    "disabled": "this run used --no-ta",
    None: "no price was saved for this run (it may predate status tracking)",
}


def price_cell(r):
    if r["price"] is not None:
        return td(fnum(r["price"], ",.2f"), r["price"])
    return td('<span class="na">&ndash;</span>', None, title=PRICE_REASON.get(r.get("price_status"), "no price saved"))


def price_banner(board, top):
    shown = board[:top]
    missing = [r for r in shown if r["price"] is None]
    if not missing:
        return ""
    counts = {}
    for r in missing:
        counts[r.get("price_status")] = counts.get(r.get("price_status"), 0) + 1
    parts = "; ".join(f"{n} &mdash; {esc(PRICE_REASON.get(k, 'unknown'))}" for k, n in
                      sorted(counts.items(), key=lambda kv: -kv[1]))
    return (f'<p class="warnbox"><b>Price data is missing for {len(missing)} of the {len(shown)} tickers shown.</b> '
            f'Reasons: {parts}. Hover a &ndash; in the Price column for the reason on each row.</p>')


def flow_cells(m):
    """Leaderboard cells for the buying/selling-pressure proxies."""
    cmf, ud = m.get("cmf_20"), m.get("updown_20")
    s1, s5, s20 = m.get("short_1d"), m.get("short_5d"), m.get("short_20d")
    dev = None if s5 is None or s20 is None else s5 - s20
    return [
        td(fsent(cmf), None if cmf is None else round(cmf, 4), sign_cls(cmf)),
        td(fpct(ud, 0, False), None if ud is None else round(ud, 4), sign_cls(None if ud is None else ud - 0.5)),
        td(fpct(s1, 0, False), None if s1 is None else round(s1, 4),
           title="" if not m.get("short_date") else f"FINRA short-sale volume ratio, {m['short_date']}"),
        td("-" if dev is None else f"{dev * 100:+.1f} pts", None if dev is None else round(dev, 4)),
    ]


def leaderboard_html(board, top):
    heads = [("Ticker", True), ("Heat", True), ("4ch", True), ("r/wsb", True), ("r/wsb 24h", True),
             ("4ch NET", True), ("STW 0-100", True), ("Composite", True), ("Consensus", True),
             ("Price", True), ("vs SMA15", True), ("vs SMA30", True), ("vs SMA60", True),
             ("CMF 20d", True), ("Up-vol 20d", True), ("Short 1d", True), ("Short 5d vs 20d", True),
             ("Sentiment trend", False), ("r/wsb trend", False)]
    out = ['<table class="sortable" id="board"><thead><tr>']
    for h, sortable in heads:
        out.append("<th>%s</th>" % esc(h) if sortable else '<th class="nosort">%s</th>' % esc(h))
    out.append("</tr></thead><tbody>")
    for r in board[:top]:
        comp = r["composite"]
        cells = [
            td(f'<a href="#t-{esc(r["ticker"])}"><b>{esc(r["ticker"])}</b></a>', r["ticker"]),
            td(fnum(r["heat"], ".2f"), r["heat"]),
            td(fnum(r["c4_mentions"]), r["c4_mentions"]),
            td(fnum(r["rd_mentions"]), r["rd_mentions"]),
            td(fpct(r["rd_chg"], 0), None if r["rd_chg"] is None else round(r["rd_chg"], 4),
               sign_cls(r["rd_chg"])),
            td(fsent(r["c4_net"]), r["c4_net"], sign_cls(r["c4_net"])),
            td(fnum(r["st_sentiment"]), r["st_sentiment"]),
            td(f'<b>{fsent(comp)}</b>', comp, "chip", f"background:{sent_bg(comp)};color:#111" if comp is not None else None),
            td(esc(D.short_consensus(r["consensus"])), None, "cons", title=r["consensus"] or ""),
            price_cell(r),
            td(fpct(r["vs_sma15"]), r["vs_sma15"], sign_cls(r["vs_sma15"])),
            td(fpct(r["vs_sma30"]), r["vs_sma30"], sign_cls(r["vs_sma30"])),
            td(fpct(r["vs_sma60"]), r["vs_sma60"], sign_cls(r["vs_sma60"])),
            *flow_cells(r.get("flow") or {}),
            td(spark(r["spark_comp"], color="var(--c2)")),
            td(spark(r["spark_rd"], color="var(--c1)")),
        ]
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def changes_html(ch, latest_date, prev_date):
    if ch is None:
        return '<p class="empty">Only one run is saved so far. Changes appear from the second run day.</p>'

    def chips(items, fmt):
        return "".join(f'<a class="tag" href="#t-{esc(t)}">{fmt(t, *rest)}</a>'
                       for t, *rest in items) or '<span class="na">none</span>'

    parts = [f'<p class="note">{esc(prev_date)} &rarr; {esc(latest_date)}</p><div class="grid3">']
    parts.append('<div class="card"><h4>New in the report</h4>'
                 + chips([(t,) for t in ch["new"]], lambda t: esc(t)) + "</div>")
    parts.append('<div class="card"><h4>Dropped out</h4>'
                 + ("".join(f'<span class="tag">{esc(t)}</span>' for t in ch["dropped"])
                    or '<span class="na">none</span>') + "</div>")
    parts.append('<div class="card"><h4>Sentiment flipped sign</h4>'
                 + chips(ch["flips"], lambda t, a, b: f"{esc(t)} <small>{a:+.2f} &rarr; {b:+.2f}</small>")
                 + "</div>")
    parts.append('<div class="card"><h4>Biggest composite moves</h4>'
                 + chips(ch["biggest_moves"], lambda t, a, b, d: f"{esc(t)} <small>{d:+.2f}</small>")
                 + "</div>")
    parts.append('<div class="card"><h4>r/wsb mention jumps (vs prior 24h)</h4>'
                 + chips(ch["reddit_jumps"], lambda t, m, c: f"{esc(t)} <small>{m:,} ({c:+.0%})</small>")
                 + "</div></div>")
    return "".join(parts)


def heatmap_html(rows, labels, kind, vmax=None):
    head = "".join(f"<th>{esc(l)}</th>" for l in labels)
    out = [f'<div class="scroll"><table class="heat"><thead><tr><th>Ticker</th>{head}</tr></thead><tbody>']
    for t, vals in rows:
        cells = []
        for lab, v in zip(labels, vals):
            if v is None:
                cells.append('<td class="na">&middot;</td>')
            elif kind == "sent":
                shown = "0" if abs(v) < 0.05 else f"{v:+.1f}"
                cells.append(f'<td style="background:{sent_bg(v)}" title="{esc(t)} {esc(lab)}: {v:+.2f}">'
                             f'{shown}</td>')
            else:
                cells.append(f'<td style="background:{attn_bg(D.log_scale(v, vmax))}" '
                             f'title="{esc(t)} {esc(lab)}: {v:,}">{v:,}</td>')
        out.append(f'<tr><th><a href="#t-{esc(t)}">{esc(t)}</a></th>{"".join(cells)}</tr>')
    out.append("</tbody></table></div>")
    return "".join(out)


def flow_backtest_html(fbt):
    if not fbt:
        return ""
    out = ['<h3>Buying / selling pressure proxies vs later returns</h3>'
           '<p class="note">Each signal uses only data dated on or before its base date. CMF and up-volume share are '
           'treated as bullish/bearish calls (CMF beyond &plusmn;0.05; up-volume share beyond 45%/55%). '
           'This does not test sell <em>orders</em>, which are not public.</p>']
    for h, d in fbt.items():
        out.append(f'<h4>{h} trading day{"s" if h > 1 else ""} ahead</h4>'
                   '<table class="plain"><thead><tr><th>Predictor</th><th>Calls</th><th>Hit %</th><th>Hit % vs SPY</th>'
                   '<th>Avg return: bullish</th><th>Avg return: bearish</th><th>Corr</th></tr></thead><tbody>')
        for r in d["pressure"]:
            out.append("<tr>" + td(esc(r["name"]), cls="l") + td(str(r["calls"])) + td(fpct(r["hit"], 0, False))
                       + td(fpct(r["hit_ex"], 0, False)) + td(fpct(r["avg_bull"], 2), cls=sign_cls(r["avg_bull"]))
                       + td(fpct(r["avg_bear"], 2), cls=sign_cls(r["avg_bear"]))
                       + td("-" if r["corr"] is None else f'{r["corr"]:+.2f}') + "</tr>")
        out.append("</tbody></table>")
        out.append('<table class="plain"><thead><tr><th>FINRA short ratio bucket</th><th>N</th><th>% up</th>'
                   '<th>Avg return</th><th>vs SPY</th><th>Avg |return|</th></tr></thead><tbody>')
        for r in d["short"]:
            if not r["n"]:
                out.append("<tr>" + td(esc(r["name"]), cls="l") + td("0") + "</tr>")
                continue
            out.append("<tr>" + td(esc(r["name"]), cls="l") + td(str(r["n"])) + td(fpct(r["up"], 0, False))
                       + td(fpct(r["avg_ret"], 2), cls=sign_cls(r["avg_ret"]))
                       + td(fpct(r["avg_ex"], 2), cls=sign_cls(r["avg_ex"])) + td(fpct(r["avg_abs"], 2, False)) + "</tr>")
        out.append("</tbody></table>")
    out.append('<p class="note">Short-ratio buckets compare each ticker with its own recent average because the level '
               'mostly reflects market-maker activity. As elsewhere: compare with the &ldquo;all&rdquo; row, and treat '
               'small samples as noise.</p>')
    return "".join(out)


def backtest_html(bt, scorecards, attention, horizons, fbt=None):
    if not bt:
        return ('<p class="empty">No scored outcomes yet. A signal is scored once later closing prices exist '
                '(1 trading day after the run at the earliest), and the numbers only mean something after '
                'weeks of runs.</p>')
    out = []
    h0 = horizons[0]
    out.append(f'<h3>Source scorecard <small>({h0} trading day{"s" if h0 > 1 else ""} ahead, judged against SPY)</small></h3>')
    out.append('<table class="plain"><thead><tr><th>Source</th><th>Calls</th><th>Hit rate</th>'
               '<th>95% range</th><th></th><th>Last 10 days</th><th class="l">Verdict</th></tr></thead><tbody>')
    for s in scorecards:
        out.append("<tr>" + td(esc(s["name"]), cls="l") + td(str(s["calls"]))
                   + td(fpct(s["hit"], 0, False))
                   + td("-" if s["lo"] is None else f'{s["lo"]:.0%}&ndash;{s["hi"]:.0%}')
                   + td(ci_bar(s["hit"], s["lo"], s["hi"])) + td(fpct(s["recent"], 0, False))
                   + td(esc(s["verdict"]), cls="l") + "</tr>")
    out.append("</tbody></table>")
    out.append('<p class="note">A range that includes 50% means the result could be luck. '
               'Calls = bullish/bearish (non-neutral) composite or source calls with a resolved outcome.</p>')
    for h, d in bt.items():
        out.append(f'<h3>Backtest <small>{h} trading day{"s" if h > 1 else ""} ahead &middot; '
                   f'{d["n"]} observations &middot; {d["up_rate"]:.0%} of them rose</small></h3>')
        out.append('<table class="plain"><thead><tr><th>Predictor</th><th>Calls</th><th>Hit %</th>'
                   '<th>Hit % vs SPY</th><th>Avg return: bullish</th><th>Avg return: bearish</th>'
                   '<th>Corr</th></tr></thead><tbody>')
        for r in d["rows"]:
            out.append("<tr>" + td(esc(r["name"]), cls="l") + td(str(r["calls"]))
                       + td(fpct(r["hit"], 0, False)) + td(fpct(r["hit_ex"], 0, False))
                       + td(fpct(r["avg_bull"], 2), cls=sign_cls(r["avg_bull"]))
                       + td(fpct(r["avg_bear"], 2), cls=sign_cls(r["avg_bear"]))
                       + td("-" if r["corr"] is None else f'{r["corr"]:+.2f}') + "</tr>")
        out.append("</tbody></table>")
    out.append('<p class="note">Compare hit rates with the share that rose: in a rising market, '
               '&ldquo;always bullish&rdquo; also wins. A useful signal has bullish calls averaging above zero '
               'and bearish calls below zero. Under ~30 calls per row is noise.</p>')
    if attention:
        out.append('<h3>Reddit attention vs later moves <small>(change in r/wsb mentions vs prior 24h)</small></h3>')
        for h, rows in attention.items():
            out.append(f'<h4>{h} trading day{"s" if h > 1 else ""} ahead</h4>'
                       '<table class="plain"><thead><tr><th>Bucket</th><th>N</th><th>% up</th><th>Avg return</th>'
                       '<th>vs SPY</th><th>Avg |return|</th></tr></thead><tbody>')
            for r in rows:
                out.append("<tr>" + td(esc(r["name"]), cls="l") + td(str(r["n"]))
                           + td(fpct(r["up"], 0, False)) + td(fpct(r["avg_ret"], 2), cls=sign_cls(r["avg_ret"]))
                           + td(fpct(r["avg_ex"], 2), cls=sign_cls(r["avg_ex"]))
                           + td(fpct(r["avg_abs"], 2, False)) + "</tr>")
            out.append("</tbody></table>")
        out.append('<p class="note">Attention has no direction of its own: if &ldquo;surging&rdquo; shows a bigger '
                   '|return| than the &ldquo;all&rdquo; row, attention is flagging bigger moves, not necessarily up ones.</p>')
    out.append(flow_backtest_html(fbt))
    return "".join(out)


def detail_html(ticker, o, hist, labels, price, signals, horizons, flow_d=None, intraday=None):
    """The per-ticker overlay (shown via CSS :target when its link is clicked)."""
    kpis = [("Price", fnum(o["price"], ",.2f") + (f' <small>{esc(o["price_date"] or "")}</small>' if o["price"] else "")),
            ("Composite", f'<b class="{sign_cls(o["composite"])}">{fsent(o["composite"])}</b>'),
            ("Consensus", esc(D.short_consensus(o["consensus"]))),
            ("4chan mentions", fnum(o["c4_mentions"])),
            ("r/wsb mentions", fnum(o["rd_mentions"]) + (f' <small>{fpct(o["rd_mentions"] / o["rd_prev_mentions"] - 1, 0)}</small>'
                                                          if o["rd_mentions"] and o["rd_prev_mentions"] else "")),
            ("StockTwits (0-100)", fnum(o["st_sentiment"])),
            ("Heat", fnum(o["heat"], ".2f"))]
    fm = (flow_d or {}).get("metrics") or {}
    if fm:
        kpis += [("CMF 20d", f'<b class="{sign_cls(fm.get("cmf_20"))}">{fsent(fm.get("cmf_20"))}</b>'),
                 ("Up-volume 20d", fpct(fm.get("updown_20"), 0, False)),
                 ("Short ratio", fpct(fm.get("short_1d"), 0, False)
                  + (f' <small>{esc(fm["short_date"])}</small>' if fm.get("short_date") else ""))]
    out = [f'<section class="detail" id="t-{esc(ticker)}"><div class="detail-inner">'
           f'<a class="close" href="#leaderboard">&times; Close</a><h2>{esc(ticker)}</h2><div class="kpis">'
           + "".join(f'<div class="kpi"><span>{k}</span>{v}</div>' for k, v in kpis) + "</div><div class=\"grid2\">"]
    out.append(chart_card("Sentiment", [
        {"name": "Composite", "values": [h["composite"] for h in hist], "color": "var(--c1)"},
        {"name": "4chan NET", "values": [h["c4_net"] for h in hist], "color": "var(--c2)"},
        {"name": "StockTwits NET", "values": [h["st_net"] for h in hist], "color": "var(--c3)"}],
        labels, ylim=(-1.05, 1.05), zero_line=True, fmt=lambda v: f"{v:+.1f}",
        note="Per saved run day; gaps = not in that day's report."))
    if intraday:
        per_day = {}
        for i in intraday:
            per_day[i["run_date"]] = per_day.get(i["run_date"], 0) + 1
        if max(per_day.values()) >= 2:      # only worth a chart when some day has several runs
            il = [i["run_ts"][5:16].replace("T", " ") for i in intraday]
            out.append(chart_card("Intraday sentiment (every saved run, last 3 days)", [
                {"name": "Composite", "values": [i["composite"] for i in intraday], "color": "var(--c1)"},
                {"name": "4chan NET", "values": [i["c4_net"] for i in intraday], "color": "var(--c2)"},
                {"name": "StockTwits NET", "values": [i["st_net"] for i in intraday], "color": "var(--c3)"}],
                il, ylim=(-1.05, 1.05), zero_line=True, fmt=lambda v: f"{v:+.1f}",
                note="One point per saved run (times are UTC). The daily charts and the backtest use each day's average."))
    out.append(chart_card("r/wsb mentions", [{"name": "mentions", "values": [h["rd_mentions"] for h in hist]}],
                          labels, fmt=lambda v: f"{v:,.0f}", integer=True))
    out.append(chart_card("4chan mentions", [{"name": "mentions", "values": [h["c4_mentions"] for h in hist]}],
                          labels, fmt=lambda v: f"{v:,.0f}", integer=True))
    if price:
        pl = [mmdd(d) for d in price["dates"]]
        out.append(chart_card("Price and moving averages", [
            {"name": "Close", "values": price["close"], "color": "var(--fg)"},
            {"name": "SMA 15", "values": price["sma15"], "color": "var(--c1)"},
            {"name": "SMA 30", "values": price["sma30"], "color": "var(--c2)"},
            {"name": "SMA 60", "values": price["sma60"], "color": "var(--c3)"}], pl,
            fmt=lambda v: f"{v:,.2f}", note="Adjusted daily closes; averages are trading-day SMAs."))
        out.append(chart_card("On-balance volume", [{"name": "OBV", "values": price["obv"], "color": "var(--c4)"}], pl,
                              fmt=lambda v: "0" if v == 0 else f"{v / 1e6:.1f}M" if abs(v) >= 1e6 else f"{v / 1e3:.0f}k",
                              note="Starts at 0 at the left edge: read the direction, not the level."))
        if flow_d:
            rl = [mmdd(d) for d in flow_d["rolling"]["dates"]]
            out.append(chart_card("Chaikin Money Flow (20d)", [
                {"name": "CMF", "values": flow_d["rolling"]["cmf"], "color": "var(--c1)"}], rl,
                zero_line=True, fmt=lambda v: f"{v:+.2f}",
                note="Above 0: closes tend to land in the upper part of the day's range on volume (buying "
                     "pressure). Below 0: the reverse."))
            out.append(chart_card("Up-volume share (20d)", [
                {"name": "share of volume on up days", "values": flow_d["rolling"]["updown"], "color": "var(--c2)"}],
                rl, ref=0.5, fmt=lambda v: f"{v:.0%}",
                note="Volume on up days / (up + down days). 50% = balanced. A crude buy-vs-sell volume proxy."))
    else:
        out.append('<figure class="card"><figcaption><b>Price</b></figcaption>'
                   '<div class="empty">No price history saved for this ticker.</div></figure>')
    if flow_d and flow_d["short"]["dates"]:
        sl = [mmdd(d) for d in flow_d["short"]["dates"]]
        out.append(chart_card("FINRA short-sale volume ratio", [
            {"name": "daily", "values": flow_d["short"]["daily"], "color": "var(--c3)"},
            {"name": "5-day avg", "values": flow_d["short"]["avg5"], "color": "var(--fg)", "dash": "4 3"}], sl,
            fmt=lambda v: f"{v:.0%}",
            note="Short-sale volume / total volume reported to FINRA (mostly off-exchange). Includes market-maker "
                 "hedging, so a high ratio is not the same as bearish bets: watch the change against its own average."))
    out.append("</div>")
    if signals:
        hs = horizons or [1]
        out.append('<h3>Recent calls and what happened</h3><table class="plain"><thead><tr><th>Run day</th>'
                   '<th>Runs</th><th>Avg composite</th><th class="l">Consensus</th>' + "".join(f"<th>{h}d return</th>" for h in hs)
                   + "<th>Result (first horizon)</th></tr></thead><tbody>")
        for s in signals:
            d = D._direction(s["composite"], D.DEAD_ZONE)
            r0 = s["ret"].get(hs[0])
            res = "-" if r0 is None else ("neutral call" if d == 0 else "matched" if (d > 0) == (r0 > 0) else "missed")
            out.append("<tr>" + td(esc(s["date"]), cls="l") + td(str(s["n_runs"])) + td(fsent(s["composite"]))
                       + td(esc(D.short_consensus(s["consensus"])), cls="l")
                       + "".join(td(fpct(s["ret"].get(h), 2), cls=sign_cls(s["ret"].get(h))) for h in hs)
                       + td(res) + "</tr>")
        out.append("</tbody></table>")
    out.append("</div></section>")
    return "".join(out)


# ----------------------------------------------------------------------------
# page shell
# ----------------------------------------------------------------------------
CSS = """
:root{--bg:#fff;--fg:#1b1f24;--muted:#667085;--line:#e4e7ec;--card:#f7f8fa;--pos:#067647;--neg:#b42318;
--accent:#2563eb;--c1:#2563eb;--c2:#ea580c;--c3:#7c3aed;--c4:#059669}
@media (prefers-color-scheme:dark){:root{--bg:#0f1419;--fg:#e6e8eb;--muted:#9aa4b2;--line:#2a3340;--card:#161c24;
--pos:#4ade80;--neg:#f87171;--accent:#60a5fa;--c1:#60a5fa;--c2:#fb923c;--c3:#a78bfa;--c4:#34d399}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:16px 20px 60px}
header.top{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between;
border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:8px}
h1{font-size:20px;margin:0}h2{font-size:18px;margin:26px 0 6px}h3{font-size:15px;margin:20px 0 6px}h4{margin:14px 0 6px;font-size:13px}.card h4{margin:0 0 6px}
small,.note,.na{color:var(--muted)}.note{font-size:12px;margin:4px 0 10px}
nav a{margin-right:14px;color:var(--accent);text-decoration:none}a{color:var(--accent)}
.banner{background:#b42318;color:#fff;padding:8px 12px;border-radius:6px;margin:10px 0;font-weight:600}
.meta{color:var(--muted);font-size:12px}
.pick{margin:8px 0 12px}select{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 8px}
table{border-collapse:collapse;width:100%}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child,td.l,.plain th:first-child{text-align:left}
thead th{position:sticky;top:0;background:var(--bg);font-size:12px;color:var(--muted);cursor:pointer;user-select:none}
thead th.nosort{cursor:default}th[data-dir=asc]::after{content:" \\25B2"}th[data-dir=desc]::after{content:" \\25BC"}
tbody tr:hover{background:var(--card)}.pos{color:var(--pos)}.neg{color:var(--neg)}
.chip{font-variant-numeric:tabular-nums}td.cons{text-align:left;color:var(--muted)}
.scroll{overflow-x:auto}.heat td{text-align:center;font-size:11px;color:#111;min-width:34px;padding:3px 4px}
.heat th{font-size:11px;font-weight:600}.heat td.na{color:var(--muted);background:transparent}
.plain th{cursor:default;position:static}.plain td{text-align:right}
.plain td:first-child,.plain td.l,.plain th.l{text-align:left}td:first-child a{text-decoration:none}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:0}
figcaption{display:flex;flex-wrap:wrap;gap:10px;align-items:baseline;margin-bottom:4px}
.lg{font-size:11px;color:var(--muted)}.lg i{display:inline-block;width:14px;height:3px;margin-right:4px;vertical-align:middle}
svg.chart{width:100%;height:auto}.axis line{stroke:var(--line);stroke-width:1}
.axis text{fill:var(--muted);font-size:12px}line.zero{stroke:var(--muted);stroke-dasharray:3 3}
.spark{vertical-align:middle}.cibar .track{stroke:var(--line);stroke-width:2}.cibar .mid{stroke:var(--muted)}
.cibar .range{fill:var(--accent);opacity:.35}.cibar .dot{fill:var(--accent)}
.tag{display:inline-block;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:2px 9px;
margin:2px 4px 2px 0;text-decoration:none}.empty{color:var(--muted);padding:14px 4px}
.warnbox{background:var(--card);border:1px solid var(--c2);border-left:4px solid var(--c2);border-radius:6px;
padding:8px 12px;margin:8px 0;font-size:13px}
.detail{display:none;position:fixed;inset:0;overflow:auto;background:var(--bg);z-index:20}
.detail:target{display:block}.detail-inner{max-width:1280px;margin:0 auto;padding:18px 20px 60px;position:relative}
.close{position:absolute;right:20px;top:18px;text-decoration:none;border:1px solid var(--line);border-radius:6px;padding:4px 10px}
.kpis{display:flex;flex-wrap:wrap;gap:10px;margin:8px 0 14px}.kpi{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:6px 12px}.kpi span{display:block;font-size:11px;color:var(--muted)}
details>summary{cursor:pointer;margin:10px 0;font-weight:600}
footer{margin-top:40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:10px}
"""

JS = """
(function(){
  document.querySelectorAll('table.sortable').forEach(function(tbl){
    var heads = tbl.tHead.rows[0].cells;
    Array.prototype.forEach.call(heads, function(th, idx){
      if (th.className.indexOf('nosort') >= 0) return;
      th.addEventListener('click', function(){
        var tb = tbl.tBodies[0], rows = Array.prototype.slice.call(tb.rows);
        var dir = th.getAttribute('data-dir') === 'desc' ? 'asc' : 'desc';
        Array.prototype.forEach.call(heads, function(h){ h.removeAttribute('data-dir'); });
        th.setAttribute('data-dir', dir);
        function val(r){
          var c = r.cells[idx], v = c.getAttribute('data-v');
          if (v === null) v = c.textContent.trim();
          var n = parseFloat(v);
          return (v !== '' && !isNaN(n) && /^[-+]?[0-9.]/.test(v)) ? n : String(v).toLowerCase();
        }
        rows.sort(function(a, b){
          var x = val(a), y = val(b), xe = (x === '' || x === '-'), ye = (y === '' || y === '-');
          if (xe && !ye) return 1; if (!xe && ye) return -1;
          var c = (typeof x === 'number' && typeof y === 'number') ? x - y : String(x).localeCompare(String(y));
          return dir === 'asc' ? c : -c;
        });
        rows.forEach(function(r){ tb.appendChild(r); });
      });
    });
  });
  var pick = document.getElementById('pick');
  function sync(){ if (pick && location.hash.indexOf('#t-') === 0) pick.value = decodeURIComponent(location.hash.slice(3)); }
  if (pick) {
    pick.addEventListener('change', function(){ if (pick.value) location.hash = '#t-' + pick.value; });
    window.addEventListener('hashchange', sync); sync();
  }
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape' && location.hash.indexOf('#t-') === 0) location.hash = '#leaderboard';
  });
})();
"""


def page(title, body):
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
            f'<style>{CSS}</style></head><body>{body}<script>{JS}</script></body></html>')


def build(db_path="history.db", out_path="dashboard.html", top=40, max_tickers=60,
          history_days=30, price_days=90, heat_tickers=25):
    conn = D.connect(db_path)
    try:
        runs_all = D.run_days(conn)
        if not runs_all:
            html_text = page("Ticker dashboard", '<main><h1>Ticker dashboard</h1><p class="empty">'
                             "No runs saved yet. Run combined_report.py first.</p></main>")
            open(out_path, "w", encoding="utf-8").write(html_text)
            return out_path
        runs = runs_all[-history_days:]
        daily = D.daily_all(conn)                 # every figure per day = the average of that day's runs
        obs = D.observations_by_run(conn, runs, daily)
        latest = runs[-1]
        latest_obs = obs[latest["run_id"]]
        prev = runs[-2] if len(runs) >= 2 else None
        prev_obs = obs[prev["run_id"]] if prev else None
        window = [obs[r["run_id"]] for r in runs][-14:]
        board = D.leaderboard(latest_obs, window)
        flow_map = D.flow_latest(conn, [r["ticker"] for r in board[:top]])
        pstat = D.price_status(conn, latest["run_id"])
        for r in board[:top]:
            r["flow"] = flow_map.get(r["ticker"], {})
            r["price_status"] = pstat.get(r["ticker"])
        summary = D.database_summary(conn, runs_all)
        horizons = D.horizons_available(conn)
        bt = D.backtest(conn, horizons[:3])
        scorecards = D.scorecard(conn, horizons[0]) if horizons else []
        attention = D.attention_study(conn, sorted({horizons[0], horizons[-1]})) if horizons else {}
        flow_bt = D.flow_backtest(conn, sorted({horizons[0], horizons[-1]})) if horizons else {}
        labels = [mmdd(r["run_date"]) for r in runs]

        tickers = [r["ticker"] for r in board][:max_tickers]
        details = []
        for t in tickers:
            details.append(detail_html(
                t, latest_obs[t], D.ticker_history(runs, obs, t), labels,
                D.price_history(conn, t, price_days), D.ticker_signals(conn, t, daily=daily), horizons[:3],
                D.flow_charts(conn, t, price_days), D.ticker_intraday(conn, t, 3)))

        hm_t = tickers[:heat_tickers]
        sent_rows = D.heatmap(runs, obs, hm_t, "composite")
        rd_rows = D.heatmap(runs, obs, hm_t, "rd_mentions")
        c4_rows = D.heatmap(runs, obs, hm_t, "c4_mentions")
        rd_max = max([v for _, vs in rd_rows for v in vs if v] or [1])
        c4_max = max([v for _, vs in c4_rows for v in vs if v] or [1])

        details_html = "".join(details)
        banner = ('<div class="banner">SYNTHETIC DEMO DATA &mdash; not real market or forum data</div>'
                  if D.is_demo(runs_all) else "")
        opts = '<option value="">Pick a ticker&hellip;</option>' + "".join(
            f'<option value="{esc(t)}">{esc(t)}</option>' for t in tickers)
        body = f"""<main id="top">
<header class="top"><h1>Ticker attention &amp; sentiment</h1>
<nav><a href="#leaderboard">Leaderboard</a><a href="#changes">What changed</a>
<a href="#heatmaps">Heatmaps</a><a href="#backtest">Backtest</a></nav></header>
{banner}
<p class="meta">Latest day <b>{esc(latest["run_date"])}</b> ({latest["n_runs"]} run(s) that day; last saved {esc(latest["run_ts"])} UTC) &middot;
sources: {esc(latest["sources"] or "-")} &middot; {summary["run_days"]} day(s) / {summary["total_runs"]} run(s) saved
({esc(summary["first_date"])} to {esc(summary["last_date"])}) &middot; {summary["priced_symbols"]} priced symbols &middot;
{summary["scored_outcomes"]} scored outcomes</p>
<h2 id="leaderboard">Leaderboard <small>latest day, averaged over its runs &middot; click a column to sort, a ticker to open it</small></h2>
<div class="pick"><label>Jump to ticker: <select id="pick">{opts}</select></label></div>
{price_banner(board, top)}
<div class="scroll">{leaderboard_html(board, top)}</div>
<p class="note">Heat = weighted rank score. 4ch / r/wsb = mentions. 4ch NET = bullish minus bearish share of 4chan posts.
STW = StockTwits' own 0-100 sentiment score. Composite = weighted average of the sources that have sentiment (r/wsb has none).
vs SMA = price relative to its 15/30/60 trading-day average.
CMF / Up-vol = Chaikin Money Flow and share of volume on up days (20 days): buying vs selling <em>pressure</em> proxies, not order counts.
Short = FINRA short-sale volume ratio (latest day; and its 5-day average minus its own 20-day average).</p>
<h2 id="changes">What changed since the previous run day</h2>
{changes_html(D.changes(latest_obs, prev_obs), latest["run_date"], prev["run_date"] if prev else "")}
<h2 id="heatmaps">Heatmaps <small>top {len(hm_t)} tickers &middot; last {len(runs)} run day(s)</small></h2>
<h3>Composite sentiment</h3>{heatmap_html(sent_rows, labels, "sent")}
<h3>r/wsb mentions</h3>{heatmap_html(rd_rows, labels, "attn", rd_max)}
<details><summary>4chan mentions</summary>{heatmap_html(c4_rows, labels, "attn", c4_max)}</details>
<h2 id="backtest">Does sentiment line up with later returns?</h2>
<p class="note">Each day's sentiment is the average of that day's runs. Returns are measured from that day's market close
(a weekend or holiday run uses the last close before it).</p>
{backtest_html(bt, scorecards, attention, horizons, flow_bt)}
<footer>Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} from {esc(db_path)}. Data: 4chan /biz/ /smg/, ApeWisdom
(r/wallstreetbets mention counts), StockTwits, and a price provider. Forum sentiment is a noisy, often contrarian
signal and these statistics are descriptive, not predictive. Not financial advice.</footer>
</main>{details_html}"""
        html_text = page("Ticker attention & sentiment", body)
    finally:
        conn.close()
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="Build a static HTML dashboard from history.db")
    ap.add_argument("--db", default="history.db")
    ap.add_argument("--out", default="dashboard.html")
    ap.add_argument("--top", type=int, default=40, help="leaderboard rows")
    ap.add_argument("--max-tickers", type=int, default=60, help="tickers that get a detail view")
    ap.add_argument("--history-days", type=int, default=30, help="run days shown in trends/heatmaps")
    ap.add_argument("--price-days", type=int, default=90, help="trading days in the price chart")
    ap.add_argument("--heat-tickers", type=int, default=25, help="rows in the heatmaps")
    args = ap.parse_args()
    try:
        out = build(args.db, args.out, args.top, args.max_tickers, args.history_days,
                    args.price_days, args.heat_tickers)
    except FileNotFoundError as e:
        sys.exit(str(e))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
