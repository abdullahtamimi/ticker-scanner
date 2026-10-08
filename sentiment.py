"""
sentiment.py - VADER sentiment scoring tuned for WSB / biz slang.

pip install vaderSentiment

Scores are VADER "compound" values in [-1, +1]:
    >= +0.05 bullish,  <= -0.05 bearish,  otherwise neutral.
"""
import re

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Word-level valences (VADER scale is roughly -4..+4). Tune to taste.
WSB_LEXICON = {
    # bullish
    "bullish": 2.5, "calls": 1.2, "moon": 2.5, "mooning": 2.5, "tendies": 2.5,
    "squeeze": 1.5, "breakout": 2.0, "undervalued": 2.0, "btfd": 1.5,
    "hodl": 1.0, "wagmi": 2.5, "pump": 1.5, "pumping": 1.8, "printing": 2.0,
    "brrr": 1.5, "rally": 2.0, "ripping": 2.0, "diamondhands": 2.0,
    "rocket": 2.0, "bullrun": 2.5, "lfg": 2.0, "green": 1.0, "load": 0.8,
    "loading": 0.8, "cheap": 1.0, "yolo": 0.5, "generational": 1.5,
    # bearish
    "bearish": -2.5, "puts": -1.2, "crash": -2.5, "crashing": -2.5, "dump": -2.5,
    "dumping": -2.5, "rug": -3.0, "rugpull": -3.0, "bagholder": -2.5,
    "bagholding": -2.5, "bagholders": -2.5, "rekt": -3.0, "guh": -3.0,
    "drill": -2.0, "drilling": -2.0, "tank": -2.0, "tanking": -2.2, "cooked": -2.0,
    "rip": -2.0, "overvalued": -2.0, "ngmi": -2.5, "bubble": -1.5,
    "bagholds": -2.0, "red": -1.0, "wiped": -2.5, "margin_call": -3.0,
    "clown": -1.5, "scam": -3.0, "fraud": -3.0, "overpriced": -1.8,
    "worthless": -3.0, "bankrupt": -3.0, "bankruptcy": -3.0, "sell-off": -2.0,
    "selloff": -2.0, "downgrade": -1.8, "dead": -2.0,
}

# VADER swaps emoji for text descriptions before scoring, which would bypass
# custom emoji valences - so we map emoji to lexicon words first.
EMOJI_TO_WORD = {
    "🚀": "moon", "🌙": "moon", "💎": "diamondhands", "🙌": "diamondhands",
    "📈": "bullish", "📉": "bearish", "🐂": "bullish", "🐻": "bearish",
    "🤡": "clown", "💀": "rekt", "🩸": "bleeding", "🔥": "ripping",
    "🟢": "green", "🔴": "red", "🍗": "tendies",
}

_analyzer = SentimentIntensityAnalyzer()
_analyzer.lexicon.update(WSB_LEXICON)
_analyzer.lexicon["bleeding"] = -2.0


def score(text, mask=()):
    """Compound sentiment of `text`. Tickers in `mask` are removed first so a
    ticker that's also an English word (LOVE, WIN, BEST) doesn't skew the score."""
    for emoji, word in EMOJI_TO_WORD.items():
        text = text.replace(emoji, f" {word} ")
    for sym in mask:
        text = re.sub(rf"\$?\b{re.escape(sym)}\b", " ", text)
    return _analyzer.polarity_scores(text)["compound"]


def label(compound, bull=0.15, bear=-0.15):
    """Label for an *average* score across many posts."""
    if compound >= bull:
        return "Bullish"
    if compound <= bear:
        return "Bearish"
    return "Mixed"
