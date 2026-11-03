"""Synchronous Binance USDⓈ-M client. Run this module for a read-only account check."""

import hashlib
import hmac
import os
import time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import dotenv_values

_EXCHANGE_INFO = {}
_BASE_URLS = {"demo": "https://demo-fapi.binance.com", "live": "https://fapi.binance.com"}


class BinanceError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg
        super().__init__(f"Binance {code}: {msg}")


def round_price(price, tick_size) -> Decimal:
    """Round to the nearest tick, with ties rounded up."""
    price, tick = Decimal(str(price)), Decimal(str(tick_size))
    if not price.is_finite() or price <= 0 or not tick.is_finite() or tick <= 0:
        raise ValueError("price and tick size must be positive and finite")
    result = (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    if result <= 0:
        raise ValueError("rounded price must be positive")
    return result


def round_quantity(quantity, price, filters) -> Decimal:
    """Round down, then check minimum quantity and notional at the supplied price."""
    quantity, price = Decimal(str(quantity)), Decimal(str(price))
    if not quantity.is_finite() or quantity <= 0 or not price.is_finite() or price <= 0:
        raise ValueError("quantity and price must be positive and finite")
    step = filters["step_size"]
    quantity = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
    if quantity <= 0 or quantity < filters["min_qty"]:
        raise ValueError("rounded quantity is below min qty")
    if quantity * price < filters["min_notional"]:
        raise ValueError("rounded order is below min notional")
    return quantity


def _retry_delay(value, attempt):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        return 0.25 * 2 ** attempt


class BinanceClient:
    def __init__(self):
        self.mode = os.environ.get("TRADING_BOT_MODE", "demo")
        if self.mode not in _BASE_URLS:
            raise ValueError("TRADING_BOT_MODE must be demo or live")
        self.base_url = _BASE_URLS[self.mode]
        path = Path(os.environ.get("TRADING_BOT_SECRETS") or
                    Path.home() / ".config/trading-bot/secrets.env").expanduser()
        try:
            with path.open() as secrets:
                if os.fstat(secrets.fileno()).st_mode & 0o077:
                    raise ValueError("Secrets file permissions are too open; use chmod 600")
                values = dotenv_values(stream=secrets, interpolate=False)
        except OSError:
            raise ValueError("Cannot read trading bot secrets file") from None
        prefix = "BINANCE_TESTNET" if self.mode == "demo" else "BINANCE_LIVE"
        self._key, self._secret = values.get(f"{prefix}_API_KEY"), values.get(f"{prefix}_API_SECRET")
        if not self._key or not self._secret:
            raise ValueError(f"Secrets file must contain {prefix}_API_KEY and {prefix}_API_SECRET")
        self._offset = None
        self._session = requests.Session()

    def _sync_time(self):
        before = time.time() * 1000
        server = self._request("GET", "/fapi/v1/time")["serverTime"]
        self._offset = server - (before + time.time() * 1000) / 2

    def _request(self, method, path, params=None, *, signed=False, base_url=None):
        if signed and self._offset is None:
            self._sync_time()
        attempt, resynced = 0, False
        while True:
            data = dict(params or {})
            headers, signature = {}, ""
            if signed:
                data.update(timestamp=int(time.time() * 1000 + self._offset), recvWindow=5000)
                signature = hmac.new(self._secret.encode(), urlencode(data).encode(),
                                     hashlib.sha256).hexdigest()
                data["signature"] = signature
                headers["X-MBX-APIKEY"] = self._key
            try:
                response = self._session.request(method, (base_url or self.base_url) + path, params=urlencode(data),
                                                 headers=headers, timeout=10, allow_redirects=False)
            except requests.RequestException:
                if method == "GET" and attempt < 3:
                    time.sleep(_retry_delay(None, attempt))
                    attempt += 1
                    continue
                # Requests exceptions may contain the signed URL; never propagate them.
                raise BinanceError(None, "Transport failure; request outcome may be unknown") from None
            if method == "GET" and (response.status_code == 429 or response.status_code >= 500) and attempt < 3:
                time.sleep(_retry_delay(response.headers.get("Retry-After"), attempt))
                attempt += 1
                continue
            try:
                payload = response.json()
            except ValueError:
                error = BinanceError(None, "Invalid JSON response; request outcome may be unknown")
                error.status = response.status_code
                raise error from None
            code = payload.get("code", 0) if isinstance(payload, dict) else 0
            if signed and code == -1021 and not resynced:
                self._sync_time()
                resynced = True
                continue
            if response.status_code >= 300 or code < 0:
                msg = str(payload.get("msg", "HTTP request failed")) if isinstance(payload, dict) else "HTTP request failed"
                for sensitive in (signature, self._key, self._secret):
                    if sensitive:
                        msg = msg.replace(sensitive, "[redacted]")
                error = BinanceError(code or response.status_code, msg)
                error.status = response.status_code
                raise error
            return payload

    def filters(self, symbol):
        cached_at, info = _EXCHANGE_INFO.get(self.base_url, (0, None))
        if info is None or time.monotonic() - cached_at >= 3600:
            info = self._request("GET", "/fapi/v1/exchangeInfo")
            _EXCHANGE_INFO[self.base_url] = time.monotonic(), info
        for item in info["symbols"]:
            if item["symbol"] == symbol:
                filters = {f["filterType"]: f for f in item["filters"]}
                lot = filters["LOT_SIZE"]
                return {"tick_size": Decimal(filters["PRICE_FILTER"]["tickSize"]),
                        "step_size": Decimal(lot["stepSize"]), "min_qty": Decimal(lot["minQty"]),
                        "min_notional": Decimal(filters["MIN_NOTIONAL"]["notional"])}
        raise ValueError("Unknown Binance symbol")

    def symbols(self, *, live=False):
        """Return venue symbols or live public metadata from the one-hour cache."""
        base_url = _BASE_URLS["live"] if live else self.base_url
        cached_at, info = _EXCHANGE_INFO.get(base_url, (0, None))
        if info is None or time.monotonic() - cached_at >= 3600:
            info = self._request("GET", "/fapi/v1/exchangeInfo", base_url=base_url)
            _EXCHANGE_INFO[base_url] = time.monotonic(), info
        return info["symbols"]

    def tickers(self):
        # Demo volumes are synthetic; rank liquidity using live public data.
        return self._request("GET", "/fapi/v1/ticker/24hr", base_url=_BASE_URLS["live"])

    def klines(self, symbol, interval="1h", limit=500):
        return self._request("GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    def mark_price(self, symbol):
        """Return Binance's mark-price record, including lastFundingRate."""
        return self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})

    def balance(self):
        for asset in self._request("GET", "/fapi/v3/balance", signed=True):
            if asset["asset"] == "USDT":
                return {"wallet": Decimal(asset["balance"]), "available": Decimal(asset["availableBalance"])}
        raise BinanceError(None, "USDT balance missing from account response")

    def positions(self):
        return [p for p in self._request("GET", "/fapi/v3/positionRisk", signed=True)
                if Decimal(p["positionAmt"]) != 0]

    def ensure_setup(self, symbol, leverage):
        for path, params, unchanged in [
            ("/fapi/v1/positionSide/dual", {"dualSidePosition": "false"}, -4059),
            ("/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, -4046),
            ("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, None),
        ]:
            try:
                self._request("POST", path, params, signed=True)
            except BinanceError as exc:
                if unchanged is None or exc.code != unchanged:
                    raise

    def order_by_client_id(self, symbol, client_id, *, algo=False):
        path = "/fapi/v1/algoOrder" if algo else "/fapi/v1/order"
        params = {"clientAlgoId": client_id} if algo else {"symbol": symbol, "origClientOrderId": client_id}
        return self._request("GET", path, params, signed=True)

    def _post_order(self, symbol, client_id, params, *, algo=False):
        if not client_id:
            raise ValueError("A client order id is required")
        path = "/fapi/v1/algoOrder" if algo else "/fapi/v1/order"
        params["clientAlgoId" if algo else "newClientOrderId"] = client_id
        try:
            return self._request("POST", path, params, signed=True)
        except BinanceError as exc:
            if exc.code not in (None, -1006, -1007) and getattr(exc, "status", 0) < 500:
                raise
            for attempt in range(5):
                try:
                    return self.order_by_client_id(symbol, client_id, algo=algo)
                except BinanceError as lookup_error:
                    if lookup_error.code != -2013:
                        break
                    # Accepted orders can take a couple of seconds to become queryable.
                    if attempt < 4:
                        time.sleep(1)
            raise BinanceError(None, "Order outcome unknown; reconcile by client id before resubmitting") from None

    def market_order(self, symbol, side, quantity, client_id, *, reduce_only=False):
        """Round and validate at the current mark price before submitting."""
        filters = self.filters(symbol)
        if reduce_only:
            filters = {**filters, "min_notional": Decimal(0)}
        quantity = round_quantity(quantity, self.mark_price(symbol)["markPrice"], filters)
        return self._post_order(symbol, client_id, {"symbol": symbol, "side": side, "type": "MARKET",
                                "quantity": format(quantity, "f"), "reduceOnly": str(reduce_only).lower()})

    def protective_order(self, symbol, side, trigger_price, client_id, *, take_profit=False):
        """Place a close-all stop or take-profit; side is the closing side."""
        price = round_price(trigger_price, self.filters(symbol)["tick_size"])
        return self._post_order(symbol, client_id, {"symbol": symbol, "side": side,
                                "algoType": "CONDITIONAL", "type": "TAKE_PROFIT_MARKET" if take_profit else "STOP_MARKET",
                                "triggerPrice": format(price, "f"), "closePosition": "true", "workingType": "MARK_PRICE"}, algo=True)

    def open_orders(self, symbol):
        return (self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True) +
                self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True))

    def cancel_all_orders(self, symbol):
        results, error = [], None
        for path in ("/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"):
            try:
                results.append(self._request("DELETE", path, {"symbol": symbol}, signed=True))
            except BinanceError as exc:
                error = error or exc
        if error:
            raise error
        return results

    def income_history(self, since):
        """Return trading income since an epoch millisecond timestamp (Binance retains 3 months)."""
        rows, start, end = {}, since, int(time.time() * 1000)
        while start <= end:
            batch = self._request("GET", "/fapi/v1/income", {"startTime": start, "endTime": end, "limit": 1000}, signed=True)
            for row in batch:
                if row["incomeType"] in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                    rows[row["incomeType"], row["tranId"]] = row
            if len(batch) < 1000:
                return sorted(rows.values(), key=lambda row: row["time"])
            # Re-read the boundary millisecond so simultaneous income records are not skipped.
            last = batch[-1]["time"]
            if last <= start:
                raise BinanceError(None, "Income history exceeds 1000 records in one millisecond")
            start = last
        return []


def main():
    client = BinanceClient()
    print(f"Mode: {client.mode}\nBase URL: {client.base_url}")
    print("USDT:", client.balance())
    print("Open positions:", client.positions())


if __name__ == "__main__":
    main()
