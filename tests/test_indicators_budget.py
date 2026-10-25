from datetime import date

import pandas as pd
import pytest

from bot.budget import Ledger, iso_week, weeks_between
from bot.config import ConfigError, load_config, validate
from bot.indicators import compute_signal, rsi, sma

from conftest import ROOT, make_series


def test_sma_and_rsi_basics():
    s = pd.Series(range(1, 31), dtype=float)
    assert sma(s, 5).iloc[-1] == pytest.approx(28.0)
    assert rsi(s, 14).iloc[-1] == pytest.approx(100.0)
    assert rsi(pd.Series(range(30, 0, -1), dtype=float), 14).iloc[-1] == pytest.approx(0.0, abs=1e-9)
    assert rsi(pd.Series([10.0] * 30), 14).iloc[-1] == pytest.approx(50.0)


def test_signal_uptrend_and_downtrend(cfg):
    up = compute_signal("X", "etf", make_series(daily=0.002, noise=0.003), cfg["signals"])
    assert up.trend_up and up.above_long and up.momentum > 0
    down = compute_signal("Y", "etf", make_series(daily=-0.002, noise=0.003), cfg["signals"])
    assert not down.trend_up and not down.above_long and down.momentum < 0


def test_signal_needs_history(cfg):
    assert compute_signal("X", "etf", make_series(n=40), cfg["signals"]) is None
    assert compute_signal("X", "etf", None, cfg["signals"]) is None
    short = compute_signal("X", "etf", make_series(n=120, daily=0.002), cfg["signals"])
    assert short.sma_long is None and short.trend_up  # repli sur la moyenne courte


def test_ledger_weekly_credit_rollover_and_cap():
    led = Ledger({})
    assert "Premier" in led.roll(date(2026, 10, 26), 50, 4)
    assert led.reserve == 50
    assert led.roll(date(2026, 10, 28), 50, 4) is None          # même semaine
    led.spend(30)
    assert led.reserve == 20 and led.spent_week == 30
    led.roll(date(2026, 11, 2), 50, 4)                            # semaine suivante
    assert led.reserve == 70 and led.spent_week == 0
    led.roll(date(2026, 11, 30), 50, 4)                           # 4 semaines manquées → plafond 200
    assert led.reserve == 200
    led.add_proceeds(100)                                         # le produit d'une vente n'est pas rogné
    led.roll(date(2026, 12, 7), 50, 4)
    assert led.reserve == 300


def test_iso_week_helpers():
    assert iso_week(date(2026, 1, 1)) == "2026-W01"
    assert weeks_between("2025-W52", "2026-W02") == 2


def test_config_validation(cfg):
    sleeves = [a["sleeve"] for a in cfg["universe"]]
    assert sleeves.count("etf") == 8 and sleeves.count("actions") == 20 and sleeves.count("crypto") == 2
    stock = next(a for a in cfg["universe"] if a["sleeve"] == "actions")
    with pytest.raises(ConfigError, match="socle"):
        validate(dict(cfg, universe=[dict(stock, core=True)] + cfg["universe"][1:]))
    bad = dict(cfg, allocation=dict(cfg["allocation"], etf=0.9))
    with pytest.raises(ConfigError):
        validate(bad)
    env_cfg = load_config(ROOT / "config.yaml", env={"WEEKLY_BUDGET_EUR": "30,5"})
    assert env_cfg["budget"]["weekly_eur"] == 30.5
