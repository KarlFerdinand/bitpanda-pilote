"""Veille d'actualité quotidienne par Claude (API Anthropic + recherche web).

Optionnelle : active seulement si le secret ANTHROPIC_API_KEY est défini.
Son influence est bornée : un biais de -1 à +1 par poche (±20 % maximum sur le
montant du jour avec le réglage par défaut) et, rarement, un veto d'achat d'un
jour sur un actif précis. Elle ne déclenche jamais de vente.
"""
from __future__ import annotations

import json
import logging
from datetime import date

import requests

from .strategy import MarketContext

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
SEARCH_TOOL = "web_search_20260318"

PROMPT = """Tu es un analyste de marché prudent qui assiste un petit robot d'investissement \
(environ {weekly:.0f} €/semaine : {etf_pct:.0%} ETF, {actions_pct:.0%} actions de grandes entreprises \
US et européennes, {crypto_pct:.0%} cryptos majeures). \
Nous sommes le {today}.

Fais au plus {max_searches} recherches web pour savoir ce qui compte AUJOURD'HUI pour ces actifs :
{assets}

Cherche notamment : mouvements marquants des marchés actions (US, Europe, émergents), décisions \
ou annonces de banques centrales imminentes, statistiques macro importantes du jour ou du lendemain, \
événement propre à un ETF ou à un secteur, résultats trimestriels ou annonce majeure d'une des \
entreprises suivies, actualité réglementaire ou piratage majeur côté crypto.

Signaux techniques déjà calculés par le robot (inutile de les refaire) :
{technicals}

Réponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"etf_bias": <-1 à 1>, "actions_bias": <-1 à 1>, "crypto_bias": <-1 à 1>, "veto": [<clés à NE PAS acheter aujourd'hui>], \
"summary": "<3 phrases max, en français>", "sources": [{{"title": "...", "url": "..."}}]}}

Règles : biais 0 par défaut ; ±0,3 pour un contexte nettement favorable ou défavorable ; ±1 \
seulement pour un événement exceptionnel. Une baisse de marché sans cause fondamentale n'est pas \
un biais négatif. Veto rare, uniquement pour un événement précis et grave, expliqué dans summary. \
Clés valides : {keys}."""


def _extract_json(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    found = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict) and any(k in obj for k in ("etf_bias", "actions_bias", "crypto_bias")):
            found = obj
    return found


def _clamp(value) -> float:
    try:
        return max(-1.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def parse_view(payload: dict, valid_keys: set[str]) -> dict:
    sources = []
    for s in payload.get("sources") or []:
        if isinstance(s, dict) and str(s.get("url", "")).startswith("http"):
            sources.append({"title": str(s.get("title") or s["url"])[:150], "url": s["url"]})
    return {
        "bias": {"etf": _clamp(payload.get("etf_bias")),
                 "actions": _clamp(payload.get("actions_bias", payload.get("etf_bias"))),
                 "crypto": _clamp(payload.get("crypto_bias"))},
        "veto": {k for k in payload.get("veto") or [] if k in valid_keys},
        "summary": str(payload.get("summary") or "")[:800],
        "sources": sources[:8],
    }


def fetch_view(api_key: str, cfg: dict, signals: dict, today: date,
               session: requests.Session | None = None) -> dict:
    session = session or requests.Session()
    agent = cfg["agent"]
    assets_txt = "\n".join(f"- {a['key']} : {a['name']} ({a['sleeve']})" for a in cfg["universe"])
    tech_txt = "\n".join(
        f"- {k} : prix {s.price:.2f}, tendance {'haussière' if s.trend_up else 'non haussière'}, "
        f"RSI {s.rsi:.0f}, momentum {s.momentum:+.1%}" for k, s in signals.items()
    ) or "- (aucun)"
    prompt = PROMPT.format(
        weekly=float(cfg["budget"]["weekly_eur"]), etf_pct=float(cfg["allocation"]["etf"]),
        actions_pct=float(cfg["allocation"].get("actions", 0)),
        crypto_pct=float(cfg["allocation"]["crypto"]), today=today.isoformat(),
        max_searches=int(agent["max_searches"]), assets=assets_txt, technicals=tech_txt,
        keys=", ".join(a["key"] for a in cfg["universe"]),
    )
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": SEARCH_TOOL, "name": "web_search", "max_uses": int(agent["max_searches"]),
              "allowed_callers": ["direct"]}]

    data = {}
    for _ in range(4):  # la recherche peut mettre le tour en pause (stop_reason = pause_turn)
        r = session.post(API_URL, headers=headers, timeout=180,
                         json={"model": agent["model"], "max_tokens": 2000, "messages": messages, "tools": tools})
        r.raise_for_status()
        data = r.json()
        if data.get("stop_reason") != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": data.get("content", [])}]

    text = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    payload = _extract_json(text)
    if payload is None:
        raise ValueError("réponse de la veille sans JSON exploitable")
    return parse_view(payload, {a["key"] for a in cfg["universe"]})


def apply_view(market: MarketContext, view: dict) -> None:
    market.bias = view["bias"]
    market.veto = set(view["veto"])
    market.agent_summary = view["summary"]
    market.agent_sources = view["sources"]
