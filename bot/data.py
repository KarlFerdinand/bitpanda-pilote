"""Sources de marché quotidiennes : historiques de cours (convertis en euros), VIX, Fear & Greed crypto."""
from __future__ import annotations

import logging

import pandas as pd
import requests

log = logging.getLogger(__name__)

FNG_URL = "https://api.alternative.me/fng/?limit=1"


def daily_index(series: pd.Series) -> pd.Series:
    """Indexe une série par date calendaire (sans fuseau), une valeur par jour."""
    s = series.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(s.index))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    s.index = idx.normalize()
    return s[~s.index.duplicated(keep="last")].sort_index()


def to_eur(prices: pd.Series, eur_fx: pd.Series) -> pd.Series:
    """Convertit des cours en devise X en euros avec la paire EURX=X (nombre de X pour 1 €)."""
    p, fx = daily_index(prices), daily_index(eur_fx)
    fx = fx.reindex(p.index.union(fx.index)).ffill().reindex(p.index)
    return (p / fx).dropna()


class MarketData:
    """Lit les données publiques. Remplaçable par un faux fournisseur dans les tests."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self._cache: dict[str, pd.Series | None] = {}

    def history(self, ticker: str) -> pd.Series | None:
        """Cours de clôture quotidiens (2 ans) via Yahoo Finance, dans la devise de cotation."""
        if ticker in self._cache:
            return self._cache[ticker]
        series = None
        try:
            import yfinance as yf

            df = yf.Ticker(ticker).history(period="2y", interval="1d", auto_adjust=True)
            if df is not None and not df.empty and "Close" in df:
                series = df["Close"].dropna()
        except Exception as exc:  # réseau, ticker inconnu, format changé…
            log.warning("Historique indisponible pour %s : %s", ticker, exc)
        if series is None or series.empty:
            log.warning("Aucune donnée de cours pour %s", ticker)
            series = None
        self._cache[ticker] = series
        return series

    def history_eur(self, ticker: str, currency: str = "EUR") -> pd.Series | None:
        prices = self.history(ticker)
        currency = (currency or "EUR").upper()
        if prices is None or currency == "EUR":
            return prices
        fx = self.history(f"EUR{currency}=X")
        if fx is None:
            log.warning("Taux de change EUR/%s indisponible : %s ignoré", currency, ticker)
            return None
        return to_eur(prices, fx)

    def vix(self) -> float | None:
        s = self.history("^VIX")
        return float(s.iloc[-1]) if s is not None else None

    def crypto_fear_greed(self) -> int | None:
        try:
            r = self.session.get(FNG_URL, timeout=15)
            r.raise_for_status()
            return int(r.json()["data"][0]["value"])
        except Exception as exc:
            log.warning("Indice Fear & Greed crypto indisponible : %s", exc)
            return None
