"""Point d'entrée : `python -m bot.main [run|check|set-limit]`.

run        exécution quotidienne (par défaut)
check      vérifie la connexion Bitpanda, les actifs et les prix, sans rien acheter
set-limit  pose le plafond journalier d'achat/vente sur la clé API Bitpanda
analyse    analyse tout ton portefeuille Bitpanda (lecture seule) → journal/PORTEFEUILLE.md
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import news, notify
from .broker import BitpandaClient, BitpandaError, LiveBroker, PaperBroker, UncertainExecution
from .budget import Ledger
from .config import ROOT, ConfigError, assets_by_key, load_config
from .data import MarketData
from .indicators import compute_signal
from .report import JOURNAL_DIR, RunReport, append_trades, eur, write_journal
from .state import load_state, save_state
from .strategy import Holding, MarketContext, build_plan

log = logging.getLogger("bot")


def today_paris() -> date:
    override = os.environ.get("BOT_TODAY", "").strip()
    if override:
        return date.fromisoformat(override)
    return datetime.now(ZoneInfo("Europe/Paris")).date()


def resolve_mode(cfg: dict, env: dict) -> tuple[str, list[str]]:
    if cfg["mode"] != "live":
        return "paper", []
    if str(env.get("LIVE_TRADING", "")).strip().lower() != "true":
        return "paper", ["config.yaml est en mode live mais la variable GitHub LIVE_TRADING n'est pas à true : "
                         "le bot reste en simulation"]
    if not env.get("BITPANDA_API_KEY"):
        return "paper", ["secret BITPANDA_API_KEY absent : le bot reste en simulation"]
    return "live", []


# ---------------------------------------------------------------------------
# Exécution quotidienne
# ---------------------------------------------------------------------------

def _update_meta_buy(meta: dict, key: str, fill, today: date) -> None:
    m = meta.setdefault(key, {})
    old_qty = float(m.get("qty", 0.0))
    new_qty = old_qty + fill.quantity
    old_cost = old_qty * float(m.get("avg_cost") or 0.0)
    m["qty"] = new_qty
    m["avg_cost"] = (old_cost + fill.notional) / new_qty if new_qty > 0 else None
    if old_qty <= 0 or not m.get("first_buy"):
        m["first_buy"] = today.isoformat()
    m["peak"] = max(float(m.get("peak") or 0.0), fill.price)


def _update_meta_sell(meta: dict, key: str, fill, today: date) -> None:
    m = meta.get(key, {})
    m["qty"] = float(m.get("qty", 0.0)) - fill.quantity
    if m["qty"] <= 1e-10:
        meta.pop(key, None)
    else:
        m["last_trim"] = today.isoformat()


def _execute(cfg, env, report, state, data, client, agent_fetch, today):
    assets = assets_by_key(cfg)
    names = {k: a["name"] for k, a in assets.items()}
    b = cfg["budget"]

    if not cfg.get("enabled", True) or (ROOT / "PAUSE").exists():
        report.notes.append("bot en pause (enabled: false ou fichier PAUSE présent) : aucun ordre")
        return

    ledger = Ledger(state.setdefault("ledger", {}))
    report.ledger_note = ledger.roll(today, float(b["weekly_eur"]), float(b["max_reserve_weeks"]))

    # 1) Sources de marché du jour
    data = data or MarketData()
    signals = {}
    for a in cfg["universe"]:
        sig = compute_signal(a["key"], a["sleeve"], data.history_eur(a["yahoo"], a.get("currency", "EUR")),
                             cfg["signals"])
        if sig:
            signals[a["key"]] = sig
        else:
            report.notes.append(f"{a['name']} : historique de cours indisponible ou trop court, ignoré aujourd'hui")
    market = MarketContext(
        vix=data.vix() if any(a["sleeve"] in ("etf", "actions") for a in cfg["universe"]) else None,
        crypto_fng=data.crypto_fear_greed() if any(a["sleeve"] == "crypto" for a in cfg["universe"]) else None,
    )
    report.vix, report.fng = market.vix, market.crypto_fng
    report.signals = list(signals.values())

    # 2) Courtier
    if report.mode == "live":
        client = client or BitpandaClient(env["BITPANDA_API_KEY"])
        limits = client.trading_limits()
        if not limits or limits.get("buy_limit") is None:
            raise RuntimeError("aucun plafond journalier n'est posé sur la clé API Bitpanda. "
                               "Lance l'action GitHub avec la commande « set-limit » avant de trader en réel.")
        broker = LiveBroker(client, cfg, state)
    else:
        broker = PaperBroker(cfg, state)
    for key, ref in broker.resolve(cfg["universe"]).items():
        if not ref.asset_id:
            report.notes.append(f"{names[key]} : {ref.note}")
        elif not ref.buy_active:
            report.notes.append(f"{names[key]} : actuellement non achetable sur Bitpanda")

    # 3) Positions gérées par le bot (jamais tes autres avoirs Bitpanda)
    meta = state.setdefault("positions_meta", {})
    live_holdings = broker.holdings() if report.mode == "live" else None
    holdings = {}
    for key, m in list(meta.items()):
        qty = float(m.get("qty", 0.0))
        if live_holdings is not None:
            actual = live_holdings.get(key).quantity if key in live_holdings else 0.0
            if actual < qty * 0.999:
                report.notes.append(f"{names.get(key, key)} : solde Bitpanda ({actual:.6f}) inférieur à la "
                                    f"position du bot ({qty:.6f}), le bot s'aligne sur le solde réel")
                qty = actual
                m["qty"] = actual
        if qty > 0:
            holdings[key] = Holding(quantity=qty, avg_price=m.get("avg_cost"))
            if key in signals:
                m["peak"] = max(float(m.get("peak") or 0.0), signals[key].price)
    cash = broker.cash_eur()

    # 4) Veille d'actualité (optionnelle)
    api_key = env.get("ANTHROPIC_API_KEY", "").strip()
    if cfg["agent"].get("enabled") and api_key and signals:
        try:
            view = (agent_fetch or news.fetch_view)(api_key, cfg, signals, today)
            news.apply_view(market, view)
            report.agent_summary, report.agent_sources = market.agent_summary, market.agent_sources
            if market.veto:
                report.notes.append("achats suspendus aujourd'hui par la veille : "
                                    + ", ".join(names.get(k, k) for k in sorted(market.veto)))
        except Exception as exc:
            report.notes.append(f"veille d'actualité indisponible aujourd'hui ({type(exc).__name__})")

    # 5) Plan du jour
    plan = build_plan(today, cfg, assets, signals, holdings, meta, ledger.reserve, ledger.spent_week,
                      state.get("pending", {}), market, broker.tradeable, cash)
    report.multipliers, report.reasons = plan.multipliers, plan.reasons
    report.notes.extend(plan.notes)
    pending = dict(plan.pending)

    # 6) Exécution : ventes d'abord, puis achats
    halted = False
    for order in plan.sells:
        try:
            fill = broker.sell(order.key, order.quantity, signals[order.key].price)
        except UncertainExecution as exc:
            report.failed.append((order, str(exc)))
            halted = True
            break
        except BitpandaError as exc:
            report.failed.append((order, str(exc)))
            continue
        ledger.add_proceeds(fill.notional)
        _update_meta_sell(meta, order.key, fill, today)
        report.executed.append((order, fill))

    for order in ([] if halted else plan.buys):
        if order.amount_eur > ledger.reserve + 0.01:
            report.failed.append((order, "réserve insuffisante"))
            continue
        try:
            fill = broker.buy(order.key, order.amount_eur, signals[order.key].price)
        except UncertainExecution as exc:
            report.failed.append((order, str(exc)))
            halted = True
            break
        except BitpandaError as exc:
            report.failed.append((order, str(exc)))
            pending[order.sleeve] = round(pending.get(order.sleeve, 0.0) + order.amount_eur, 2)
            continue
        ledger.spend(fill.notional or order.amount_eur)
        _update_meta_buy(meta, order.key, fill, today)
        report.executed.append((order, fill))
        if fill.kid_links:
            report.kid_links[order.key] = fill.kid_links
        for w in fill.warnings:
            report.notes.append(f"{names[order.key]} — avertissement Bitpanda : {w}")
    if halted:
        report.notes.append("⚠️ exécution d'un ordre non confirmée : le bot s'est arrêté pour aujourd'hui. "
                            "Vérifie ton compte Bitpanda.")
        notify.push("Bitpanda Pilote : vérification nécessaire",
                    "Un ordre n'a pas pu être confirmé. Vérifie ton compte Bitpanda.", priority=5)

    state["pending"] = {s: round(min(v, max(ledger.reserve, 0.0)), 2) for s, v in pending.items()}
    report.reserve, report.spent_week, report.pending = ledger.reserve, ledger.spent_week, state["pending"]

    # 7) Photo du portefeuille du bot
    for key, m in meta.items():
        qty = float(m.get("qty", 0.0))
        if qty <= 0:
            continue
        price = signals[key].price if key in signals else None
        avg = m.get("avg_cost")
        report.positions.append({
            "name": names.get(key, key), "qty": qty, "price": price,
            "value": qty * price if price else 0.0, "avg": avg, "cost": qty * avg if avg else 0.0,
        })


def run(cfg: dict, env: dict | None = None, *, data=None, client=None, agent_fetch=None,
        today: date | None = None, state_base: Path | None = None, journal_base: Path | None = None) -> RunReport:
    env = os.environ if env is None else env
    today = today or today_paris()
    mode, mode_notes = resolve_mode(cfg, env)
    state = load_state(mode, state_base)
    report = RunReport(today=today, mode=mode, weekly=float(cfg["budget"]["weekly_eur"]), notes=list(mode_notes))
    assets = assets_by_key(cfg)
    try:
        _execute(cfg, env, report, state, data, client, agent_fetch, today)
    except Exception as exc:
        log.exception("Échec de l'exécution")
        report.error = f"{type(exc).__name__} : {exc}"
    finally:
        state["last_run"] = today.isoformat()
        save_state(state, mode, state_base)
        names = {k: a["name"] for k, a in assets.items()}
        path = write_journal(report, names, journal_base)
        append_trades(report, assets, journal_base)
        _github_summary(path.read_text(encoding="utf-8"))
        _notify(report)
    return report


def _notify(report: RunReport) -> None:
    if not (report.executed or report.failed or report.error):
        return
    label = "réel" if report.mode == "live" else "simulation"
    lines = [report.headline()]
    for order, fill in report.executed:
        lines.append(f"{'+' if order.side == 'BUY' else '−'} {eur(fill.notional)} {order.key} — {order.reason}")
    for order, msg in report.failed:
        lines.append(f"✗ {order.key} : {msg}")
    if report.error:
        lines.append(f"Erreur : {report.error}")
    lines.append(f"Réserve : {eur(report.reserve)}")
    notify.push(f"Bitpanda Pilote ({label})", "\n".join(lines), priority=4 if report.error else 3)


def _github_summary(markdown: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


# ---------------------------------------------------------------------------
# Vérification et plafond Bitpanda
# ---------------------------------------------------------------------------

def check(cfg: dict, env: dict, *, client=None, data=None, journal_base: Path | None = None) -> int:
    key = env.get("BITPANDA_API_KEY", "").strip()
    out = ["# Vérification Bitpanda", ""]
    if not key and client is None:
        out.append("❌ Secret BITPANDA_API_KEY absent : ajoute-le dans Settings → Secrets and variables → Actions.")
        return _finish_check(out, journal_base, 1)
    client = client or BitpandaClient(key)
    broker = LiveBroker(client, cfg, {})
    code = 0
    try:
        eur_id = broker.eur_currency_id()
        out.append("✅ Connexion à l'API Bitpanda réussie (devise EUR trouvée).")
    except BitpandaError as exc:
        out.append(f"❌ Connexion impossible : {exc}")
        return _finish_check(out, journal_base, 1)

    data = data or MarketData()
    refs = broker.resolve(cfg["universe"])
    out += ["", "| Actif | Sur Bitpanda | Achetable | Prix Bitpanda | Prix de référence | Écart |",
            "|---|---|:---:|---:|---:|---:|"]
    max_dev = float(cfg["execution"]["max_price_deviation"])
    for a in cfg["universe"]:
        ref = refs[a["key"]]
        if not ref.asset_id:
            out.append(f"| {a['name']} | ❌ {ref.note} | | | | |")
            continue
        bp_price, yahoo_price, gap = None, None, ""
        try:
            t = client.ticker(ref.asset_id)
            if t.get("currency_id") in (None, eur_id):
                bp_price = float(t.get("price"))
        except (BitpandaError, TypeError, ValueError):
            pass
        hist = data.history_eur(a["yahoo"], a.get("currency", "EUR"))
        if hist is not None and len(hist):
            yahoo_price = float(hist.iloc[-1])
        if bp_price and yahoo_price:
            dev = bp_price / yahoo_price - 1
            gap = f"{dev:+.1%}".replace(".", ",") + (" ⚠️" if abs(dev) > max_dev else " ✅")
        out.append(f"| {a['name']} | ✅ {ref.name} | {'✅' if ref.buy_active else '❌'} | "
                   f"{eur(bp_price) if bp_price else '—'} | {eur(yahoo_price) if yahoo_price else '—'} | {gap} |")

    try:
        rows = client.portfolio(eur_id)
        cash = broker.cash_eur()
        out += ["", f"Portefeuille lu : {len(rows)} ligne(s). Solde EUR détecté : "
                    f"{eur(cash) if cash is not None else 'non détecté (le bot laissera Bitpanda refuser un ordre sans fonds)'}."]
    except BitpandaError as exc:
        out += ["", f"❌ Lecture du portefeuille impossible : {exc} (la clé a-t-elle le droit « Balances » ?)"]
        code = 1
    try:
        limits = client.trading_limits()
        if limits:
            out.append(f"✅ Plafond journalier posé : achats {limits.get('buy_limit')} / ventes {limits.get('sell_limit')} "
                       f"(reste aujourd'hui : {limits.get('buy_budget_remaining')} / {limits.get('sell_budget_remaining')}).")
        else:
            out.append("⚠️ Aucun plafond journalier sur la clé : lance la commande « set-limit » avant de passer en réel.")
    except BitpandaError as exc:
        out.append(f"⚠️ Plafonds illisibles : {exc}")
    out += ["", "Les ETF marqués ❌ seront simplement ignorés. Un écart ⚠️ signale un code Yahoo à corriger dans config.yaml."]
    return _finish_check(out, journal_base, code)


def _finish_check(lines: list[str], journal_base: Path | None, code: int) -> int:
    text = "\n".join(lines) + "\n"
    folder = journal_base or JOURNAL_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "VERIFICATION.md").write_text(text, encoding="utf-8")
    print(text)
    _github_summary(text)
    return code


def set_limit(cfg: dict, env: dict, *, client=None) -> int:
    key = env.get("BITPANDA_API_KEY", "").strip()
    if not key and client is None:
        print("Secret BITPANDA_API_KEY absent.")
        return 1
    client = client or BitpandaClient(key)
    b = cfg["budget"]
    default_buy = math.ceil(round(float(b["weekly_eur"]) * float(b["max_daily_pct_of_weekly"]) * 1.1, 6))
    buy_limit = float(env.get("DAILY_BUY_LIMIT_EUR") or default_buy)
    sell_limit = float(cfg["execution"]["daily_sell_limit_eur"])
    broker = LiveBroker(client, cfg, {})
    try:
        res = client.set_trading_limits(buy_limit, sell_limit, broker.eur_currency_id())
    except BitpandaError as exc:
        msg = f"Impossible de poser le plafond : {exc}"
        if exc.status == 409:
            msg += " (un plafond existe peut-être déjà : vérifie avec la commande « check »)"
        print(msg)
        _github_summary(msg)
        return 1
    msg = (f"✅ Plafond journalier posé sur la clé API : achats {res.get('buy_limit', buy_limit)} €, "
           f"ventes {res.get('sell_limit', sell_limit)} €.")
    print(msg)
    _github_summary(msg)
    return 0


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s : %(message)s")
    parser = argparse.ArgumentParser(prog="bot", description="Bitpanda Pilote")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check", "set-limit", "analyse"])
    args = parser.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Erreur dans config.yaml : {exc}")
        return 2
    env = dict(os.environ)
    if args.command == "check":
        return check(cfg, env)
    if args.command == "set-limit":
        return set_limit(cfg, env)
    if args.command == "analyse":
        from .portfolio import analyse
        return analyse(cfg, env)
    report = run(cfg, env)
    print(report.headline())
    return 1 if report.error else 0


if __name__ == "__main__":
    sys.exit(main())
