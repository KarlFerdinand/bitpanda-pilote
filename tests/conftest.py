import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.data import to_eur  # noqa: E402

EUR_ID = "eur-0000"


def make_series(start=100.0, daily=0.001, n=300, noise=0.0, seed=0, tail=None, end="2026-10-23"):
    """Série de cours synthétique. tail = liste de rendements quotidiens ajoutés à la fin."""
    rng = np.random.default_rng(seed)
    rets = np.full(n, daily) + (rng.normal(0, noise, n) if noise else 0.0)
    if tail:
        rets = np.concatenate([rets[: n - len(tail)], np.array(tail)])
    prices = start * np.cumprod(1 + rets)
    idx = pd.bdate_range(end=end, periods=n)
    return pd.Series(prices, index=idx)


class FakeData:
    def __init__(self, histories: dict, vix=18.0, fng=50):
        self.histories = histories
        self._vix = vix
        self._fng = fng

    def history(self, ticker):
        return self.histories.get(ticker)

    def history_eur(self, ticker, currency="EUR"):
        prices = self.history(ticker)
        if prices is None or currency.upper() == "EUR":
            return prices
        fx = self.history(f"EUR{currency.upper()}=X")
        return None if fx is None else to_eur(prices, fx)

    def vix(self):
        return self._vix

    def crypto_fear_greed(self):
        return self._fng


def uptrend_histories(cfg, daily=0.0012):
    out = {}
    for i, a in enumerate(cfg["universe"]):
        out[a["yahoo"]] = make_series(start=50 + 10 * i, daily=daily + 0.0001 * (i % 10), noise=0.004, seed=i)
    out["EURUSD=X"] = make_series(start=1.10, daily=0.0, n=320)
    return out


class FakeClient:
    """Imite l'API publique Bitpanda (réponses au format de la documentation)."""

    def __init__(self, cfg, prices: dict, limits=True, portfolio=None, fee_pct=0.0099, price_skew=0.0):
        self.cfg = cfg
        self.prices = prices          # key -> prix EUR
        self.limits = limits
        self.extra_rows = portfolio or []
        self.fee_pct = fee_pct
        self.price_skew = price_skew
        self.ids = {a["key"]: f"id-{a['key']}" for a in cfg["universe"]}
        self.by_id = {v: k for k, v in self.ids.items()}
        self.quotes = {}
        self.accepted = []
        self.balances = {}            # asset_id -> qty
        self.cash = 1000.0
        self.limit_calls = []

    def currencies(self):
        return [{"id": "usd-1", "symbol": "USD", "name": "Dollar"}, {"id": EUR_ID, "symbol": "EUR", "name": "Euro"}]

    def assets(self, **filters):
        rows = []
        for a in self.cfg["universe"]:
            if filters.get("isin") and a.get("isin") != filters["isin"]:
                continue
            if filters.get("symbol") and a.get("symbol") != filters["symbol"]:
                continue
            rows.append({"id": self.ids[a["key"]], "name": a["name"], "symbol": a.get("symbol", ""),
                         "isin": a.get("isin", ""), "type": "CRYPTO" if a["sleeve"] == "crypto" else "SECURITY",
                         "buy_active": True, "sell_active": True})
        return rows

    def ticker(self, asset_id):
        return {"asset_id": asset_id, "price": str(self.prices[self.by_id[asset_id]]), "currency_id": EUR_ID}

    def portfolio(self, equivalent_currency_id=None):
        rows = [{"asset_id": EUR_ID, "currency_id": EUR_ID,
                 "balance": {"value": str(self.cash)}, "available_balance": {"value": str(self.cash)}}]
        for aid, qty in self.balances.items():
            rows.append({"asset_id": aid, "currency_id": EUR_ID, "balance": {"value": str(qty)},
                         "available_balance": {"value": str(qty)}})
        return rows + self.extra_rows

    def trading_limits(self):
        if not self.limits:
            return None
        return {"buy_limit": 60, "sell_limit": 2000, "currency_id": EUR_ID,
                "buy_budget_remaining": 60, "sell_budget_remaining": 2000}

    def set_trading_limits(self, buy_limit, sell_limit, currency_id):
        self.limit_calls.append((buy_limit, sell_limit, currency_id))
        self.limits = True
        return {"buy_limit": buy_limit, "sell_limit": sell_limit, "currency_id": currency_id}

    def create_quote(self, body):
        key = self.by_id[body["asset_id"]]
        price = self.prices[key] * (1 + self.price_skew)
        if "notional" in body:
            notional = float(body["notional"])
            fee = notional * self.fee_pct
            qty = (notional - fee) / price
        else:
            qty = float(body["quantity"])
            notional = qty * price
            fee = notional * self.fee_pct
        qid = str(uuid.uuid4())
        quote = {"quote_id": qid, "status": "OPEN", "side": body["side"], "asset_id": body["asset_id"],
                 "currency_id": body["currency_id"],
                 "quote": {"price": f"{price:.6f}", "price_with_fee": f"{price * (1 + self.fee_pct):.6f}",
                           "fee": f"{fee:.4f}", "quantity": f"{qty:.8f}", "notional": f"{notional:.2f}",
                           "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                           "to_euro_rate": "1"},
                 "key_information_documents": ([{"country": "FR", "language": "fr", "url": f"https://kid.example/{key}"}]
                                               if "isin" in self.cfg_asset(key) else []),
                 "warnings": []}
        self.quotes[qid] = (body, quote)
        return quote

    def cfg_asset(self, key):
        return next(a for a in self.cfg["universe"] if a["key"] == key)

    def accept_quote(self, quote_id):
        body, quote = self.quotes.pop(quote_id)
        q = quote["quote"]
        qty = float(q["quantity"])
        aid = body["asset_id"]
        if body["side"] == "BUY":
            self.balances[aid] = self.balances.get(aid, 0.0) + qty
            self.cash -= float(q["notional"])
        else:
            self.balances[aid] = self.balances.get(aid, 0.0) - qty
            self.cash += float(q["notional"]) - float(q["fee"])
        self.accepted.append(body)
        return {"quote_id": quote_id, "trade_id": f"t-{len(self.accepted)}", "status": "FILLED",
                "side": body["side"], "asset_id": aid, "currency_id": EUR_ID,
                "execution": {"price": q["price"], "price_with_fee": q["price_with_fee"], "fee": q["fee"],
                              "quantity": q["quantity"], "notional": q["notional"],
                              "executed_at": datetime.now(timezone.utc).isoformat(), "to_euro_rate": "1"}}


@pytest.fixture
def cfg():
    c = load_config(ROOT / "config.yaml", env={})
    c["agent"]["enabled"] = False
    return c
