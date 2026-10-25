from datetime import date

import pandas as pd
import pytest

from bot.config import assets_by_key
from bot.data import to_eur
from bot.indicators import Signal
from bot.strategy import Holding, MarketContext, build_plan, plan_sells, sleeve_split

TUESDAY = date(2026, 10, 27)


def sig(key, sleeve, momentum=0.10, vol=0.25, trend=True, price=100.0, rsi=55.0):
    long = price * (0.9 if trend else 1.1)
    short = price * (0.95 if trend else 1.05)
    return Signal(key, sleeve, price, short, long, rsi, momentum, vol, -0.02, 300)


def base_signals(cfg, trend_stocks=True):
    out = {}
    for a in cfg["universe"]:
        if a["sleeve"] == "actions":
            out[a["key"]] = sig(a["key"], "actions", momentum=0.05, vol=0.30, trend=trend_stocks)
        else:
            out[a["key"]] = sig(a["key"], a["sleeve"], momentum=0.04, vol=0.15)
    return out


def plan(cfg, signals, **kw):
    args = dict(holdings={}, meta={}, reserve=50.0, spent=0.0, pending={"etf": 0.0, "actions": 0.0, "crypto": 0.0},
                market=MarketContext(), tradeable=lambda k, s="BUY": True, cash=None)
    args.update(kw)
    return build_plan(TUESDAY, cfg, assets_by_key(cfg), signals, args["holdings"], args["meta"], args["reserve"],
                      args["spent"], args["pending"], args["market"], args["tradeable"], args["cash"])


def test_usd_prices_are_converted_to_eur_across_timezones():
    ny = pd.DatetimeIndex(["2026-10-21 00:00", "2026-10-22 00:00", "2026-10-23 00:00"], tz="America/New_York")
    ldn = pd.DatetimeIndex(["2026-10-21 00:00", "2026-10-22 00:00"], tz="Europe/London")  # 23/10 manquant
    usd = pd.Series([110.0, 121.0, 132.0], index=ny)
    eurusd = pd.Series([1.10, 1.21], index=ldn)
    eur = to_eur(usd, eurusd)
    assert list(eur.round(6)) == [100.0, 100.0, pytest.approx(132 / 1.21)]
    assert eur.index.tz is None


def test_actions_ranked_by_risk_adjusted_momentum(cfg):
    s = base_signals(cfg)
    s["NVDA"] = sig("NVDA", "actions", momentum=0.40, vol=0.80)   # 0,50 par unité de risque
    s["V"] = sig("V", "actions", momentum=0.20, vol=0.15)         # 1,33 par unité de risque
    p = plan(cfg, s, pending={"etf": 0.0, "actions": 10.0, "crypto": 0.0})
    stock_buys = [o for o in p.buys if o.sleeve == "actions"]
    assert [o.key for o in stock_buys] == ["V"]


def test_no_stock_in_trend_moves_provision_to_etf(cfg):
    s = base_signals(cfg, trend_stocks=False)
    p = plan(cfg, s, pending={"etf": 0.0, "actions": 15.0, "crypto": 0.0})
    assert not [o for o in p.buys if o.sleeve == "actions"]
    assert sum(o.amount_eur for o in p.buys if o.sleeve == "etf") >= 15.0
    assert any("part sur les ETF" in n for n in p.notes)


def test_single_stock_capped_at_8_percent(cfg):
    s = base_signals(cfg)
    s["V"] = sig("V", "actions", momentum=0.30, vol=0.15)
    holdings = {"V": Holding(80 / 100.0), "MSCI_WORLD": Holding(620 / 100.0), "BTC": Holding(200 / 100.0)}
    p = plan(cfg, s, holdings=holdings, pending={"etf": 0.0, "actions": 20.0, "crypto": 0.0}, reserve=100)
    assert "V" not in {o.key for o in p.buys}
    assert any("Visa écarté" in n for n in p.notes)


def test_split_three_sleeves_rebalances_new_money(cfg):
    split, note = sleeve_split({"etf": 820.0, "actions": 0.0, "crypto": 180.0}, 1000.0, cfg)
    assert split["etf"] == 0 and split["actions"] > split["crypto"] > 0
    assert abs(sum(split.values()) - 1) < 1e-9 and "rattrapage" in note
    split, note = sleeve_split({"etf": 450.0, "actions": 350.0, "crypto": 200.0}, 1000.0, cfg)
    assert split["actions"] == 0 and "plafond" in note


def test_stock_trailing_stop_is_20_percent(cfg):
    s = base_signals(cfg)
    s["AAPL"] = sig("AAPL", "actions", price=82.0, trend=False)
    meta = {"AAPL": {"first_buy": "2026-09-01", "peak": 100.0}}
    sells = plan_sells(s, {"AAPL": Holding(1.0, 90.0)}, meta, assets_by_key(cfg), cfg, TUESDAY, lambda k, side: True)
    assert sells and sells[0].key == "AAPL"
    meta["AAPL"]["peak"] = 95.0  # -13,7 % seulement : pas de stop suiveur, mais tendance cassée
    sells = plan_sells(s, {"AAPL": Holding(1.0, 90.0)}, meta, assets_by_key(cfg), cfg, TUESDAY, lambda k, side: True)
    assert sells and "tendance cassée" in sells[0].reason
