"""La configuration livrée : plus de crypto, actions filtrées par le consensus des analystes et plafonnées par thème."""
from datetime import date

from bot import main as botmain
from bot.config import assets_by_key
from bot.indicators import Signal
from bot.strategy import Holding, MarketContext, build_plan

from conftest import FakeData, uptrend_histories

TUESDAY = date(2026, 10, 27)


def sig(key, sleeve, momentum=0.08, vol=0.25, trend=True, price=100.0):
    return Signal(key, sleeve, price, price * (0.95 if trend else 1.05), price * (0.9 if trend else 1.1),
                  55.0, momentum, vol, -0.02, 300)


def signals(cfg, trend_stocks=True):
    return {a["key"]: sig(a["key"], a["sleeve"], trend=(trend_stocks or a["sleeve"] != "actions"))
            for a in cfg["universe"]}


def good(upside=0.10):
    return {"recommendation": 1.9, "analysts": 25, "upside": upside, "label": "buy"}


def plan(cfg, s, market, holdings=None, pending=None, reserve=60.0):
    return build_plan(TUESDAY, cfg, assets_by_key(cfg), s, holdings or {}, {}, reserve, 0.0,
                      pending or {"etf": 0.0, "actions": 20.0, "crypto": 30.0}, market,
                      lambda k, side="BUY": True, None)


def test_no_crypto_is_ever_provisioned_or_bought(cfg_real):
    p = plan(cfg_real, signals(cfg_real), MarketContext(analysts={k: good() for k in assets_by_key(cfg_real)}))
    assert p.pending["crypto"] == 0 and "crypto" not in p.multipliers
    assert all(o.sleeve in ("etf", "actions") for o in p.buys)


def test_analyst_consensus_filters_and_ranks_stocks(cfg_real):
    s = signals(cfg_real)
    consensus = {k: {"recommendation": 3.1, "analysts": 30, "upside": 0.02, "label": "hold"}
                 for k, a in assets_by_key(cfg_real).items() if a["sleeve"] == "actions"}
    consensus["LLY"] = good(upside=0.18)
    consensus["SAN"] = good(upside=0.05)
    p = plan(cfg_real, s, MarketContext(analysts=consensus))
    keys = [o.key for o in p.buys if o.sleeve == "actions"]
    assert keys == ["LLY", "SAN"]          # les seules notées « achat », la plus prometteuse en premier
    assert "analystes 1,9/5" in next(o for o in p.buys if o.key == "LLY").reason
    assert any("écartées par le consensus" in n for n in p.notes)


def test_no_consensus_means_no_stock_purchase(cfg_real):
    p = plan(cfg_real, signals(cfg_real), MarketContext(analysts={}))
    assert not [o for o in p.buys if o.sleeve == "actions"]
    assert sum(o.amount_eur for o in p.buys if o.sleeve == "etf") >= 20.0   # la provision part sur les ETF


def test_theme_cap_skips_crowded_theme(cfg_real):
    s = signals(cfg_real)
    analysts = {k: good() for k in assets_by_key(cfg_real)}
    analysts["NVDA"] = good(upside=0.40)                    # le préféré… mais l'IA est déjà pleine
    holdings = {"MSFT": Holding(2.5), "GOOGL": Holding(2.5), "MSCI_WORLD": Holding(15.0)}  # IA = 500 € / 2 000 €
    p = plan(cfg_real, s, MarketContext(analysts=analysts), holdings=holdings)
    picked = {assets_by_key(cfg_real)[o.key]["theme"] for o in p.buys if o.sleeve == "actions"}
    assert picked and "ia" not in picked
    assert any("thème(s) déjà à 15%" in n and "ia" in n for n in p.notes)


def test_shipped_config_end_to_end_paper(cfg_real, tmp_path):
    hist = uptrend_histories(cfg_real)
    for cur in ("CHF", "DKK"):
        hist[f"EUR{cur}=X"] = hist["EURUSD=X"]
    reports = []
    d = date(2026, 10, 26)
    while len(reports) < 10:
        if d.weekday() < 5:
            reports.append(botmain.run(cfg_real, {}, data=FakeData(hist), today=d,
                                       state_base=tmp_path / "s", journal_base=tmp_path / "j"))
        d = date.fromordinal(d.toordinal() + 1)
    assert not any(r.error for r in reports)
    bought = [(o.sleeve, o.key) for r in reports for o, _ in r.executed]
    assert bought and all(s in ("etf", "actions") for s, _ in bought)
    assert any(s == "actions" for s, _ in bought)
    text = (tmp_path / "j" / "paper" / "DERNIER.md").read_text()
    assert "| Analystes |" in text and "Crypto |" not in text.split("## Climat de marché")[1].split("## Ordres")[0]
