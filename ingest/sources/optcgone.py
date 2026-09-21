"""optcg.one static data. Explicitly free ("no accounts, no paywalls"). One file."""
from __future__ import annotations

from ..http import get_json

URL = "https://www.optcg.one/data/decks-by-leader.json"


def fetch_all() -> dict:
    return get_json(URL)
