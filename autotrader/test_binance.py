"""Binance requests are mocked; these tests never contact an exchange."""

import hashlib
import hmac
import json
import traceback
from decimal import Decimal
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from autotrader import binance

EXCHANGE_INFO = {"symbols": [{"symbol": "BTCUSDT", "filters": [
    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
    {"filterType": "MIN_NOTIONAL", "notional": "50"},
]}]}


def response(payload, status=200, headers=None):
    result = requests.Response()
    result.status_code, result._content = status, json.dumps(payload).encode()
    result.headers.update(headers or {})
    return result


def params(call):
    return {key: values[0] for key, values in parse_qs(call.kwargs["params"]).items()}


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text("BINANCE_TESTNET_API_KEY=demo-key\nBINANCE_TESTNET_API_SECRET=demo-secret\n")
    path.chmod(0o600)
    monkeypatch.setenv("TRADING_BOT_SECRETS", str(path))
    monkeypatch.delenv("TRADING_BOT_MODE", raising=False)
    return path


@pytest.fixture
def http(monkeypatch):
    replies = []

    def request(method, url, **kwargs):
        path = urlsplit(url).path
        if path == "/fapi/v1/time":
            return response({"serverTime": 1000000})
        if path == "/fapi/v1/exchangeInfo":
            return response(EXCHANGE_INFO)
        if path == "/fapi/v1/premiumIndex":
            return response({"markPrice": "60000"})
        assert replies, f"Unexpected HTTP call: {method} {path}"
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    mock = Mock(side_effect=request)
    monkeypatch.setattr(requests.Session, "request", mock)
    monkeypatch.setattr(binance.time, "sleep", Mock())
    monkeypatch.setattr(binance.time, "time", lambda: 1000.0)
    monkeypatch.setattr(binance.time, "monotonic", Mock(return_value=1000.0))
    monkeypatch.setattr(binance, "_EXCHANGE_INFO", {})
    return mock, replies


@pytest.fixture
def client(secrets, http):
    return binance.BinanceClient()


def test_signing(client, http):
    mock, replies = http
    replies.append(response([]))
    client.positions()
    call = mock.call_args
    query, signature = call.kwargs["params"].rsplit("&signature=", 1)
    assert signature == hmac.new(b"demo-secret", query.encode(), hashlib.sha256).hexdigest()
    assert params(call)["timestamp"] == "1000000" and params(call)["recvWindow"] == "5000"
    assert call.kwargs["headers"] == {"X-MBX-APIKEY": "demo-key"}
    prepared = requests.Request(*call.args, params=call.kwargs["params"], headers=call.kwargs["headers"]).prepare()
    assert "demo-secret" not in prepared.url + repr(prepared.headers)
    assert "demo-key" not in prepared.url and call.kwargs["allow_redirects"] is False


def test_secrets_permissions(secrets):
    secrets.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        binance.BinanceClient()


@pytest.mark.parametrize("mode", ["demo", "live"])
def test_tickers_use_live_public_data_without_credentials(client, http, mode):
    mock, replies = http
    client.mode, client.base_url = mode, binance._BASE_URLS[mode]
    replies.append(response([]))
    assert client.tickers() == []
    assert mock.call_args.args == ("GET", "https://fapi.binance.com/fapi/v1/ticker/24hr")
    assert mock.call_args.kwargs["headers"] == {} and mock.call_args.kwargs["params"] == ""
    assert client.base_url == binance._BASE_URLS[mode]


@pytest.mark.parametrize("mode", ["demo", "live"])
def test_live_symbol_metadata_is_unsigned_and_cached_separately(client, http, mode):
    mock, _ = http
    client.mode, client.base_url = mode, binance._BASE_URLS[mode]
    client.symbols()
    client.symbols(live=True)
    client.symbols(live=True)
    client.symbols()
    urls = [call.args[1] for call in mock.call_args_list]
    assert urls == ([client.base_url + "/fapi/v1/exchangeInfo"] +
                    ([binance._BASE_URLS["live"] + "/fapi/v1/exchangeInfo"] if mode == "demo" else []))
    assert all(call.kwargs["headers"] == {} and call.kwargs["params"] == "" for call in mock.call_args_list)
    assert client.base_url == binance._BASE_URLS[mode]


