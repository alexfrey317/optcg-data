"""Card-text flags the replay viewer needs but the sim's combat log never records.

The sim taps a card whose ability cost is "rest this card" without writing a log line (GameplayLogicScript's
SelfTap path), so the viewer infers that rest from the card text: a card is flagged when any of its abilities has
"rest this Leader/Character/card" as a cost (followed by ':' or joined with 'and'), ignoring the Blocker reminder
text.  Text comes from dotgg's public card index (the same CDN the viewer loads card art from).

Output: data/latest/card_flags.json  {"restCost": ["OP01-016", ...]}  (sorted; the previous file is kept on failure)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..http import get_json

URL = "https://api.dotgg.gg/cgfw/getcards?game=onepiece&mode=indexed"
REST_COST = re.compile(r"rest this (?:Leader|Character|card)\s*(?::|and\b)", re.I)
BLOCKER_REMINDER = re.compile(r"\(After your opponent declares an attack, you may rest this card to make it the new target of the attack\.\)", re.I)
CODE = re.compile(r"^[A-Z]{1,3}\d{2}-\d{3}$")


def rest_cost_ids(payload: dict) -> list[str]:
    names = payload["names"]
    i_id, i_eff = names.index("id"), names.index("Effect")
    out: set[str] = set()
    for row in payload["data"]:
        cid, eff = row[i_id], row[i_eff]
        if not cid or not eff or not CODE.match(cid):
            continue
        if REST_COST.search(BLOCKER_REMINDER.sub("", eff.replace("<br>", " "))):
            out.add(cid)
    return sorted(out)


def card_db(payload: dict) -> dict:
    """Every card in dotgg's index in the shape of normalize.build_card_db (no image: the site derives art from the
    id). Kaizoku's database lags new sets by weeks, so this fills in the unreleased cards the spoiler queue plays."""
    names = payload["names"]
    col = {k: names.index(k) for k in ("id", "rarity", "cardType", "name", "Cost", "Power", "Counter", "Color", "set")}
    db = {}
    for row in payload["data"]:
        cid = row[col["id"]]
        if not cid or not CODE.match(cid) or cid in db:
            continue
        db[cid] = {"name": row[col["name"]], "type": (row[col["cardType"]] or "").upper(), "cost": row[col["Cost"]],
                   "color": row[col["Color"]], "power": row[col["Power"]], "counter": row[col["Counter"]],
                   "rarity": row[col["rarity"]], "img": None, "set": row[col["set"]] or cid.split("-")[0]}
    return db


def fetch_all(out: Path, cards_out: Path | None = None) -> dict:
    payload = get_json(URL)
    ids = rest_cost_ids(payload)
    if len(ids) < 50:  # the index normally has a few hundred such cards; a tiny result means a changed format
        raise RuntimeError(f"only {len(ids)} rest-cost cards found")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"restCost": ids}, separators=(",", ":")))
    db = card_db(payload)
    if cards_out and len(db) > 1000:
        cards_out.write_text(json.dumps(db, separators=(",", ":")))
    return {"restCost": len(ids), "cards": len(db)}
