"""Notifications téléphone via ntfy.sh (gratuit, appli ntfy sur Android/iOS). Optionnel."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def push(title: str, message: str, priority: int = 3, topic: str | None = None) -> bool:
    topic = topic or os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    try:
        r = requests.post("https://ntfy.sh/", timeout=15, json={
            "topic": topic, "title": title[:120], "message": message[:3500], "priority": priority,
            "tags": ["chart_with_upwards_trend"],
        })
        r.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Notification ntfy impossible : %s", exc)
        return False