def test_rounding_and_cache_expiry(client, http, monkeypatch):
    filters = client.filters("BTCUSDT")
    assert filters == {"tick_size": Decimal("0.1"), "step_size": Decimal("0.001"),
                       "min_qty": Decimal("0.001"), "min_notional": Decimal("50")}
    assert binance.round_quantity("0.001999", "60000", filters) == Decimal("0.001")
    assert binance.round_quantity("0.001", "50000", filters) == Decimal("0.001")
    assert binance.round_price("60000.15", "0.1") == Decimal("60000.2")
    assert binance.round_price("1.13", "0.25") == Decimal("1.25")
    for quantity, price, error in [("0.000999", "60000", "min qty"), ("0.001999", "49999", "min notional")]:
        with pytest.raises(ValueError, match=error):
            binance.round_quantity(quantity, price, filters)
        monkeypatch.setattr(client, "mark_price", lambda symbol, price=price: {"markPrice": price})
        with pytest.raises(ValueError, match=error):
            client.market_order("BTCUSDT", "BUY", quantity, "bot-small")
    assert not any(call.args[0] == "POST" for call in http[0].call_args_list)
    for bad in ("-1", "0", "NaN", "Infinity"):
        with pytest.raises(ValueError, match="positive and finite"):
            binance.round_quantity(bad, "60000", filters)
        with pytest.raises(ValueError, match="positive and finite"):
            binance.round_price(bad, "0.1")
    monkeypatch.setitem(EXCHANGE_INFO["symbols"][0]["filters"][0], "tickSize", "0.2")
    binance.time.monotonic.return_value += 3599
    assert binance.BinanceClient().filters("BTCUSDT")["tick_size"] == Decimal("0.1")
    assert http[0].call_count == 1
    binance.time.monotonic.return_value += 1
    assert client.filters("BTCUSDT")["tick_size"] == Decimal("0.2")
    assert http[0].call_count == 2


@pytest.mark.parametrize("algo, failure", [(False, requests.Timeout()), (True, requests.ConnectionError()),
                                          (False, response({"code": -1007}, 503)), (False, "invalid-json")])
@pytest.mark.parametrize("misses", [0, 2])
def test_uncertain_post_polls_without_reposting(client, http, algo, failure, misses):
    mock, replies = http
    existing = {"algoId": 42} if algo else {"orderId": 42}
    if failure == "invalid-json":
        failure = response(None, 503)
        failure._content = b"<html>Service unavailable</html>"
    replies.append(failure)
    replies.extend(response({"code": -2013, "msg": "Order does not exist"}, 400) for _ in range(misses))
    replies.append(response(existing))
    place = client.protective_order if algo else client.market_order
    assert place("BTCUSDT", "SELL", "55000" if algo else "0.0019", "bot-order") == existing
    posts = [call for call in mock.call_args_list if call.args[0] == "POST"]
    assert len(posts) == 1
    path, id_key = ("algoOrder", "clientAlgoId") if algo else ("order", "origClientOrderId")
    lookups = [call for call in mock.call_args_list if call.args == ("GET", f"{client.base_url}/fapi/v1/{path}")]
    assert len(lookups) == misses + 1 and all(params(call)[id_key] == "bot-order" for call in lookups)
    assert [call.args for call in binance.time.sleep.call_args_list] == [(1,)] * misses


@pytest.mark.parametrize("lookup_code, tries", [(-2013, 5), (-2015, 1)])
def test_unknown_order_outcome_never_reposts(client, http, lookup_code, tries):
    mock, replies = http
    replies.append(requests.Timeout("demo-secret"))
    replies.extend(response({"code": lookup_code}, 400) for _ in range(tries))
    with pytest.raises(binance.BinanceError, match="reconcile by client id"):
        client.market_order("BTCUSDT", "BUY", "0.001", "bot-entry")
    assert sum(call.args[0] == "POST" for call in mock.call_args_list) == 1
    assert sum(call.args == ("GET", f"{client.base_url}/fapi/v1/order") for call in mock.call_args_list) == tries
    assert binance.time.sleep.call_count == tries - 1


def test_ensure_setup_is_idempotent(client, http):
    mock, replies = http
    replies.extend([response({"code": -4059}, 400), response({"code": -4046}, 400), response({"leverage": 5})])
    client.ensure_setup("BTCUSDT", 5)
    posts = [params(call) for call in mock.call_args_list if call.args[0] == "POST"]
    assert posts[0]["dualSidePosition"] == "false" and posts[1]["marginType"] == "ISOLATED"
    assert posts[2]["leverage"] == "5"
    replies.append(response({"code": -4067}, 400))
    with pytest.raises(binance.BinanceError) as raised:
        client.ensure_setup("BTCUSDT", 5)
    assert raised.value.code == -4067


@pytest.mark.parametrize("take_profit", [False, True])
def test_protection_closes_all_on_mark_price(client, http, take_profit):
    mock, replies = http
    replies.append(response({"algoId": 1}))
    client.protective_order("BTCUSDT", "SELL", "55000.15", "bot-protect", take_profit=take_profit)
    assert mock.call_args.args == ("POST", f"{client.base_url}/fapi/v1/algoOrder")
    data = params(mock.call_args)
    assert data.items() >= {"type": "TAKE_PROFIT_MARKET" if take_profit else "STOP_MARKET",
                            "algoType": "CONDITIONAL", "workingType": "MARK_PRICE", "closePosition": "true",
                            "triggerPrice": "55000.2", "clientAlgoId": "bot-protect"}.items()
    assert not {"quantity", "reduceOnly", "stopPrice"} & data.keys()


