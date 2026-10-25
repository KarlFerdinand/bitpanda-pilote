"""Exécution des ordres : Bitpanda (réel, via devis « quote » puis acceptation) ou simulation.

API publique Bitpanda : https://docs.public.bitpanda.com
  POST /v1/quotes               crée un devis (BUY/SELL, montant en € ou quantité)
  POST /v1/quotes/{id}/accept   exécute le devis
  GET  /v1/portfolio            soldes
  GET  /v1/assets               actifs (filtre isin / symbol / type)
  GET  /v1/currencies           devises (id de l'EUR)
  GET  /v1/tickers/{assetId}    dernier prix
  GET/POST /v1/trading-limits   plafonds journaliers de la clé API
"""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.public.bitpanda.com"

# Formats imposés par l'API (cf. schéma OpenAPI de POST /v1/quotes)
NOTIONAL_RE = re.compile(r"^([1-9]\d*(\.\d{1,2})?|0\.\d{0,1}[1-9])$")
QUANTITY_RE = re.compile(r"^([1-9]\d*(\.\d{1,8})?|0\.\d{0,7}[1-9])$")


def format_notional(amount: float) -> str:
    value = math.floor(round(amount * 100, 6)) / 100  # arrondi à l'inférieur au centime
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    if not NOTIONAL_RE.match(text):
        raise ValueError(f"montant invalide pour Bitpanda : {amount}")
    return text


def format_quantity(qty: float) -> str:
    value = math.floor(round(qty * 1e8, 4)) / 1e8  # arrondi à l'inférieur à 8 décimales
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    if not QUANTITY_RE.match(text):
        raise ValueError(f"quantité invalide pour Bitpanda : {qty}")
    return text


class BitpandaError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class UncertainExecution(BitpandaError):
    """L'acceptation d'un devis n'a pas pu être confirmée (coupure réseau) : on arrête tout."""


@dataclass
class AssetRef:
    key: str
    asset_id: str | None
    name: str = ""
    buy_active: bool = False
    sell_active: bool = False
    note: str = ""


