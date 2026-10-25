from datetime import date

from bot.config import assets_by_key
from bot.indicators import compute_signal
from bot.strategy import Holding, MarketContext, build_plan, plan_sells, sleeve_multiplier

from conftest import make_series, uptrend_histories

MONDAY, TUESDAY, FRIDAY = date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 30)


def signals_for(cfg, histories):
    out = {}
    for a in cfg["universe"]:
        s = compute_signal(a["key"], a["sleeve"], histories.get(a["yahoo"]), cfg["signals"])
        if s:
            out[a["key"]] = s
    return out


def always(key, side="BUY"):
    return True


def plan(cfg, signals, *, today=TUESDAY, holdings=None, meta=None, reserve=50.0, spent=0.0,
         pending=None, market=None, cash=None, tradeable=always):
    return build_plan(today, cfg, assets_by_key(cfg), signals, holdings or {}, meta or {}, reserve, spent,
                      pending or {"etf": 0.0, "crypto": 0.0}, market or MarketContext(), tradeable, cash)


def test_pending_accumulates_until_min_order(cfg):
    sig = signals_for(cfg, uptrend_histories(cfg))
    p1 = plan(cfg, sig, today=MONDAY)
    m = p1.multipliers["etf"]
    assert not p1.buys or all(o.amount_eur >= 10 for o in p1.buys)
    if m * 8 < 10:  # 50 €/5 j × 80 % = 8 € de provision ETF par jour à rythme normal
        assert not [o for o in p1.buys if o.sleeve == "etf"]
        p2 = plan(cfg, sig, pending=p1.pending)
        assert [o for o in p2.buys if o.sleeve == "etf"]


def test_never_spends_more_than_reserve_or_daily_cap(cfg):
    sig = signals_for(cfg, uptrend_histories(cfg))
    p = plan(cfg, sig, reserve=12.0, pending={"etf": 80.0, "crypto": 30.0})
    assert sum(o.amount_eur for o in p.buys) <= 12.0 + 1e-9
    p = plan(cfg, sig, reserve=500.0, pending={"etf": 400.0, "crypto": 100.0})
    assert sum(o.amount_eur for o in p.buys) <= 50.0 + 1e-9   # 1 semaine de budget max par jour


def test_downtrend_slows_down(cfg):
    hist = {a["yahoo"]: make_series(daily=-0.0015, noise=0.003, seed=i) for i, a in enumerate(cfg["universe"])}
    sig = signals_for(cfg, hist)
    m, reasons = sleeve_multiplier("etf", sig["MSCI_WORLD"], MarketContext(vix=20), cfg)
    assert m <= 0.5 and "baissière" in reasons[0]
    p = plan(cfg, sig, pending={"etf": 20.0, "crypto": 0.0})
    # aucun actif en tendance : repli sur le socle (achats réguliers), jamais sur un satellite baissier
    assert all(o.key == "MSCI_WORLD" for o in p.buys if o.sleeve == "etf")


def test_dip_in_uptrend_accelerates(cfg):
    tail = [-0.012] * 6
    s = make_series(daily=0.0015, noise=0.002, tail=tail)
    ref = compute_signal("MSCI_WORLD", "etf", s, cfg["signals"])
    assert ref.above_long and ref.rsi < 35
    m, _ = sleeve_multiplier("etf", ref, MarketContext(vix=20), cfg)
    assert m >= 1.5


def test_fear_and_agent_bias_are_bounded(cfg):
    sig = signals_for(cfg, uptrend_histories(cfg))
    calm, _ = sleeve_multiplier("etf", sig["MSCI_WORLD"], MarketContext(vix=20), cfg)
    fear, _ = sleeve_multiplier("etf", sig["MSCI_WORLD"], MarketContext(vix=35), cfg)
    assert fear > calm
    biased, _ = sleeve_multiplier("etf", sig["MSCI_WORLD"], MarketContext(vix=20, bias={"etf": 5}), cfg)
    assert biased <= calm * 1.2 + 1e-9  # biais borné à ±20 %
    worst, _ = sleeve_multiplier("crypto", sig["BTC"], MarketContext(crypto_fng=90, bias={"crypto": -1}), cfg)
    assert 0 <= worst < 1


