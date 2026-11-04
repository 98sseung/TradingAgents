"""Daily liquid-contract selection, without scheduling or database state."""

import logging
import time
from datetime import UTC, datetime
from decimal import Decimal as D

import yfinance as yf

log = logging.getLogger(__name__)
MIN_AGE, VOLUME_LIMIT, CANDIDATE_LIMIT = 90 * 86400, 20, 5
FAST_EMA, SLOW_EMA, ATR_PERIOD = 20, 50, 14


def _score(client, symbol, now):
    bars = [b for b in client.klines(symbol, "1d", 100) if b[6] < now * 1000]
    if len(bars) < SLOW_EMA:
        raise ValueError("Insufficient completed daily candles")
    closes = [D(b[4]) for b in bars]
    emas = []
    for period in (FAST_EMA, SLOW_EMA):
        ema, alpha = sum(closes[:period]) / period, D(2) / (period + 1)
        for close in closes[period:]:
            ema += alpha * (close - ema)
        emas.append(ema)
    ranges = [max(D(b[2]) - D(b[3]), abs(D(b[2]) - D(a[4])), abs(D(b[3]) - D(a[4])))
              for a, b in zip(bars, bars[1:], strict=False)]
    atr = sum(ranges[:ATR_PERIOD]) / ATR_PERIOD
    for true_range in ranges[ATR_PERIOD:]:
        atr = (atr * (ATR_PERIOD - 1) + true_range) / ATR_PERIOD
    if not atr.is_finite() or atr <= 0:
        raise ValueError("Invalid daily ATR")
    score = abs(emas[0] - emas[1]) / atr
    if not score.is_finite():
        raise ValueError("Invalid EMA spread")
    return score, closes[-1], bars[-1][6], 1 if emas[0] >= emas[1] else -1, atr


def _yahoo_matches(symbol, close, closed_ms):
    try:
        history = yf.Ticker(symbol.removesuffix("USDT") + "-USD").history(
            period="5d", interval="1d", raise_errors=True)
        # Same UTC day as the completed Binance bar: Yahoo's last row is today's unfinished candle.
        day = datetime.fromtimestamp(closed_ms / 1000, UTC).date()
        same_day = [v for ts, v in history["Close"].dropna().items() if ts.date() == day]
        if not same_day:
            raise ValueError(f"No Yahoo daily close for {day}")
        yahoo_close = D(str(same_day[0]))
        if not yahoo_close.is_finite() or close <= 0 or abs(yahoo_close - close) > close * D("0.05"):
            raise ValueError(f"Yahoo close {yahoo_close} differs from Binance close {close}")
        return True
    except Exception as exc:
        log.warning("%s: Yahoo identity unavailable (%s: %s)", symbol, type(exc).__name__, exc)
        return False


def candidates(client):
    """Return top picks with frozen daily inputs and the full eligible shock universe."""
    now = time.time()
    volumes = {}
    for ticker in client.tickers():
        try:
            volume = D(ticker["quoteVolume"])
            if not volume.is_finite() or volume < 0:
                raise ValueError("Invalid volume")
            volumes[ticker["symbol"]] = volume
        except Exception as exc:
            log.warning("%s: volume unavailable (%s)", ticker.get("symbol", "unknown"), type(exc).__name__)
    tradable = {s["symbol"] for s in client.symbols() if s.get("status") == "TRADING"}
    universe = [s for s in client.symbols(live=True) if s.get("marginAsset") == "USDT"
                and s.get("contractType") == "PERPETUAL" and s.get("symbol") in tradable
                and s.get("underlyingType") == "COIN" and s.get("symbol") in volumes]
    liquid = sorted(universe, key=lambda s: (-volumes.get(s["symbol"], D(0)), s["symbol"]))[:VOLUME_LIMIT]
    ranked = []
    for item in liquid:
        symbol = item["symbol"]
        try:
            if "Meme" in item.get("underlyingSubType", []) or now * 1000 - item["onboardDate"] < MIN_AGE * 1000:
                continue
            ranked.append((*_score(client, symbol, now), symbol))
        except Exception as exc:
            log.warning("%s: trend unavailable (%s)", symbol, type(exc).__name__)
    selected = {}
    for _, close, closed, direction, atr, symbol in sorted(ranked, key=lambda row: (-row[0], row[-1])):
        if _yahoo_matches(symbol, close, closed):
            selected[symbol] = {"direction": direction, "atr": float(atr), "price": float(close)}
            if len(selected) == CANDIDATE_LIMIT:
                break
    return selected, [row[-1] for row in ranked]
