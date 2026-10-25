"""Chargement et validation de config.yaml (+ surcharges par variables d'environnement)."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Ordre de traitement des poches : les actions passent en premier pour que leur
# provision non utilisée puisse être réorientée vers les ETF le jour même.
SLEEVES = ("actions", "crypto", "etf")
SLEEVE_LABELS = {"etf": "ETF", "actions": "Actions", "crypto": "Crypto"}
# Poche dont l'actif socle sert de baromètre du marché pour chaque poche.
MARKET_REFERENCE = {"etf": "etf", "actions": "etf", "crypto": "crypto"}

DEFAULTS: dict = {
    "enabled": True,
    "mode": "paper",
    "budget": {
        "weekly_eur": 50.0,
        "max_reserve_weeks": 4,
        "min_weekly_invest_pct": 0.4,
        "max_daily_pct_of_weekly": 1.0,
        "min_order_eur": 10.0,
    },
    "allocation": {
        "etf": 0.6, "actions": 0.2, "crypto": 0.2,
        "max": {"etf": 1.0, "actions": 0.30, "crypto": 0.25},
        "rebalance_band": 0.05,
        "scope": "account",
    },
    "signals": {
        "sma_short": 50,
        "sma_long": 200,
        "rsi_period": 14,
        "rsi_dip": 35,
        "rsi_hot": 75,
        "momentum_windows": [21, 63, 126],
        "risk_adjusted": ["actions"],
        "analysts": {"enabled": True, "max_recommendation": 2.5, "min_analysts": 5,
                     "min_upside": 0.0, "upside_weight": 0.5},
        "pullback_pct": 0.08,
        "top_n": {"etf": 2, "actions": 2, "crypto": 1},
        "vix_fear": 28,
        "vix_calm": 15,
        "crypto_fear": 25,
        "crypto_greed": 75,
        "max_multiplier": 2.5,
    },
    "exits": {
        "min_hold_days": 14,
        "trailing_stop": {"etf": 0.15, "actions": 0.20, "crypto": 0.25},
        "trend_break_pct": 0.03,
        "take_profit_gain": 0.30,
        "take_profit_rsi": 75,
        "take_profit_trim": 0.25,
        "take_profit_cooldown_days": 30,
    },
    "risk": {
        "max_position_pct": {"etf": 0.40, "actions": 0.08, "crypto": 0.40},
        "max_theme_pct": 0.15,
        "caps_min_portfolio_eur": 300.0,
    },
    "execution": {
        "max_orders_per_run": 6,
        "max_price_deviation": 0.03,
        "max_fee_pct": 0.025,
        "daily_sell_limit_eur": 2000.0,
        "paper_fee_pct": {"etf": 0.005, "actions": 0.005, "crypto": 0.015},
    },
    "agent": {"enabled": True, "model": "claude-haiku-5-5", "max_searches": 5, "weight": 0.2},
    "universe": [],
}


class ConfigError(ValueError):
    pass


def _merge(base: dict, override: dict) -> dict:
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _to_float(text: str, name: str) -> float:
    try:
        return float(str(text).strip().replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un nombre (reçu : {text!r})") from exc


def load_config(path: str | Path | None = None, env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = _merge(copy.deepcopy(DEFAULTS), raw)

    weekly = (env.get("WEEKLY_BUDGET_EUR") or "").strip()
    if weekly:
        cfg["budget"]["weekly_eur"] = _to_float(weekly, "WEEKLY_BUDGET_EUR")

    validate(cfg)
    return cfg


def sleeve_value(table, sleeve: str, default: float = 0.0) -> float:
    """Lit un réglage donné soit par poche ({etf: x, …}), soit sous forme d'une valeur unique."""
    if isinstance(table, dict):
        return float(table.get(sleeve, default))
    return float(table)


def validate(cfg: dict) -> None:
    b = cfg["budget"]
    if not 0 <= float(b["weekly_eur"]) <= 5000:
        raise ConfigError("budget.weekly_eur doit être compris entre 0 et 5000 €")
    if float(b["min_order_eur"]) < 1:
        raise ConfigError("budget.min_order_eur doit valoir au moins 1 €")
    if cfg["mode"] not in ("paper", "live"):
        raise ConfigError("mode doit valoir 'paper' ou 'live'")

    alloc = cfg["allocation"]
    if alloc.get("scope", "account") not in ("account", "bot"):
        raise ConfigError("allocation.scope doit valoir 'account' ou 'bot'")
    total = sum(float(alloc.get(s, 0)) for s in SLEEVES)
    if abs(total - 1.0) > 0.01:
        raise ConfigError(f"allocation : etf + actions + crypto doit faire 1 (actuellement {total:g})")
    for s in SLEEVES:
        if float(alloc.get(s, 0)) < 0:
            raise ConfigError(f"allocation.{s} ne peut pas être négatif")
        if sleeve_value(alloc["max"], s, 1.0) < float(alloc.get(s, 0)):
            raise ConfigError(f"allocation.max.{s} doit être ≥ allocation.{s}")

    keys = set()
    for asset in cfg["universe"]:
        key = asset.get("key")
        if not key:
            raise ConfigError(f"actif sans 'key' dans l'univers : {asset}")
        if key in keys:
            raise ConfigError(f"clé d'actif en double : {key}")
        keys.add(key)
        if asset.get("sleeve") not in SLEEVES:
            raise ConfigError(f"{key} : sleeve doit valoir 'etf', 'actions' ou 'crypto'")
        if not asset.get("yahoo"):
            raise ConfigError(f"{key} : code 'yahoo' manquant (historique des cours)")
        if asset["sleeve"] != "crypto" and not asset.get("isin"):
            raise ConfigError(f"{key} : un ETF ou une action doit avoir un 'isin'")
        if asset["sleeve"] == "crypto" and not asset.get("symbol"):
            raise ConfigError(f"{key} : une crypto doit avoir un 'symbol' (ex. BTC)")
        if not re.fullmatch(r"[A-Z]{3}", str(asset.get("currency", "EUR")).upper()):
            raise ConfigError(f"{key} : 'currency' doit être un code devise de 3 lettres (ex. USD)")
        if asset.get("core") and asset["sleeve"] == "actions":
            raise ConfigError(f"{key} : une action seule ne peut pas être un socle (core)")
    for sleeve in SLEEVES:
        if float(alloc.get(sleeve, 0)) > 0 and not any(a["sleeve"] == sleeve for a in cfg["universe"]):
            raise ConfigError(f"aucun actif '{sleeve}' dans l'univers alors que son allocation est > 0")


def assets_by_key(cfg: dict) -> dict[str, dict]:
    return {a["key"]: a for a in cfg["universe"]}


def core_keys(cfg: dict) -> set[str]:
    return {a["key"] for a in cfg["universe"] if a.get("core")}
