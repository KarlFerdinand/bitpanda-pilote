"""Grand livre du budget : crédit hebdomadaire, report du non-dépensé, plafond de réserve."""
from __future__ import annotations

from datetime import date


def iso_week(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def _monday(week: str) -> date:
    year, w = week.split("-W")
    return date.fromisocalendar(int(year), int(w), 1)


def weeks_between(older: str, newer: str) -> int:
    return (_monday(newer) - _monday(older)).days // 7


class Ledger:
    """Vit dans state["ledger"] :
    week          semaine ISO du dernier crédit
    reserve       € disponibles pour acheter (budget non dépensé + produits de ventes)
    spent_week    € dépensés depuis lundi
    credited_total cumul des budgets crédités
    """

    def __init__(self, data: dict):
        self.d = data

    @property
    def reserve(self) -> float:
        return float(self.d.get("reserve", 0.0))

    @property
    def spent_week(self) -> float:
        return float(self.d.get("spent_week", 0.0))

    def roll(self, today: date, weekly: float, max_weeks: float) -> str | None:
        """Crédite le budget de la semaine si on a changé de semaine. Renvoie une note ou None."""
        week = iso_week(today)
        if not self.d.get("week"):
            self.d.update(week=week, reserve=round(weekly, 2), spent_week=0.0, credited_total=round(weekly, 2))
            return f"Premier lancement : {weekly:.2f} € crédités pour la semaine {week}."
        if self.d["week"] == week:
            return None
        n = weeks_between(self.d["week"], week)
        if n <= 0:
            return None
        before = self.reserve
        cap = max_weeks * weekly
        after = before + n * weekly
        if after > cap:
            after = max(cap, before)  # on ne rogne jamais ce qui existe déjà (ex. produit de ventes)
        credited = round(after - before, 2)
        self.d.update(week=week, reserve=round(after, 2), spent_week=0.0)
        self.d["credited_total"] = round(float(self.d.get("credited_total", 0.0)) + credited, 2)
        if credited < n * weekly - 0.005:
            return (f"Nouvelle semaine {week} : {credited:.2f} € crédités "
                    f"(réserve plafonnée à {cap:.2f} €).")
        return f"Nouvelle semaine {week} : {credited:.2f} € crédités."

    def spend(self, amount: float) -> None:
        self.d["reserve"] = round(self.reserve - amount, 2)
        self.d["spent_week"] = round(self.spent_week + amount, 2)

    def add_proceeds(self, amount: float) -> None:
        self.d["reserve"] = round(self.reserve + amount, 2)
