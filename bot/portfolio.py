"""Analyse factuelle de TOUT ton portefeuille Bitpanda (lecture seule, aucun ordre).

Commande : `python -m bot.main analyse` (ou l'action GitHub « analyse »).
Résultat : journal/PORTEFEUILLE.md — répartition, concentration, plus/moins-values,
écart à la cible du bot et points d'attention. Ce n'est pas un conseil en
investissement : ce sont des constats chiffrés pour t'aider à décider.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .broker import BitpandaClient, BitpandaError, LiveBroker
from .config import SLEEVES, SLEEVE_LABELS
from .report import JOURNAL_DIR, eur, pct

CLASS_TO_SLEEVE = {"ETF / ETC": "etf", "Actions": "actions", "Crypto": "crypto"}


def _num(obj, default=None):
    try:
        value = obj.get("value") if isinstance(obj, dict) else obj
        return float(value)
    except (TypeError, ValueError, AttributeError):
        return default


def classify(info: dict) -> str:
    kind = str(info.get("type", "")).upper()
    group = str(info.get("group", "")).upper()
    name = str(info.get("name", "")).upper()
    if "CRYPTO" in kind or "CRYPTO" in group:
        return "Crypto"
    if "METAL" in kind or "METAL" in group:
        return "Métaux"
    if any(w in kind or w in group for w in ("ETF", "ETC", "FUND")) or any(
            w in name for w in ("ETF", "ETC", "UCITS", "ISHARES", "XTRACKERS", "VANGUARD", "AMUNDI", "SPDR")):
        return "ETF / ETC"
    if any(w in kind or w in group for w in ("STOCK", "SHARE", "EQUITY", "SECURITY")) or info.get("isin"):
        return "Actions"
    return "Autre"


def build_positions(client: BitpandaClient, cfg: dict) -> tuple[list[dict], float | None]:
    broker = LiveBroker(client, cfg, {})
    eur_id = broker.eur_currency_id()
    rows = client.portfolio(eur_id)
    ids = sorted({r.get("asset_id") for r in rows if r.get("asset_id") and r.get("asset_id") != eur_id})
    infos: dict[str, dict] = {}
    for i in range(0, len(ids), 25):
        for a in client.assets(id=",".join(ids[i:i + 25])):
            infos[a.get("id")] = a
    in_bot_isin = {a.get("isin") for a in cfg["universe"] if a.get("isin")}
    in_bot_symbol = {a.get("symbol") for a in cfg["universe"] if a.get("symbol")}

    cash, positions = None, []
    for r in rows:
        aid = r.get("asset_id")
        if aid == eur_id:
            cash = _num(r.get("balance"), 0.0)
            continue
        qty = _num(r.get("balance"), 0.0)
        if not qty or qty <= 0:
            continue
        info = infos.get(aid, {})
        value = _num(r.get("currency_balance"))
        if not value:
            try:
                t = client.ticker(aid)
                if t.get("currency_id") in (None, eur_id):
                    value = qty * float(t.get("price"))
            except (BitpandaError, TypeError, ValueError):
                value = None
        invested = _num(r.get("invested_amount"))
        ret = _num(r.get("total_return"))
        ret_pct = _num(r.get("total_return_percent"))
        if ret_pct is None and invested and value is not None:
            ret_pct = (value / invested - 1) * 100
        positions.append({
            "name": info.get("name") or info.get("symbol") or aid,
            "symbol": info.get("symbol", ""),
            "isin": info.get("isin", ""),
            "class": classify(info),
            "qty": qty,
            "value": value or 0.0,
            "invested": invested,
            "return": ret,
            "return_pct": ret_pct,
            "in_bot": info.get("isin") in in_bot_isin or info.get("symbol") in in_bot_symbol,
        })
    positions.sort(key=lambda p: -p["value"])
    return positions, cash


def to_markdown(positions: list[dict], cash: float | None, cfg: dict, today: date) -> str:
    total = sum(p["value"] for p in positions)
    invested = sum(p["invested"] for p in positions if p["invested"])
    L = [f"# Analyse du portefeuille Bitpanda — {today:%d/%m/%Y}", "",
         "_Lecture seule : aucun ordre passé. Constats chiffrés, pas un conseil en investissement._", ""]
    if not positions:
        L += ["Aucune position trouvée sur le compte.", ""]
        return "\n".join(L)

    pnl = total - invested if invested else None
    L += ["## Vue d'ensemble", "",
          "| Indicateur | Valeur |", "|---|---:|",
          f"| Valeur des positions | **{eur(total)}** |",
          f"| Montant investi | {eur(invested) if invested else '—'} |",
          f"| Plus/moins-value latente | {eur(pnl) if pnl is not None else '—'}"
          f"{' (' + pct(total / invested - 1) + ')' if invested else ''} |",
          f"| Liquidités EUR | {eur(cash) if cash is not None else '—'} |",
          f"| Nombre de lignes | {len(positions)} |", ""]

    by_class: dict[str, float] = {}
    for p in positions:
        by_class[p["class"]] = by_class.get(p["class"], 0.0) + p["value"]
    alloc = cfg["allocation"]
    L += ["## Répartition par classe d'actifs", "",
          "| Classe | Valeur | Poids | Cible du bot | Écart (points) |", "|---|---:|---:|---:|---:|"]
    for cls, val in sorted(by_class.items(), key=lambda kv: -kv[1]):
        w = val / total if total else 0
        sleeve = CLASS_TO_SLEEVE.get(cls)
        target = float(alloc.get(sleeve, 0)) if sleeve else 0.0
        L.append(f"| {cls} | {eur(val)} | {pct(w, False)} | {pct(target, False)} | {pct(w - target)} |")
    for s in SLEEVES:
        label = next((c for c, v in CLASS_TO_SLEEVE.items() if v == s), SLEEVE_LABELS[s])
        if label not in by_class and float(alloc.get(s, 0)) > 0:
            L.append(f"| {label} | {eur(0)} | 0,0% | {pct(float(alloc[s]), False)} | {pct(-float(alloc[s]))} |")
    L.append("")

    L += ["## Détail des lignes", "",
          "| Actif | Classe | Valeur | Poids | Investi | +/- | Suivi par le bot |",
          "|---|---|---:|---:|---:|---:|:---:|"]
    for p in positions:
        w = p["value"] / total if total else 0
        perf = pct(p["return_pct"] / 100) if p["return_pct"] is not None else "—"
        L.append(f"| {p['name']} | {p['class']} | {eur(p['value'])} | {pct(w, False)} | "
                 f"{eur(p['invested']) if p['invested'] else '—'} | {perf} | {'✅' if p['in_bot'] else ''} |")
    L.append("")

    weights = [p["value"] / total for p in positions if total]
    hhi = sum(w * w for w in weights)
    top1 = weights[0] if weights else 0
    top3 = sum(weights[:3])
    tiny = [p for p in positions if total and p["value"] / total < 0.01]
    L += ["## Concentration", "",
          f"- Plus grosse ligne : **{positions[0]['name']}**, {pct(top1, False)} du portefeuille.",
          f"- 3 plus grosses lignes : {pct(top3, False)}.",
          f"- Nombre « effectif » de lignes (diversification réelle) : {1 / hhi:.1f}".replace(".", ",")
          + f" sur {len(positions)}.",
          f"- Petites lignes (< 1 %) : {len(tiny)}.", ""]

    notes = []
    crypto_w = by_class.get("Crypto", 0) / total if total else 0
    if crypto_w > float(alloc["max"].get("crypto", 1) if isinstance(alloc.get("max"), dict) else 1):
        notes.append(f"La crypto pèse {pct(crypto_w, False)} : au-dessus du plafond de "
                     f"{pct(float(alloc['max']['crypto']), False)} que tu as fixé au bot.")
    for p in positions:
        w = p["value"] / total if total else 0
        if p["class"] == "Actions" and w > 0.10:
            notes.append(f"{p['name']} représente {pct(w, False)} à elle seule : une seule entreprise porte une "
                         "grosse part du risque.")
        if p["class"] == "Crypto" and p["symbol"] not in ("BTC", "ETH") and w > 0.05:
            notes.append(f"{p['name']} ({pct(w, False)}) est une crypto hors Bitcoin/Ethereum : en général "
                         "plus volatile et moins liquide.")
        if p["return_pct"] is not None and p["return_pct"] <= -30:
            notes.append(f"{p['name']} est à {pct(p['return_pct'] / 100)} par rapport à ton prix moyen d'achat.")
    if len(tiny) >= 5:
        notes.append(f"{len(tiny)} lignes pèsent moins de 1 % chacune : elles compliquent le suivi et la "
                     "déclaration fiscale sans changer grand-chose au résultat.")
    if cash is not None and total and cash / (total + cash) > 0.25:
        notes.append(f"{eur(cash)} dorment en liquidités sur le compte ({pct(cash / (total + cash), False)} du total).")
    if not notes:
        notes.append("Rien d'anormal détecté par les règles automatiques.")
    L += ["## Points d'attention", ""] + [f"- {n}" for n in notes] + [""]
    L += ["---", "_Les positions déjà présentes ne sont jamais vendues par le bot : il ne gère que ce qu'il achète._", ""]
    return "\n".join(L)


def analyse(cfg: dict, env: dict, *, client=None, journal_base: Path | None = None, today: date | None = None) -> int:
    from .main import _github_summary, today_paris

    key = env.get("BITPANDA_API_KEY", "").strip()
    if not key and client is None:
        print("Secret BITPANDA_API_KEY absent : ajoute-le dans Settings → Secrets and variables → Actions.")
        return 1
    client = client or BitpandaClient(key)
    try:
        positions, cash = build_positions(client, cfg)
    except BitpandaError as exc:
        msg = f"Lecture du portefeuille impossible : {exc} (la clé a-t-elle le droit « Balances » ?)"
        print(msg)
        _github_summary(msg)
        return 1
    text = to_markdown(positions, cash, cfg, today or today_paris())
    folder = journal_base or JOURNAL_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "PORTEFEUILLE.md").write_text(text, encoding="utf-8")
    print(text)
    _github_summary(text)
    return 0