def test_cancel_attempts_both_order_kinds_after_failure(client, http):
    mock, replies = http
    replies.extend([response({"code": -1000}, 500), response({"code": 200})])
    with pytest.raises(binance.BinanceError):
        client.cancel_all_orders("BTCUSDT")
    assert [urlsplit(call.args[1]).path for call in mock.call_args_list if call.args[0] == "DELETE"] == [
        "/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"]


def test_timestamp_resyncs_once(client, http):
    mock, replies = http
    replies.extend([response({"code": -1021}, 400), response({"orderId": 42})])
    client._offset = -10000
    client.market_order("BTCUSDT", "SELL", "0.001", "bot-exit", reduce_only=True)
    posts = [params(call) for call in mock.call_args_list if call.args[0] == "POST"]
    assert [p["timestamp"] for p in posts] == ["990000", "1000000"]
    assert posts[0]["signature"] != posts[1]["signature"] and posts[1]["reduceOnly"] == "true"
    replies.extend([response({"code": -1021}, 400), response({"code": -1021}, 400)])
    with pytest.raises(binance.BinanceError) as raised:
        client.positions()
    assert raised.value.code == -1021
    assert sum(call.args[1].endswith("/time") for call in mock.call_args_list) == 2


@pytest.mark.parametrize("status", [429, 503])
def test_get_retries_are_bounded_and_honor_retry_after(client, http, status):
    mock, replies = http
    replies.extend([response({}, 429, {"Retry-After": "2"}), response({}, 503), requests.ConnectionError(), response([])])
    assert client.tickers() == []
    assert mock.call_count == 4
    assert [call.args[0] for call in binance.time.sleep.call_args_list] == [2, 0.5, 1]
    replies.extend(response({"code": -1000}, status) for _ in range(4))
    with pytest.raises(binance.BinanceError):
        client.tickers()
    assert mock.call_count == 8 and binance.time.sleep.call_count == 6


def test_errors_redact_credentials_and_signatures(client, http):
    mock, _ = http
    client._offset = 0
    mock.side_effect = lambda *args, **kw: response({"code": -1022, "msg": f"demo-key demo-secret {kw['params']}"}, 400)
    with pytest.raises(binance.BinanceError) as raised:
        client.positions()
    signature = params(mock.call_args)["signature"]
    sensitive = ("demo-key", "demo-secret", signature)
    assert all(value not in "".join(traceback.format_exception(raised.value)) for value in sensitive)
    mock.side_effect = requests.ConnectionError(" ".join(sensitive))
    with pytest.raises(binance.BinanceError) as raised:
        client.positions()
    assert all(value not in "".join(traceback.format_exception(raised.value)) for value in sensitive)
    assert binance.time.sleep.call_count == 3


def test_income_history_preserves_boundary_records(client, http):
    mock, replies = http
    batch = [{"incomeType": "COMMISSION", "tranId": i, "time": i + 10} for i in range(1000)]
    extra = {"incomeType": "REALIZED_PNL", "tranId": 1000, "time": 1009}
    funding = {"incomeType": "FUNDING_FEE", "tranId": 1001, "time": 1010}
    replies.extend([response(batch), response([batch[-1], extra, funding])])
    rows = client.income_history(10)
    assert len(rows) == 1002 and rows[-2:] == [extra, funding]
    assert params(mock.call_args)["startTime"] == "1009"


def test_main_is_read_only(client, http, capsys):
    mock, replies = http
    replies.extend([response([{"asset": "USDT", "balance": "100", "availableBalance": "80"}]),
                    response([{"symbol": "BTCUSDT", "positionAmt": "0.001"}, {"symbol": "ETHUSDT", "positionAmt": "0"}])])
    binance.main()
    output = capsys.readouterr().out
    assert "demo" in output and client.base_url in output and "USDT" in output
    assert "BTCUSDT" in output and "ETHUSDT" not in output
    assert all(call.args[0] == "GET" for call in mock.call_args_list)


def test_reduce_only_ignores_min_notional_but_keeps_quantity_filters(client, http, monkeypatch):
    mock, replies = http
    monkeypatch.setattr(client, "mark_price", lambda symbol: {"markPrice": "100"})
    replies.append(response({"orderId": 42}))
    client.market_order("BTCUSDT", "SELL", "0.0019", "bot-dust", reduce_only=True)
    assert params(mock.call_args)["quantity"] == "0.001"
    assert params(mock.call_args)["reduceOnly"] == "true"
    with pytest.raises(ValueError, match="min qty"):
        client.market_order("BTCUSDT", "SELL", "0.0009", "bot-small", reduce_only=True)
    with pytest.raises(ValueError, match="min notional"):
        client.market_order("BTCUSDT", "BUY", "0.0019", "bot-entry")
    assert sum(call.args[0] == "POST" for call in mock.call_args_list) == 1