@dataclass
class Fill:
    side: str
    key: str
    quantity: float
    price: float
    notional: float
    fee: float
    trade_id: str = ""
    kid_links: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class BitpandaClient:
    def __init__(self, api_key: str, base_url: str = API_BASE, session: requests.Session | None = None,
                 sleep=time.sleep):
        self.api_key = api_key
        self.base = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.sleep = sleep

    def _request(self, method: str, path: str, *, params=None, json=None, retry: bool = True) -> dict:
        url = f"{self.base}{path}"
        headers = {"x-api-key": self.api_key, "Accept": "application/json"}
        attempts = 4 if retry else 1
        for attempt in range(attempts):
            try:
                r = self.session.request(method, url, params=params, json=json, headers=headers, timeout=20)
            except requests.RequestException as exc:
                if attempt == attempts - 1:
                    raise BitpandaError(f"réseau : {exc}") from exc
                self.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or (r.status_code >= 500 and retry):
                if attempt < attempts - 1:
                    self.sleep(2 ** attempt + 1)
                    continue
            if r.status_code >= 400:
                code = None
                try:
                    code = (r.json().get("error") or {}).get("code")
                except ValueError:
                    pass
                raise BitpandaError(f"{method} {path} → HTTP {r.status_code} ({code or r.text[:120]})",
                                    status=r.status_code, code=code)
            return r.json() if r.content else {}
        raise BitpandaError(f"{method} {path} : échec après {attempts} tentatives")

    # --- lecture -----------------------------------------------------------
    def currencies(self) -> list[dict]:
        return self._request("GET", "/v1/currencies").get("data", [])

    def assets(self, **filters) -> list[dict]:
        out, cursor = [], None
        for _ in range(10):
            params = {k: v for k, v in filters.items() if v}
            params["page_size"] = 50
            if cursor:
                params["cursor"] = cursor
            page = self._request("GET", "/v1/assets", params=params)
            out.extend(page.get("data", []))
            if not page.get("has_next_page"):
                break
            cursor = page.get("next_cursor")
        return out

    def ticker(self, asset_id: str) -> dict:
        return self._request("GET", f"/v1/tickers/{asset_id}").get("data", {})

    def portfolio(self, equivalent_currency_id: str | None = None) -> list[dict]:
        params = {"equivalent_currency_id": equivalent_currency_id} if equivalent_currency_id else None
        return self._request("GET", "/v1/portfolio", params=params).get("data", [])

    def trading_limits(self) -> dict | None:
        try:
            return self._request("GET", "/v1/trading-limits").get("data")
        except BitpandaError as exc:
            if exc.status == 404:
                return None
            raise

    # --- écriture ----------------------------------------------------------
    def set_trading_limits(self, buy_limit: float, sell_limit: float, currency_id: str) -> dict:
        body = {"buy_limit": buy_limit, "sell_limit": sell_limit, "currency_id": currency_id}
        return self._request("POST", "/v1/trading-limits", json=body, retry=False).get("data", {})

    def create_quote(self, body: dict) -> dict:
        return self._request("POST", "/v1/quotes", json=body).get("data", {})

    def accept_quote(self, quote_id: str) -> dict:
        try:
            return self._request("POST", f"/v1/quotes/{quote_id}/accept", retry=False).get("data", {})
        except BitpandaError as exc:
            if exc.status is None or exc.status >= 500:
                raise UncertainExecution(f"acceptation du devis {quote_id} non confirmée : {exc}") from exc
            raise


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class LiveBroker:
    """Ordres réels. Chaque devis est contrôlé (prix, frais, expiration) avant acceptation."""

    def __init__(self, client: BitpandaClient, cfg: dict, state: dict, now=lambda: datetime.now(timezone.utc)):
        self.client = client
        self.cfg = cfg
        self.state = state
        self.now = now
        self.eur_id: str | None = None
        self.refs: dict[str, AssetRef] = {}
        self._rows: list[dict] | None = None

    # --- préparation -------------------------------------------------------
    def eur_currency_id(self) -> str:
        if not self.eur_id:
            for c in self.client.currencies():
                if str(c.get("symbol", "")).upper() == "EUR":
                    self.eur_id = c["id"]
                    break
        if not self.eur_id:
            raise BitpandaError("devise EUR introuvable dans /v1/currencies")
        return self.eur_id

    def resolve(self, universe: list[dict]) -> dict[str, AssetRef]:
        for asset in universe:
            key = asset["key"]
            try:
                if asset.get("isin"):
                    rows = self.client.assets(isin=asset["isin"])
                else:
                    rows = self.client.assets(symbol=asset["symbol"], type="CRYPTO")
                    rows = [r for r in rows if str(r.get("symbol", "")).upper() == asset["symbol"].upper()] or rows
            except BitpandaError as exc:
                self.refs[key] = AssetRef(key, None, note=f"recherche impossible : {exc}")
                continue
            if not rows:
                self.refs[key] = AssetRef(key, None, note="introuvable sur Bitpanda")
                continue
            row = next((r for r in rows if r.get("buy_active")), rows[0])
            self.refs[key] = AssetRef(key, row.get("id"), name=row.get("name", ""),
                                      buy_active=bool(row.get("buy_active")),
                                      sell_active=bool(row.get("sell_active")))
            self.state.setdefault("asset_ids", {})[key] = row.get("id")
        return self.refs

    def tradeable(self, key: str, side: str = "BUY") -> bool:
        ref = self.refs.get(key)
        if not ref or not ref.asset_id:
            return False
        return ref.buy_active if side == "BUY" else ref.sell_active

    def _portfolio_rows(self) -> list[dict]:
        if self._rows is None:
            self._rows = self.client.portfolio(self.eur_currency_id())
        return self._rows

    def holdings(self) -> dict:
        from .strategy import Holding

        by_id = {r.asset_id: k for k, r in self.refs.items() if r.asset_id}
        out = {}
        for row in self._portfolio_rows():
            key = by_id.get(row.get("asset_id"))
            if not key:
                continue
            qty = _num((row.get("balance") or row.get("available_balance") or {}).get("value"))
            if qty <= 0:
                continue
            avg = _num((row.get("average_buy_price") or {}).get("value"), 0.0) or None
            out[key] = Holding(quantity=qty, avg_price=avg)
        return out

    def cash_eur(self) -> float | None:
        eur = self.eur_currency_id()
        for row in self._portfolio_rows():
            if row.get("asset_id") == eur:
                bal = row.get("available_balance") or row.get("balance") or {}
                return _num(bal.get("value"))
        return None  # format inconnu : Bitpanda refusera l'ordre si le solde manque

    def refresh(self) -> None:
        self._rows = None

    # --- ordres ------------------------------------------------------------
    def _check_quote(self, quote: dict, ref_price: float, notional_hint: float) -> list[str]:
        ex = self.cfg["execution"]
        q = quote.get("quote") or {}
        price = _num(q.get("price"))
        fee = _num(q.get("fee"))
        notional = _num(q.get("notional")) or notional_hint
        problems = []
        if price <= 0:
            problems.append("prix absent du devis")
        elif ref_price > 0 and abs(price / ref_price - 1) > float(ex["max_price_deviation"]):
            problems.append(f"prix Bitpanda {price:.4f} € trop éloigné du prix de référence {ref_price:.4f} €")
        if notional > 0 and fee / notional > float(ex["max_fee_pct"]):
            problems.append(f"frais de {fee / notional:.2%} au-dessus du maximum autorisé")
        total_costs = ((quote.get("ex_ante") or {}).get("costs_and_charges") or {}).get("total_costs") or {}
        pct = _num(total_costs.get("investment_percent"), 0.0)
        if pct and pct / 100 > float(ex["max_fee_pct"]):
            problems.append(f"coûts annoncés de {pct:.2f} % au-dessus du maximum autorisé")
        expires = q.get("expires_at")
        if expires:
            try:
                exp = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                if exp <= self.now():
                    problems.append("devis expiré")
            except ValueError:
                pass
        return problems

    def _execute(self, body: dict, key: str, side: str, ref_price: float, notional_hint: float) -> Fill:
        quote = self.client.create_quote(body)
        problems = self._check_quote(quote, ref_price, notional_hint)
        if problems == ["devis expiré"]:
            quote = self.client.create_quote(body)
            problems = self._check_quote(quote, ref_price, notional_hint)
        if problems:
            raise BitpandaError("devis refusé par le bot : " + " ; ".join(problems))
        kid = [d.get("url") for d in quote.get("key_information_documents") or [] if d.get("url")]
        warnings = [w.get("message") or w.get("code") for w in quote.get("warnings") or []]
        done = self.client.accept_quote(quote["quote_id"])
        ex = done.get("execution") or {}
        self.refresh()
        return Fill(side=side, key=key, quantity=_num(ex.get("quantity")), price=_num(ex.get("price")),
                    notional=_num(ex.get("notional")), fee=_num(ex.get("fee")),
                    trade_id=done.get("trade_id", ""), kid_links=kid, warnings=warnings)

    def buy(self, key: str, amount_eur: float, ref_price: float) -> Fill:
        body = {"asset_id": self.refs[key].asset_id, "currency_id": self.eur_currency_id(),
                "side": "BUY", "notional": format_notional(amount_eur)}
        return self._execute(body, key, "BUY", ref_price, amount_eur)

    def sell(self, key: str, quantity: float, ref_price: float) -> Fill:
        body = {"asset_id": self.refs[key].asset_id, "currency_id": self.eur_currency_id(),
                "side": "SELL", "quantity": format_quantity(quantity)}
        return self._execute(body, key, "SELL", ref_price, quantity * ref_price)


