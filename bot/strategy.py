"""Stratégie adaptative : rythme d'achat réglé sur les opportunités et le budget disponible.

Principe
--------
* Chaque jour ouvré, le bot « provisionne » 1/5 du budget hebdo, réparti entre
  trois poches (ETF / actions / crypto, 60/20/20 par défaut), multiplié par un
  coefficient d'opportunité (0 à 2,5) : marché baissier → on ralentit ; repli
  dans une tendance saine ou peur excessive → on accélère ; surchauffe → on
  lève le pied.
* Quand la provision d'une poche atteint l'ordre minimum, il achète les
  actifs les plus forts du moment (momentum relatif, en tendance haussière ;
  pour les actions, momentum rapporté à la volatilité), pondérés par
  l'inverse de leur volatilité.
* Si aucune action n'est en tendance haussière, la provision « actions »
  part sur les ETF plutôt que d'attendre indéfiniment.
* Le vendredi, un plancher garantit qu'une part du budget est investie sur le
  socle, même quand rien n'est attractif (achats réguliers).
* Ventes uniquement sur les lignes satellites : stop suiveur, cassure de
  tendance, prise de bénéfices partielle. Le socle n'est jamais vendu.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from .config import MARKET_REFERENCE, SLEEVE_LABELS, SLEEVES, sleeve_value
from .indicators import Signal

WEEKDAYS = 5
FRIDAY = 4


def _fr(text: str) -> str:
    """Virgule décimale à la française dans les explications."""
    return text.replace(".", ",")


@dataclass
class MarketContext:
    vix: float | None = None
    crypto_fng: int | None = None
    bias: dict = field(default_factory=lambda: {s: 0.0 for s in SLEEVES})
    veto: set = field(default_factory=set)
    agent_summary: str = ""
    agent_sources: list = field(default_factory=list)
    analysts: dict = field(default_factory=dict)   # clé → consensus des analystes (actions)


@dataclass
class Holding:
    quantity: float
    avg_price: float | None = None


@dataclass
class Order:
    side: str          # BUY ou SELL
    key: str
    sleeve: str
    amount_eur: float = 0.0
    quantity: float = 0.0
    reason: str = ""


@dataclass
class Plan:
    buys: list[Order]
    sells: list[Order]
    multipliers: dict
    reasons: dict
    split: dict
    notes: list[str]
    pending: dict


def core_of(assets: dict, sleeve: str) -> dict | None:
    return next((a for a in assets.values() if a["sleeve"] == sleeve and a.get("core")), None)


# ---------------------------------------------------------------------------
# Coefficient d'opportunité par poche
# ---------------------------------------------------------------------------

def sleeve_multiplier(sleeve: str, ref: Signal | None, market: MarketContext, cfg: dict) -> tuple[float, list[str]]:
    p = cfg["signals"]
    reasons: list[str] = []
    if ref is None:
        return 0.5, ["pas de données sur l'actif de référence : prudence (×0,5)"]

    m = 1.0
    stocks_market = MARKET_REFERENCE.get(sleeve, sleeve) == "etf"
    label = "marché actions (ETF socle)" if stocks_market else "crypto de référence"
    if not ref.above_long:
        m *= 0.5
        reasons.append(f"{label} sous sa moyenne 200 j : tendance baissière, on ralentit (×0,5)")
    elif ref.is_dip(p["rsi_dip"]):
        m *= 1.75
        reasons.append(f"repli (RSI {ref.rsi:.0f}) dans une tendance haussière : opportunité (×1,75)")
    elif ref.drawdown <= -float(p["pullback_pct"]):
        m *= 1.5
        reasons.append(f"recul de {abs(ref.drawdown):.0%} depuis le plus haut 3 mois, tendance intacte (×1,5)")
    else:
        reasons.append(f"{label} en tendance {'haussière' if ref.trend_up else 'neutre'} : rythme normal")

    if ref.is_hot(p["rsi_hot"]):
        m *= 0.6
        reasons.append(f"surchauffe (RSI {ref.rsi:.0f}) : on lève le pied (×0,6)")

    intact = ref.sma_long is None or ref.price >= ref.sma_long * 0.95
    if stocks_market and market.vix is not None:
        if market.vix >= p["vix_fear"]:
            if intact:
                m *= 1.4
                reasons.append(f"peur élevée sur les actions (VIX {market.vix:.0f}) sans cassure : on profite (×1,4)")
            else:
                m *= 0.8
                reasons.append(f"peur élevée (VIX {market.vix:.0f}) et tendance cassée : prudence (×0,8)")
        elif market.vix <= p["vix_calm"] and ref.is_hot(p["rsi_hot"] - 5):
            m *= 0.8
            reasons.append(f"VIX très bas ({market.vix:.0f}) et marché tendu : excès de confiance (×0,8)")
    if not stocks_market and market.crypto_fng is not None:
        fng = market.crypto_fng
        if fng <= p["crypto_fear"]:
            if intact:
                m *= 1.5
                reasons.append(f"peur extrême sur la crypto (indice {fng}) sans cassure : on profite (×1,5)")
            else:
                m *= 0.75
                reasons.append(f"peur extrême (indice {fng}) et tendance cassée : prudence (×0,75)")
        elif fng >= p["crypto_greed"]:
            m *= 0.5
            reasons.append(f"avidité extrême sur la crypto (indice {fng}) : on lève le pied (×0,5)")

    bias = max(-1.0, min(1.0, float(market.bias.get(sleeve, 0.0) or 0.0)))
    if bias:
        factor = 1 + float(cfg["agent"]["weight"]) * bias
        m *= factor
        reasons.append(_fr(f"veille d'actualité {'favorable' if bias > 0 else 'défavorable'} (×{factor:.2f})"))

    m = max(0.0, min(float(p["max_multiplier"]), m))
    return round(m, 2), reasons


# ---------------------------------------------------------------------------
# Répartition des nouveaux achats entre les poches
# ---------------------------------------------------------------------------

def sleeve_split(sleeve_values: dict, total: float, cfg: dict) -> tuple[dict, str | None]:
    alloc = cfg["allocation"]
    target = {s: float(alloc.get(s, 0.0)) for s in SLEEVES}
    if total < float(cfg["risk"]["caps_min_portfolio_eur"]):
        return target, None
    band = float(alloc["rebalance_band"])
    shares, notes = {}, []
    for s in SLEEVES:
        t = target[s]
        w = sleeve_values.get(s, 0.0) / total if total else 0.0
        label = SLEEVE_LABELS[s]
        if t <= 0:
            shares[s] = 0.0
        elif w >= sleeve_value(alloc["max"], s, 1.0):
            shares[s] = 0.0
            notes.append(f"{label} à {w:.0%} du portefeuille (plafond) : achats suspendus")
        elif w > t + band:
            shares[s] = 0.0
            notes.append(f"{label} à {w:.0%} (cible {t:.0%}) : nouveaux achats réorientés")
        elif w < t - band:
            shares[s] = t * 1.5
            notes.append(f"{label} à {w:.0%} (cible {t:.0%}) : rattrapage progressif")
        else:
            shares[s] = t
    total_share = sum(shares.values())
    if total_share <= 0:
        return target, None
    return {s: v / total_share for s, v in shares.items()}, " ; ".join(notes) or None


# ---------------------------------------------------------------------------
# Ventes
# ---------------------------------------------------------------------------

def _days_since(iso: str | None, today: date) -> int:
    if not iso:
        return 10_000
    return (today - date.fromisoformat(iso)).days


def plan_sells(signals: dict, holdings: dict, meta: dict, assets: dict, cfg: dict,
               today: date, tradeable: Callable[[str, str], bool]) -> list[Order]:
    ex = cfg["exits"]
    orders: list[Order] = []
    for key, h in holdings.items():
        asset = assets.get(key)
        sig = signals.get(key)
        if not asset or not sig or asset.get("core") or h.quantity <= 0:
            continue
        if not tradeable(key, "SELL"):
            continue
        m = meta.get(key, {})
        if _days_since(m.get("first_buy"), today) < int(ex["min_hold_days"]):
            continue
        sleeve = asset["sleeve"]
        peak = max(float(m.get("peak") or sig.price), sig.price)
        trail = sleeve_value(ex["trailing_stop"], sleeve, 0.2)
        cost = h.avg_price or m.get("avg_cost")
        value = h.quantity * sig.price

        if sig.price <= peak * (1 - trail) and sig.sma_short is not None and sig.price < sig.sma_short:
            reason = f"stop suiveur : {sig.price / peak - 1:.0%} depuis le plus haut et sous la moyenne 50 j"
            orders.append(Order("SELL", key, sleeve, quantity=h.quantity, amount_eur=value, reason=reason))
        elif (sig.sma_long is not None and sig.sma_short is not None
              and sig.price < sig.sma_long * (1 - float(ex["trend_break_pct"]))
              and sig.sma_short < sig.sma_long):
            reason = "tendance cassée : sous la moyenne 200 j et moyenne 50 j < 200 j"
            orders.append(Order("SELL", key, sleeve, quantity=h.quantity, amount_eur=value, reason=reason))
        elif (cost and sig.price / float(cost) - 1 >= float(ex["take_profit_gain"])
              and sig.rsi >= float(ex["take_profit_rsi"])
              and _days_since(m.get("last_trim"), today) >= int(ex["take_profit_cooldown_days"])):
            qty = h.quantity * float(ex["take_profit_trim"])
            reason = (f"prise de bénéfices partielle ({ex['take_profit_trim']:.0%}) : "
                      f"{sig.price / float(cost) - 1:+.0%} et RSI {sig.rsi:.0f}")
            orders.append(Order("SELL", key, sleeve, quantity=qty, amount_eur=qty * sig.price, reason=reason))
    return [o for o in orders if o.amount_eur >= 1.0]


# ---------------------------------------------------------------------------
# Choix des actifs à acheter dans une poche
# ---------------------------------------------------------------------------

def _split_amount(amount: float, cands: list, min_order: float) -> list[float] | None:
    inv = [1 / max(sig.volatility, 0.05) for _, _, sig in cands]
    total_inv = sum(inv)
    parts = [round(amount * w / total_inv, 2) for w in inv]
    parts[-1] = round(amount - sum(parts[:-1]), 2)
    return parts if min(parts) >= min_order else None


def score_of(sig: Signal, sleeve: str, p: dict, consensus: dict | None = None) -> float:
    risk_adjusted = sleeve in (p.get("risk_adjusted") or [])
    score = sig.momentum / max(sig.volatility, 0.10) if risk_adjusted else sig.momentum
    bonus = 0.1 if risk_adjusted else 0.03
    if sig.is_dip(p["rsi_dip"]):
        score += bonus
    if sig.is_hot(p["rsi_hot"]):
        score -= bonus
    if consensus and consensus.get("upside") is not None:
        upside = max(0.0, min(0.5, float(consensus["upside"])))
        score += float((p.get("analysts") or {}).get("upside_weight", 0.5)) * upside
    return score


def analysts_ok(consensus: dict | None, p: dict) -> bool:
    a = p.get("analysts") or {}
    if not consensus:
        return False
    if consensus.get("analysts", 0) < int(a.get("min_analysts", 5)):
        return False
    if consensus.get("recommendation", 5) > float(a.get("max_recommendation", 2.5)):
        return False
    upside = consensus.get("upside")
    return upside is None or upside >= float(a.get("min_upside", 0.0))


def pick_assets(sleeve: str, amount: float, signals: dict, assets: dict, market: MarketContext,
                values: dict, total: float, cfg: dict, tradeable: Callable[[str, str], bool],
                notes: list[str]) -> list[Order]:
    p = cfg["signals"]
    min_order = float(cfg["budget"]["min_order_eur"])
    top_n = int(sleeve_value(p["top_n"], sleeve, 1))
    risk = cfg["risk"]
    cap = sleeve_value(risk["max_position_pct"], sleeve, 0.4)

    use_analysts = sleeve == "actions" and (p.get("analysts") or {}).get("enabled", False)
    cands, rejected = [], []
    for key, asset in assets.items():
        if asset["sleeve"] != sleeve:
            continue
        sig = signals.get(key)
        if sig is None or not tradeable(key, "BUY") or key in market.veto or not sig.trend_up:
            continue
        consensus = market.analysts.get(key) if use_analysts else None
        if use_analysts and not analysts_ok(consensus, p):
            rejected.append(asset["name"])
            continue
        cands.append((score_of(sig, sleeve, p, consensus), asset, sig))
    cands.sort(key=lambda c: c[0], reverse=True)
    if rejected:
        notes.append("en tendance mais écartées par le consensus des analystes (ou consensus indisponible) : "
                     + ", ".join(rejected))

    if sleeve == "actions" and total >= float(risk["caps_min_portfolio_eur"]):
        max_theme = float(risk.get("max_theme_pct", 1.0))
        theme_of = {k: a.get("theme", "autre") for k, a in assets.items() if a["sleeve"] == "actions"}
        theme_value: dict[str, float] = {}
        for k, v in values.items():
            if k in theme_of:
                theme_value[theme_of[k]] = theme_value.get(theme_of[k], 0.0) + v
        kept, full = [], set()
        for score, asset, sig in cands:
            th = theme_of.get(asset["key"], "autre")
            share = amount / max(1, min(len(cands), top_n))
            if (theme_value.get(th, 0.0) + share) / (total + amount) > max_theme:
                full.add(th)
                continue
            kept.append((score, asset, sig))
        cands = kept
        if full:
            notes.append(f"thème(s) déjà à {max_theme:.0%} du portefeuille, pas de nouvel achat : " + ", ".join(sorted(full)))

    if total >= float(risk["caps_min_portfolio_eur"]):
        kept = []
        for score, asset, sig in cands:
            share = amount / max(1, min(len(cands), top_n))
            after = (values.get(asset["key"], 0.0) + share) / (total + amount)
            if not asset.get("core") and after > cap:
                notes.append(f"{asset['name']} écarté : dépasserait {cap:.0%} du portefeuille")
                continue
            kept.append((score, asset, sig))
        cands = kept
    cands = cands[:top_n]

    fallback = False
    if not cands:
        core = core_of(assets, sleeve)
        if core and signals.get(core["key"]) and tradeable(core["key"], "BUY") and core["key"] not in market.veto:
            cands = [(0.0, core, signals[core["key"]])]
            fallback = True
        else:
            return []

    parts = None
    n = max(1, min(len(cands), int(amount // min_order)))
    while n >= 1:
        parts = _split_amount(amount, cands[:n], min_order)
        if parts:
            break
        n -= 1
    if not parts:
        return []

    orders = []
    for (score, asset, sig), part in zip(cands[:n], parts):
        if fallback:
            reason = "aucun actif en tendance haussière : achat régulier sur le socle"
        else:
            bits = [_fr(f"momentum {sig.momentum:+.1%}"), "tendance haussière"]
            if sig.is_dip(p["rsi_dip"]):
                bits.append(f"repli RSI {sig.rsi:.0f}")
            consensus = market.analysts.get(asset["key"])
            if sleeve == "actions" and consensus:
                txt = f"analystes {consensus['recommendation']:.1f}/5 ({consensus['analysts']})"
                if consensus.get("upside") is not None:
                    txt += f", objectif {consensus['upside']:+.0%}"
                bits.append(_fr(txt))
            reason = ", ".join(bits)
        orders.append(Order("BUY", asset["key"], sleeve, amount_eur=part, reason=reason))
    return orders


# ---------------------------------------------------------------------------
# Plan du jour
# ---------------------------------------------------------------------------

def build_plan(today: date, cfg: dict, assets: dict, signals: dict, holdings: dict, meta: dict,
               reserve: float, spent_week: float, pending: dict, market: MarketContext,
               tradeable: Callable[[str, str], bool], cash: float | None = None,
               external: dict | None = None) -> Plan:
    """external = le reste du compte Bitpanda ({'sleeves': {poche: €}, 'keys': {clé: €}}), compté pour la
    répartition et les plafonds, jamais vendu."""
    b = cfg["budget"]
    weekly = float(b["weekly_eur"])
    min_order = float(b["min_order_eur"])
    notes: list[str] = []
    pending = {s: float(pending.get(s, 0.0)) for s in SLEEVES}

    values = {k: h.quantity * signals[k].price for k, h in holdings.items() if k in signals and k in assets}
    sleeve_values = {s: sum(v for k, v in values.items() if assets[k]["sleeve"] == s) for s in SLEEVES}
    ext = external or {}
    for s in SLEEVES:
        sleeve_values[s] += float((ext.get("sleeves") or {}).get(s, 0.0))
    values_all = dict(values)
    for k, v in (ext.get("keys") or {}).items():
        values_all[k] = values_all.get(k, 0.0) + float(v)
    total = sum(sleeve_values.values())

    sells = plan_sells(signals, holdings, meta, assets, cfg, today, tradeable)

    split, split_note = sleeve_split(sleeve_values, total, cfg)
    if split_note:
        notes.append(split_note)
    receiver = max(split, key=split.get)
    for s in SLEEVES:  # poche suspendue : sa provision est réorientée
        if split[s] == 0 and pending[s] > 0 and s != receiver:
            pending[receiver] += pending[s]
            pending[s] = 0.0

    multipliers, reasons = {}, {}
    for s in SLEEVES:
        if float(cfg["allocation"].get(s, 0.0)) <= 0:
            pending[s] = 0.0  # poche à 0 % : ni provision ni achat
            continue
        ref_sleeve = MARKET_REFERENCE.get(s, s)
        core = core_of(assets, ref_sleeve)
        ref = signals.get(core["key"]) if core else None
        if ref is None:
            ref = next((signals[k] for k, a in assets.items() if a["sleeve"] == ref_sleeve and k in signals), None)
        multipliers[s], reasons[s] = sleeve_multiplier(s, ref, market, cfg)

    # 1) provision du jour
    base_daily = weekly / WEEKDAYS
    for s in SLEEVES:
        pending[s] += base_daily * split[s] * multipliers.get(s, 0.0)
    total_pending = sum(pending.values())
    if total_pending > reserve > 0:
        ratio = reserve / total_pending
        pending = {s: v * ratio for s, v in pending.items()}
    elif reserve <= 0:
        pending = {s: 0.0 for s in SLEEVES}
    pending = {s: round(v, 2) for s, v in pending.items()}

    # 2) enveloppe disponible aujourd'hui
    available = reserve if cash is None else min(reserve, cash)
    if cash is not None and cash < min_order <= reserve:
        notes.append(f"solde EUR Bitpanda insuffisant ({cash:.2f} €) : pense à alimenter ton compte")
    spend_left = max(0.0, min(available, weekly * float(b["max_daily_pct_of_weekly"])))

    buys: list[Order] = []
    for s in SLEEVES:  # actions → crypto → ETF
        amount = round(min(pending[s], spend_left), 2)
        if amount < min_order:
            continue
        orders = pick_assets(s, amount, signals, assets, market, values_all, total, cfg, tradeable, notes)
        if not orders:
            if core_of(assets, s) is None and s != "etf":  # repli sur l'ETF, la poche diversifiée
                notes.append(f"aucune ligne {SLEEVE_LABELS[s].lower()} achetable (tendance) : "
                             f"la provision de {pending[s]:.2f} € part sur les ETF".replace(".", ","))
                pending["etf"] = round(pending["etf"] + pending[s], 2)
                pending[s] = 0.0
            else:
                notes.append(f"poche {SLEEVE_LABELS[s]} : aucun actif achetable aujourd'hui, la provision est conservée")
            continue
        spent = round(sum(o.amount_eur for o in orders), 2)
        pending[s] = round(pending[s] - spent, 2)
        spend_left = round(spend_left - spent, 2)
        buys.extend(orders)

    # 3) plancher du vendredi : une part minimale investie sur le socle
    floor_pct = float(b["min_weekly_invest_pct"])
    if today.weekday() == FRIDAY and floor_pct > 0 and weekly > 0:
        already = spent_week + sum(o.amount_eur for o in buys)
        gap = round(floor_pct * weekly - already, 2)
        if gap > 0.5:
            amount = round(min(max(gap, min_order), spend_left), 2)
            core = core_of(assets, "etf")
            ok = (core is not None and core["key"] in signals and tradeable(core["key"], "BUY")
                  and core["key"] not in market.veto)
            if amount >= min_order and ok:
                reason = f"investissement minimum de la semaine ({floor_pct:.0%} du budget)"
                existing = next((o for o in buys if o.key == core["key"]), None)
                if existing:
                    existing.amount_eur = round(existing.amount_eur + amount, 2)
                    existing.reason += f" + {reason}"
                else:
                    buys.append(Order("BUY", core["key"], "etf", amount_eur=amount, reason=reason))
                pending["etf"] = round(max(0.0, pending["etf"] - amount), 2)
                spend_left = round(spend_left - amount, 2)
            else:
                notes.append("plancher du vendredi non appliqué (réserve ou socle indisponible)")

    # 4) nombre d'ordres limité
    max_orders = int(cfg["execution"]["max_orders_per_run"])
    if len(sells) + len(buys) > max_orders:
        room = max(0, max_orders - len(sells))
        ranked = sorted(buys, key=lambda o: o.amount_eur, reverse=True)
        for o in ranked[room:]:
            pending[o.sleeve] = round(pending[o.sleeve] + o.amount_eur, 2)
        buys = ranked[:room]
        sells = sells[:max_orders]
        notes.append("nombre maximum d'ordres atteint : les plus petits achats sont reportés")

    return Plan(buys=buys, sells=sells, multipliers=multipliers, reasons=reasons,
                split=split, notes=notes, pending=pending)
