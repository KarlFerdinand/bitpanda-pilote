"""État persistant du bot (un fichier JSON par mode, versionné dans le dépôt)."""
from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT

STATE_DIR = ROOT / "state"


def state_path(mode: str, base: Path | None = None) -> Path:
    return (base or STATE_DIR) / f"{mode}.json"


def empty_state() -> dict:
    return {
        "ledger": {},
        "pending": {"etf": 0.0, "crypto": 0.0},   # montants accumulés en attente d'un ordre mini
        "positions_meta": {},                     # 1er achat, plus haut atteint, coût moyen…
        "paper": {"positions": {}},               # portefeuille fictif (mode simulation)
        "asset_ids": {},                          # cache des identifiants Bitpanda
        "last_run": None,
    }


def load_state(mode: str, base: Path | None = None) -> dict:
    path = state_path(mode, base)
    if not path.exists():
        return empty_state()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    merged = empty_state()
    merged.update(data)
    return merged


def save_state(state: dict, mode: str, base: Path | None = None) -> Path:
    path = state_path(mode, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False, sort_keys=True)
    tmp.replace(path)
    return path