def test_friday_floor_invests_on_core(cfg):
    hist = {a["yahoo"]: make_series(daily=-0.0015, noise=0.003, seed=i) for i, a in enumerate(cfg["universe"])}
    sig = signals_for(cfg, hist)
    p = plan(cfg, sig, today=FRIDAY, reserve=50.0, spent=0.0)
    core = [o for o in p.buys if o.key == "MSCI_WORLD"]
    assert core and sum(o.amount_eur for o in core) >= 20.0 - 1e-9
    p_done = plan(cfg, sig, today=FRIDAY, reserve=50.0, spent=25.0)
    assert not any("minimum de la semaine" in o.reason for o in p_done.buys)


def test_crypto_overweight_stops_crypto_buys(cfg):
    sig = signals_for(cfg, uptrend_histories(cfg))
    btc_qty = 400 / sig["BTC"].price
    world_qty = 600 / sig["MSCI_WORLD"].price
    holdings = {"BTC": Holding(btc_qty, sig["BTC"].price), "MSCI_WORLD": Holding(world_qty, sig["MSCI_WORLD"].price)}
    p = plan(cfg, sig, holdings=holdings, pending={"etf": 0.0, "crypto": 40.0})
    assert p.split["crypto"] == 0
    assert not [o for o in p.buys if o.sleeve == "crypto"]


def test_veto_and_untradeable_assets_are_skipped(cfg):
    sig = signals_for(cfg, uptrend_histories(cfg))
    keys_etf = [a["key"] for a in cfg["universe"] if a["sleeve"] == "etf"]
    market = MarketContext(veto=set(keys_etf) - {"MSCI_WORLD"})
    p = plan(cfg, sig, pending={"etf": 20.0, "crypto": 0.0}, market=market)
    assert {o.key for o in p.buys if o.sleeve == "etf"} <= {"MSCI_WORLD"}
    p = plan(cfg, sig, pending={"etf": 20.0, "crypto": 0.0}, tradeable=lambda k, s="BUY": k == "SP500")
    assert {o.key for o in p.buys} <= {"SP500"}


def test_trailing_stop_sells_satellite_but_never_core(cfg):
    crash = make_series(daily=0.002, noise=0.002, tail=[-0.01] * 25)
    hist = {a["yahoo"]: crash for a in cfg["universe"]}
    sig = signals_for(cfg, hist)
    peak = float(crash.max())
    holdings = {"NASDAQ100": Holding(1.0, 50.0), "MSCI_WORLD": Holding(1.0, 50.0)}
    meta = {"NASDAQ100": {"first_buy": "2026-09-01", "peak": peak},
            "MSCI_WORLD": {"first_buy": "2026-09-01", "peak": peak}}
    sells = plan_sells(sig, holdings, meta, assets_by_key(cfg), cfg, TUESDAY, always)
    assert [o.key for o in sells] == ["NASDAQ100"]
    assert "stop suiveur" in sells[0].reason
    meta["NASDAQ100"]["first_buy"] = "2026-10-20"   # détenu depuis moins de 14 jours
    assert not plan_sells(sig, holdings, meta, assets_by_key(cfg), cfg, TUESDAY, always)


def test_take_profit_trims_a_quarter(cfg):
    hot = make_series(daily=0.004, noise=0.0005)
    sig = signals_for(cfg, {a["yahoo"]: hot for a in cfg["universe"]})
    assert sig["SP500"].rsi > 75
    holdings = {"SP500": Holding(2.0, sig["SP500"].price / 1.5)}
    meta = {"SP500": {"first_buy": "2026-01-05", "peak": sig["SP500"].price}}
    sells = plan_sells(sig, holdings, meta, assets_by_key(cfg), cfg, TUESDAY, always)
    assert len(sells) == 1 and abs(sells[0].quantity - 0.5) < 1e-9
    meta["SP500"]["last_trim"] = "2026-10-20"
    assert not plan_sells(sig, holdings, meta, assets_by_key(cfg), cfg, TUESDAY, always)


def test_position_cap_and_max_orders(cfg):
    sig = signals_for(cfg, uptrend_histories(cfg))
    best = max((k for k, a in assets_by_key(cfg).items() if a["sleeve"] == "etf" and not a.get("core")),
               key=lambda k: sig[k].momentum)
    holdings = {best: Holding(450 / sig[best].price), "MSCI_WORLD": Holding(550 / sig["MSCI_WORLD"].price)}
    p = plan(cfg, sig, holdings=holdings, pending={"etf": 40.0, "crypto": 0.0}, reserve=100)
    assert best not in {o.key for o in p.buys}
    cfg["execution"]["max_orders_per_run"] = 1
    p = plan(cfg, sig, pending={"etf": 40.0, "crypto": 15.0}, reserve=100)
    assert len(p.buys) + len(p.sells) <= 1
