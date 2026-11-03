"""Daily liquid-contract selection, without scheduling or database state."""

import logging
import time
from decimal import Decimal as D

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
    return score


def candidates(client):
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
            ranked.append((_score(client, symbol, now), symbol))
        except Exception as exc:
            log.warning("%s: trend unavailable (%s)", symbol, type(exc).__name__)
    return [symbol for _, symbol in sorted(ranked, key=lambda row: (-row[0], row[1]))[:CANDIDATE_LIMIT]]
