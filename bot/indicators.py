"""Indicateurs techniques : moyennes mobiles, RSI, momentum, volatilité, recul."""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

MIN_HISTORY = 60  # en dessous, pas assez de recul pour juger


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    """RSI de Wilder (0-100)."""
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + gain / loss)
    out = out.where(loss != 0, 100.0)               # que des hausses
    out = out.where(~((loss == 0) & (gain == 0)), 50.0)  # aucun mouvement
    return out.where(gain.notna())


def _last(series: pd.Series) -> float | None:
    if series.empty:
        return None
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


@dataclass
class Signal:
    key: str
    sleeve: str
    price: float
    sma_short: float | None
    sma_long: float | None
    rsi: float
    momentum: float          # rendement moyen sur ~1/3/6 mois
    volatility: float        # volatilité annualisée (20 j)
    drawdown: float          # recul depuis le plus haut 3 mois (≤ 0)
    days: int                # nb de séances d'historique

    @property
    def above_long(self) -> bool:
        ref = self.sma_long if self.sma_long is not None else self.sma_short
        return ref is not None and self.price > ref

    @property
    def trend_up(self) -> bool:
        if self.sma_long is not None and self.sma_short is not None:
            return self.price > self.sma_long and self.sma_short > self.sma_long
        return self.sma_short is not None and self.price > self.sma_short

    def is_dip(self, rsi_dip: float) -> bool:
        return self.trend_up and self.rsi < rsi_dip

    def is_hot(self, rsi_hot: float) -> bool:
        return self.rsi > rsi_hot


def compute_signal(key: str, sleeve: str, closes: pd.Series | None, params: dict) -> Signal | None:
    if closes is None:
        return None
    s = pd.Series(closes).dropna().astype(float)
    s = s[s > 0]
    if len(s) < MIN_HISTORY:
        return None

    price = float(s.iloc[-1])
    sma_short = _last(sma(s, int(params["sma_short"])))
    sma_long = _last(sma(s, int(params["sma_long"])))
    rsi_value = _last(rsi(s, int(params["rsi_period"])))

    rets = []
    for w in params["momentum_windows"]:
        w = int(w)
        if len(s) > w:
            rets.append(price / float(s.iloc[-1 - w]) - 1)
    momentum = sum(rets) / len(rets) if rets else 0.0

    periods = 365 if sleeve == "crypto" else 252
    daily = s.pct_change().dropna().tail(20)
    vol = float(daily.std() * math.sqrt(periods)) if len(daily) > 2 else 0.0

    high = float(s.tail(63).max())
    drawdown = price / high - 1 if high > 0 else 0.0

    return Signal(
        key=key,
        sleeve=sleeve,
        price=price,
        sma_short=sma_short,
        sma_long=sma_long,
        rsi=rsi_value if rsi_value is not None else 50.0,
        momentum=momentum,
        volatility=vol if not math.isnan(vol) else 0.0,
        drawdown=drawdown,
        days=len(s),
    )
