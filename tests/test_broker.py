from datetime import datetime, timedelta, timezone

import pytest
import requests

from bot.broker import (NOTIONAL_RE, QUANTITY_RE, BitpandaClient, BitpandaError, LiveBroker, PaperBroker,
                        UncertainExecution, format_notional, format_quantity)

from conftest import EUR_ID, FakeClient


@pytest.mark.parametrize("amount,expected", [(12.5, "12.5"), (10, "10"), (9.999, "9.99"), (0.05, "0.05"), (16.0, "16")])
def test_format_notional(amount, expected):
    assert format_notional(amount) == expected
    assert NOTIONAL_RE.match(expected)


@pytest.mark.parametrize("qty,expected", [(0.000123456789, "0.00012345"), (1.5, "1.5"), (2, "2"), (0.1, "0.1")])
def test_format_quantity(qty, expected):
    assert format_quantity(qty) == expected
    assert QUANTITY_RE.match(expected)


def test_format_rejects_zero():
    with pytest.raises(ValueError):
        format_notional(0.001)
    with pytest.raises(ValueError):
        format_quantity(1e-10)


def prices(cfg):
    return {a["key"]: 100.0 for a in cfg["universe"]}


def test_live_buy_happy_path_and_kid(cfg):
    client = FakeClient(cfg, prices(cfg))
    broker = LiveBroker(client, cfg, {})
    broker.resolve(cfg["universe"])
    fill = broker.buy("SP500", 12.34, ref_price=100.0)
    assert client.accepted[0]["notional"] == "12.34" and client.accepted[0]["currency_id"] == EUR_ID
    assert fill.quantity > 0 and fill.trade_id == "t-1"
    assert fill.kid_links == ["https://kid.example/SP500"]
    assert broker.holdings()["SP500"].quantity == pytest.approx(fill.quantity)
    assert broker.cash_eur() == pytest.approx(1000 - 12.34)


def test_live_rejects_price_gap_and_high_fees(cfg):
    client = FakeClient(cfg, prices(cfg), price_skew=0.05)
    broker = LiveBroker(client, cfg, {})
    broker.resolve(cfg["universe"])
    with pytest.raises(BitpandaError, match="trop éloigné"):
        broker.buy("SP500", 20, ref_price=100.0)
    client = FakeClient(cfg, prices(cfg), fee_pct=0.04)
    broker = LiveBroker(client, cfg, {})
    broker.resolve(cfg["universe"])
    with pytest.raises(BitpandaError, match="frais"):
        broker.buy("BTC", 20, ref_price=100.0)
    assert not client.accepted


def test_live_requotes_once_when_expired(cfg):
    client = FakeClient(cfg, prices(cfg))
    calls = {"n": 0}
    original = client.create_quote

    def create(body):
        q = original(body)
        calls["n"] += 1
        if calls["n"] == 1:
            q["quote"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        return q

    client.create_quote = create
    broker = LiveBroker(client, cfg, {})
    broker.resolve(cfg["universe"])
    broker.buy("SP500", 10, ref_price=100.0)
    assert calls["n"] == 2 and len(client.accepted) == 1


def test_live_sell_uses_quantity(cfg):
    client = FakeClient(cfg, prices(cfg))
    broker = LiveBroker(client, cfg, {})
    broker.resolve(cfg["universe"])
    broker.buy("ETH", 20, ref_price=100.0)
    qty = broker.holdings()["ETH"].quantity
    fill = broker.sell("ETH", qty, ref_price=100.0)
    assert client.accepted[-1]["side"] == "SELL" and "quantity" in client.accepted[-1]
    assert fill.notional > 0


def test_unknown_asset_is_not_tradeable(cfg):
    client = FakeClient(cfg, prices(cfg))
    client.assets = lambda **f: []
    broker = LiveBroker(client, cfg, {})
    refs = broker.resolve(cfg["universe"])
    assert all(r.asset_id is None for r in refs.values())
    assert not broker.tradeable("SP500")


class Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        return self._payload


class SeqSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_client_retries_429_then_succeeds_and_sends_key():
    session = SeqSession([Resp(429), Resp(200, {"data": [{"id": "x", "symbol": "EUR"}]})])
    client = BitpandaClient("secret-key", session=session, sleep=lambda s: None)
    assert client.currencies()[0]["symbol"] == "EUR"
    assert session.calls[0][2]["headers"]["x-api-key"] == "secret-key"
    assert len(session.calls) == 2


def test_accept_is_never_retried_and_network_error_is_uncertain():
    session = SeqSession([requests.ConnectionError("boom"), Resp(200, {"data": {}})])
    client = BitpandaClient("k", session=session, sleep=lambda s: None)
    with pytest.raises(UncertainExecution):
        client.accept_quote("q1")
    assert len(session.calls) == 1
    session = SeqSession([Resp(422, {"error": {"code": "INSUFFICIENT_FUNDS"}})])
    client = BitpandaClient("k", session=session, sleep=lambda s: None)
    with pytest.raises(BitpandaError) as err:
        client.accept_quote("q2")
    assert err.value.code == "INSUFFICIENT_FUNDS" and not isinstance(err.value, UncertainExecution)


def test_trading_limits_404_means_none():
    session = SeqSession([Resp(404, {"error": {"code": "NOT_FOUND"}})])
    client = BitpandaClient("k", session=session, sleep=lambda s: None)
    assert client.trading_limits() is None


def test_paper_broker_round_trip(cfg):
    state = {}
    broker = PaperBroker(cfg, state)
    fill = broker.buy("BTC", 20, 100.0)
    assert fill.fee == pytest.approx(0.3) and fill.quantity == pytest.approx(0.197)
    out = broker.sell("BTC", 1.0, 110.0)
    assert out.quantity == pytest.approx(0.197)
    assert "BTC" not in state["paper"]["positions"]
