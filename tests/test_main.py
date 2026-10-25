import csv
import json
from datetime import date, timedelta

import pytest

from bot import main as botmain
from bot import news
from bot.strategy import MarketContext

from conftest import FakeClient, FakeData, make_series, uptrend_histories


def run_days(cfg, tmp_path, start, n_days, env=None, data=None, client=None, agent_fetch=None):
    reports = []
    d = start
    while len(reports) < n_days:
        if d.weekday() < 5:
            reports.append(botmain.run(cfg, env or {}, data=data or FakeData(uptrend_histories(cfg)),
                                       client=client, agent_fetch=agent_fetch, today=d,
                                       state_base=tmp_path / "state", journal_base=tmp_path / "journal"))
        d += timedelta(days=1)
    return reports


def test_paper_two_weeks_end_to_end(cfg, tmp_path):
    reports = run_days(cfg, tmp_path, date(2026, 10, 26), 10)
    assert not any(r.error for r in reports)
    state = json.loads((tmp_path / "state" / "paper.json").read_text())
    led = state["ledger"]
    assert led["credited_total"] == 100.0                         # 2 semaines × 50 €
    spent = sum(f.notional for r in reports for o, f in r.executed if o.side == "BUY")
    assert spent <= 100.0 + 1e-6
    assert led["reserve"] == pytest.approx(100.0 - spent, abs=0.02)
    assert spent >= 40.0                                          # au moins les planchers du vendredi
    crypto = sum(f.notional for r in reports for o, f in r.executed if o.sleeve == "crypto")
    assert 0 < crypto < spent * 0.4
    stocks = sum(f.notional for r in reports for o, f in r.executed if o.sleeve == "actions")
    assert stocks > 0
    journal = tmp_path / "journal" / "paper"
    assert (journal / "2026-11-06.md").exists() and (journal / "DERNIER.md").exists()
    rows = list(csv.reader(open(journal / "operations.csv", encoding="utf-8"), delimiter=";"))
    assert rows[0][0] == "date" and len(rows) - 1 == sum(len(r.executed) for r in reports)
    assert "Journal du 06/11/2026" in (journal / "DERNIER.md").read_text()


def test_live_requires_explicit_switch(cfg, tmp_path):
    cfg["mode"] = "live"
    r = run_days(cfg, tmp_path, date(2026, 10, 26), 1, env={"BITPANDA_API_KEY": "k"})[0]
    assert r.mode == "paper" and "LIVE_TRADING" in r.notes[0]


def test_live_refuses_without_bitpanda_limit(cfg, tmp_path):
    cfg["mode"] = "live"
    client = FakeClient(cfg, {}, limits=False)
    r = run_days(cfg, tmp_path, date(2026, 10, 30), 1,
                 env={"BITPANDA_API_KEY": "k", "LIVE_TRADING": "true"}, client=client)[0]
    assert r.mode == "live" and r.error and "plafond" in r.error
    assert not client.accepted


def test_live_run_buys_and_never_sells_user_holdings(cfg, tmp_path):
    cfg["mode"] = "live"
    hist = uptrend_histories(cfg)
    # ETH en forte baisse : le bot ne doit pas toucher l'ETH que tu possédais déjà
    eth = next(a for a in cfg["universe"] if a["key"] == "ETH")
    hist[eth["yahoo"]] = make_series(daily=0.002, tail=[-0.015] * 30)
    data = FakeData(hist)
    prices = {a["key"]: float(hist[a["yahoo"]].iloc[-1]) for a in cfg["universe"]}
    client = FakeClient(cfg, prices)
    client.balances["id-ETH"] = 3.0
    reports = run_days(cfg, tmp_path, date(2026, 10, 26), 5,
                       env={"BITPANDA_API_KEY": "k", "LIVE_TRADING": "true"}, data=data, client=client)
    assert not any(r.error for r in reports)
    assert all(b["side"] == "BUY" for b in client.accepted) and client.accepted
    assert client.balances["id-ETH"] == pytest.approx(3.0)
    state = json.loads((tmp_path / "state" / "live.json").read_text())
    assert "ETH" not in state["positions_meta"]


def test_agent_view_is_applied_and_failure_is_harmless(cfg, tmp_path):
    cfg["agent"]["enabled"] = True
    view = {"bias": {"etf": 0.5, "crypto": -0.5}, "veto": {"NASDAQ100"}, "summary": "Résumé test.",
            "sources": [{"title": "Source", "url": "https://example.org"}]}
    r = run_days(cfg, tmp_path, date(2026, 10, 27), 1, env={"ANTHROPIC_API_KEY": "x"},
                 agent_fetch=lambda *a: view)[0]
    assert r.agent_summary == "Résumé test." and not any(o.key == "NASDAQ100" for o, _ in r.executed)

    def boom(*a):
        raise RuntimeError("API en panne")

    r = run_days(cfg, tmp_path / "b", date(2026, 10, 27), 1, env={"ANTHROPIC_API_KEY": "x"}, agent_fetch=boom)[0]
    assert not r.error and any("veille" in n for n in r.notes)


