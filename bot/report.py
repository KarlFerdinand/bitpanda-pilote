"""Journal lisible (Markdown, affiché par GitHub) + registre des opérations (CSV, utile pour les impôts)."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import ROOT

JOURNAL_DIR = ROOT / "journal"
TRADES_HEADER = ["date", "mode", "sens", "cle", "nom", "isin_ou_symbole", "quantite", "prix_eur",
                 "montant_eur", "frais_eur", "id_operation", "raison"]


def eur(x: float | None) -> str:
    if x is None:
        return "—"
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €"


def pct(x: float | None, signed: bool = True) -> str:
    if x is None:
        return "—"
    return (f"{x:+.1%}" if signed else f"{x:.1%}").replace(".", ",")


@dataclass
class RunReport:
    today: date
    mode: str
    notes: list = field(default_factory=list)
    ledger_note: str | None = None
    account_note: str | None = None
    reserve: float = 0.0
    spent_week: float = 0.0
    weekly: float = 0.0
    pending: dict = field(default_factory=dict)
    multipliers: dict = field(default_factory=dict)
    reasons: dict = field(default_factory=dict)
    vix: float | None = None
    fng: int | None = None
    agent_summary: str = ""
    agent_sources: list = field(default_factory=list)
    executed: list = field(default_factory=list)   # (Order, Fill)
    failed: list = field(default_factory=list)     # (Order, message)
    positions: list = field(default_factory=list)  # dict(name, qty, price, value, avg, pnl)
    signals: list = field(default_factory=list)    # Signal
    kid_links: dict = field(default_factory=dict)
    error: str | None = None

    # ------------------------------------------------------------------
    def headline(self) -> str:
        buys = [f for o, f in self.executed if o.side == "BUY"]
        sells = [f for o, f in self.executed if o.side == "SELL"]
        parts = []
        if buys:
            parts.append(f"{len(buys)} achat(s) pour {eur(sum(f.notional for f in buys))}")
        if sells:
            parts.append(f"{len(sells)} vente(s) pour {eur(sum(f.notional for f in sells))}")
        if self.failed:
            parts.append(f"{len(self.failed)} ordre(s) refusé(s)")
        if not parts:
            parts.append("aucun ordre aujourd'hui")
        return ", ".join(parts)

    def to_markdown(self, names: dict) -> str:
        mode_label = "RÉEL" if self.mode == "live" else "SIMULATION"
        L = [f"# Journal du {self.today:%d/%m/%Y} — mode {mode_label}", ""]
        if self.error:
            L += [f"> ⚠️ **Erreur** : {self.error}", ""]

        total_value = sum(p["value"] for p in self.positions)
        total_cost = sum(p["cost"] for p in self.positions if p["cost"])
        L += ["## En bref", "",
              f"- **{self.headline()}**",
              f"- Budget de la semaine : {eur(self.spent_week)} dépensés sur {eur(self.weekly)} ; "
              f"réserve disponible : **{eur(self.reserve)}**",
              f"- Provision en attente : ETF {eur(self.pending.get('etf'))}, actions {eur(self.pending.get('actions'))}, "
              f"crypto {eur(self.pending.get('crypto'))}",
              f"- Portefeuille : **{eur(total_value)}**"
              + (f" (coût {eur(total_cost)}, {pct(total_value / total_cost - 1)})" if total_cost else ""),
              ""]
        if self.account_note:
            L += [f"> {self.account_note}", ""]
        if self.ledger_note:
            L += [f"> {self.ledger_note}", ""]

        L += ["## Climat de marché", "",
              f"VIX (peur sur les actions US) : **{self.vix:.1f}**".replace(".", ",") if self.vix is not None else "VIX : indisponible",
              "",
              f"Fear & Greed crypto : **{self.fng}/100**" if self.fng is not None else "Fear & Greed crypto : indisponible",
              "", "| Poche | Coefficient du jour | Pourquoi |", "|---|:---:|---|"]
        for s, label in (("etf", "ETF"), ("actions", "Actions"), ("crypto", "Crypto")):
            if s in self.multipliers:
                L.append(f"| {label} | ×{self.multipliers[s]:.2f} |".replace(".", ",") + f" {' ; '.join(self.reasons.get(s, []))} |")
        L.append("")

        if self.agent_summary:
            L += ["## Veille d'actualité (Claude)", "", self.agent_summary, ""]
            for src in self.agent_sources:
                L.append(f"- [{src['title']}]({src['url']})")
            L.append("")

        L += ["## Ordres", ""]
        if self.executed or self.failed:
            L += ["| Sens | Actif | Montant | Quantité | Prix | Frais | Raison | Statut |",
                  "|---|---|---:|---:|---:|---:|---|---|"]
            for o, f in self.executed:
                L.append(f"| {'Achat' if o.side == 'BUY' else 'Vente'} | {names.get(o.key, o.key)} | "
                         f"{eur(f.notional)} | {f.quantity:.6f} | {eur(f.price)} | {eur(f.fee)} | {o.reason} | ✅ |")
            for o, msg in self.failed:
                amount = eur(o.amount_eur)
                L.append(f"| {'Achat' if o.side == 'BUY' else 'Vente'} | {names.get(o.key, o.key)} | {amount} | | | | "
                         f"{o.reason} | ❌ {msg} |")
        else:
            L.append("Aucun ordre aujourd'hui.")
        L.append("")

        if self.notes:
            L += ["## Remarques", ""] + [f"- {n}" for n in self.notes] + [""]

        L += ["## Portefeuille", ""]
        if self.positions:
            L += ["| Actif | Quantité | Prix | Valeur | Coût moyen | +/- |", "|---|---:|---:|---:|---:|---:|"]
            for p in sorted(self.positions, key=lambda p: -p["value"]):
                pnl = p["value"] / p["cost"] - 1 if p["cost"] else None
                L.append(f"| {p['name']} | {p['qty']:.6f} | {eur(p['price'])} | {eur(p['value'])} | "
                         f"{eur(p['avg'])} | {pct(pnl)} |")
            L.append(f"| **Total** | | | **{eur(total_value)}** | | |")
        else:
            L.append("Pas encore de position.")
        L.append("")

        if self.signals:
            L += ["## Signaux du jour", "",
                  "| Poche | Actif | Prix | Tendance | RSI | Momentum (1-6 mois) | Recul 3 mois |",
                  "|---|---|---:|---|---:|---:|---:|"]
            order = {"etf": 0, "actions": 1, "crypto": 2}
            labels = {"etf": "ETF", "actions": "Actions", "crypto": "Crypto"}
            for s in sorted(self.signals, key=lambda s: (order.get(s.sleeve, 9), -s.momentum)):
                trend = "↗ haussière" if s.trend_up else ("→ neutre" if s.above_long else "↘ baissière")
                L.append(f"| {labels.get(s.sleeve, s.sleeve)} | {names.get(s.key, s.key)} | {eur(s.price)} | {trend} | {s.rsi:.0f} | "
                         f"{pct(s.momentum)} | {pct(s.drawdown)} |")
            L.append("")

        if self.kid_links:
            L += ["## Documents d'informations clés (KID) des ETF achetés", ""]
            for key, links in self.kid_links.items():
                for link in links:
                    L.append(f"- [{names.get(key, key)}]({link})")
            L.append("")

        L += ["---", "_Journal généré automatiquement. Ceci n'est pas un conseil en investissement._", ""]
        return "\n".join(L)


def write_journal(report: RunReport, names: dict, base: Path | None = None) -> Path:
    folder = (base or JOURNAL_DIR) / report.mode
    folder.mkdir(parents=True, exist_ok=True)
    text = report.to_markdown(names)
    path = folder / f"{report.today.isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    (folder / "DERNIER.md").write_text(text, encoding="utf-8")
    return path


def append_trades(report: RunReport, assets: dict, base: Path | None = None) -> Path:
    folder = (base or JOURNAL_DIR) / report.mode
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "operations.csv"
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        if new:
            w.writerow(TRADES_HEADER)
        for o, f in report.executed:
            a = assets.get(o.key, {})
            w.writerow([report.today.isoformat(), report.mode, "ACHAT" if o.side == "BUY" else "VENTE", o.key,
                        a.get("name", ""), a.get("isin") or a.get("symbol", ""), f"{f.quantity:.8f}",
                        f"{f.price:.4f}", f"{f.notional:.2f}", f"{f.fee:.2f}", f.trade_id, o.reason])
    return path