class PaperBroker:
    """Simulation : exécute au prix de référence, frais estimés, portefeuille fictif dans l'état."""

    def __init__(self, cfg: dict, state: dict):
        self.cfg = cfg
        self.book = state.setdefault("paper", {}).setdefault("positions", {})
        self.assets = {a["key"]: a for a in cfg["universe"]}

    def resolve(self, universe: list[dict]) -> dict[str, AssetRef]:
        return {a["key"]: AssetRef(a["key"], f"paper-{a['key']}", a["name"], True, True) for a in universe}

    def tradeable(self, key: str, side: str = "BUY") -> bool:
        return key in self.assets

    def holdings(self) -> dict:
        from .strategy import Holding

        return {k: Holding(quantity=v["qty"], avg_price=v.get("avg_price"))
                for k, v in self.book.items() if v.get("qty", 0) > 0}

    def cash_eur(self) -> float | None:
        return None

    def _fee(self, key: str) -> float:
        return float(self.cfg["execution"]["paper_fee_pct"][self.assets[key]["sleeve"]])

    def buy(self, key: str, amount_eur: float, ref_price: float) -> Fill:
        if ref_price <= 0:
            raise BitpandaError("prix de référence manquant")
        fee = round(amount_eur * self._fee(key), 4)
        qty = (amount_eur - fee) / ref_price
        pos = self.book.setdefault(key, {"qty": 0.0, "avg_price": None})
        old_qty, old_avg = pos["qty"], pos.get("avg_price") or 0.0
        pos["qty"] = old_qty + qty
        pos["avg_price"] = (old_qty * old_avg + amount_eur) / pos["qty"]  # coût moyen frais inclus
        return Fill("BUY", key, qty, ref_price, amount_eur, fee, trade_id="paper")

    def sell(self, key: str, quantity: float, ref_price: float) -> Fill:
        pos = self.book.get(key)
        if not pos or pos["qty"] <= 0:
            raise BitpandaError("aucune position à vendre")
        qty = min(quantity, pos["qty"])
        gross = qty * ref_price
        fee = round(gross * self._fee(key), 4)
        pos["qty"] -= qty
        if pos["qty"] <= 1e-12:
            self.book.pop(key)
        return Fill("SELL", key, qty, ref_price, gross - fee, fee, trade_id="paper")