def test_pause_switch(cfg, tmp_path):
    cfg["enabled"] = False
    r = run_days(cfg, tmp_path, date(2026, 10, 30), 1)[0]
    assert not r.executed and "pause" in r.notes[0]


def test_check_and_set_limit(cfg, tmp_path):
    hist = uptrend_histories(cfg)
    prices = {a["key"]: float(hist[a["yahoo"]].iloc[-1]) for a in cfg["universe"]}
    client = FakeClient(cfg, prices, limits=False)
    code = botmain.check(cfg, {"BITPANDA_API_KEY": "k"}, client=client, data=FakeData(hist), journal_base=tmp_path)
    text = (tmp_path / "VERIFICATION.md").read_text()
    assert code == 0 and "Connexion à l'API Bitpanda réussie" in text and "Aucun plafond" in text
    assert botmain.set_limit(cfg, {"BITPANDA_API_KEY": "k"}, client=client) == 0
    assert client.limit_calls[0][0] == 55  # 50 € × 1,1 arrondi


def test_news_parsing_and_pause_turn(cfg):
    text = 'Voici mon analyse.\n{"etf_bias": 3, "crypto_bias": -0.3, "veto": ["BTC", "FAKE"], "summary": "ok", ' \
           '"sources": [{"title": "t", "url": "https://a.b"}, {"url": "javascript:x"}]}'
    view = news.parse_view(news._extract_json(text), {"BTC", "ETH"})
    assert view["bias"] == {"etf": 1.0, "actions": 1.0, "crypto": -0.3} and view["veto"] == {"BTC"} and len(view["sources"]) == 1

    class Sess:
        def __init__(self):
            self.n = 0
            self.bodies = []

        def post(self, url, headers, json, timeout):
            self.n += 1
            self.bodies.append(json)
            payload = ({"stop_reason": "pause_turn", "content": [{"type": "server_tool_use", "id": "s1"}]}
                       if self.n == 1 else {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]})

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return payload
            return R()

    sess = Sess()
    out = news.fetch_view("key", cfg, {}, date(2026, 10, 27), session=sess)
    assert sess.n == 2 and out["bias"]["crypto"] == -0.3
    assert sess.bodies[0]["tools"][0]["type"] == "web_search_20260318"
    assert sess.bodies[1]["messages"][-1]["role"] == "assistant"
    m = MarketContext()
    news.apply_view(m, out)
    assert m.veto == {"BTC"}


def test_paper_run_counts_whole_account(cfg, tmp_path):
    hist = uptrend_histories(cfg)
    prices = {a["key"]: float(hist[a["yahoo"]].iloc[-1]) for a in cfg["universe"]}
    client = FakeClient(cfg, prices)
    client.extra_rows = [  # compte existant : beaucoup de crypto et d'actions, peu d'ETF
        {"asset_id": "id-BTC", "balance": {"value": "1"}, "currency_balance": {"value": "1100"}},
        {"asset_id": "id-MSFT", "balance": {"value": "1"}, "currency_balance": {"value": "1800"}},
        {"asset_id": "id-MSCI_WORLD", "balance": {"value": "1"}, "currency_balance": {"value": "400"}},
    ]
    reports = run_days(cfg, tmp_path, date(2026, 10, 26), 5, env={"BITPANDA_API_KEY": "k"},
                       data=FakeData(hist), client=client)
    assert reports[0].mode == "paper" and "hors bot vaut" in reports[0].account_note
    sleeves = {o.sleeve for r in reports for o, _ in r.executed}
    assert sleeves == {"etf"}            # crypto 33 % et actions 55 % : tout l'argent neuf va aux ETF
    assert not client.accepted           # simulation : aucun ordre réel

    cfg["allocation"]["scope"] = "bot"
    reports = run_days(cfg, tmp_path / "bot", date(2026, 10, 26), 5, env={"BITPANDA_API_KEY": "k"},
                       data=FakeData(hist), client=client)
    assert reports[0].account_note is None


def test_second_run_same_day_does_nothing(cfg, tmp_path):
    d = date(2026, 10, 30)
    first = run_days(cfg, tmp_path, d, 1)[0]
    second = run_days(cfg, tmp_path, d, 1)[0]
    assert first.executed and not second.executed and "déjà tourné" in second.notes[-1]


def test_summary_and_history_written(cfg, tmp_path):
    import json
    run_days(cfg, tmp_path, date(2026, 10, 26), 3)
    folder = tmp_path / "journal" / "paper"
    s = json.loads((folder / "resume.json").read_text())
    assert s["date"] == "2026-10-28" and "coefficients" in s and s["valeur_eur"] >= 0
    lines = (folder / "historique.csv").read_text().splitlines()
    assert lines[0].startswith("date;valeur_eur") and len(lines) == 4
