#!/usr/bin/env python3
"""Daily ingest entry point.  python -m ingest.run [--skip opbounty,kaizoku,optcgone]

Writes data/latest/*.json and appends data/history/*.  Raw payloads land in raw/ (gitignored,
uploaded as a CI artifact).  Any single source failing is logged and the rest still runs; the
previous day's files for that source are left untouched.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import normalize as N
from .http import get_json
from .sources import kaizoku, kaizoku_players, opbounty, optcgone

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
LATEST = ROOT / "data" / "latest"
HISTORY = ROOT / "data" / "history"


def dump(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))


def load(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", default="", help="comma list: opbounty,kaizoku,optcgone,cards,players")
    args = ap.parse_args(argv)
    skip = set(filter(None, args.skip.split(",")))

    RAW.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    t0 = time.time()
    status: dict[str, str] = {}
    raw = {}

    def step(name, fn):
        if name in skip:
            status[name] = "skipped"
            raw[name] = load(RAW / f"{name}.json", {})
            return
        try:
            raw[name] = fn()
            dump(RAW / f"{name}.json", raw[name])
            status[name] = "ok"
        except Exception as e:  # keep going; site rebuilds from the other sources
            traceback.print_exc()
            status[name] = f"error: {e}"
            raw[name] = load(RAW / f"{name}.json", {})
        print(f"[{name}] {status[name]} ({time.time() - t0:.0f}s)", file=sys.stderr)

    step("kaizoku", lambda: kaizoku.fetch_all(RAW / "kaizoku_state.json", RAW))
    step("optcgone", optcgone.fetch_all)
    step("cards", lambda: get_json(f"{kaizoku.CDN}/card_data.json"))
    step("opbounty", opbounty.fetch_all)

    opb, kz, one = raw["opbounty"], raw["kaizoku"], raw["optcgone"]
    card_db = N.build_card_db(raw["cards"]) if raw.get("cards") else load(LATEST / "cards.json", {})

    players = N.build_players(opb)
    leaders = N.build_leaders(opb, kz, card_db)
    used_cards: set[str] = set(leaders)

    for code in leaders:
        decks = N.build_decks(code, opb, kz, one)
        cards = N.build_cards(code, kz)
        dump(LATEST / "decks" / f"{code}.json", decks)
        dump(LATEST / "cards" / f"{code}.json", cards)
        for d in decks["decks"]:
            used_cards.update(c["id"] for c in d["cards"])
        used_cards.update(c["id"] for c in cards["cards"])
        leaders[code]["deckCount"] = len(decks["decks"])
        leaders[code]["cardCount"] = len(cards["cards"])
        leaders[code]["matchups"] = dict(sorted(
            leaders[code]["matchups"].items(),
            key=lambda kv: -max((s.get("games") or 0) for s in kv[1].values())))

    for p in players:
        for t in p["topLeaders"]:
            used_cards.add(t["code"])

    # per-player exact decklists (Card Kaizoku player routes)
    player_stats = None
    if "players" in skip:
        status["players"] = "skipped"
    else:
        try:
            player_stats = kaizoku_players.fetch_all(players, LATEST / "player_ids.json", LATEST / "players", today)
            status["players"] = "ok"
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            status["players"] = f"error: {e}"
        print(f"[players] {status['players']} {json.dumps(player_stats) if player_stats else ''} ({time.time() - t0:.0f}s)", file=sys.stderr)
    # drop per-player files for players no longer in the pulled range, so stale decks never linger
    keep = {str(p["id"]) for p in players if p.get("id")}
    if (LATEST / "players").exists():
        for f in (LATEST / "players").glob("*.json"):
            if f.stem not in keep:
                f.unlink()
    for f in (LATEST / "players").glob("*.json") if (LATEST / "players").exists() else []:
        for d in load(f, {}).get("decks", []):
            used_cards.update(c["id"] for c in d["cards"])

    dump(LATEST / "players.json", players)
    dump(LATEST / "leaders.json", leaders)
    dump(LATEST / "cards.json", {cid: card_db[cid] for cid in sorted(used_cards) if cid in card_db})

    # history
    if status.get("opbounty") == "ok":
        for p in players:
            if p["id"]:
                N.append_history(HISTORY / "bounty" / f"{p['id']}.json", [today, p["bounty"], p["rank"]])
    for code, L in leaders.items():
        k = L["sources"].get("kaizoku") or {}
        o = L["sources"].get("opbounty") or {}
        N.append_history(HISTORY / "leaders" / f"{code}.json",
                         [today, k.get("winRate"), k.get("playRate"), k.get("matches"), o.get("winRate"), o.get("popularity")])

    meta = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": today,
        "status": status,
        "players": len(players),
        "leaders": len(leaders),
        "opbounty": {"set": opb.get("set"), "sets": opb.get("sets")},
        "kaizoku": {"dataset": kz.get("dataset"), "manifestDate": kz.get("manifest_date"), "files": kz.get("files")},
        "optcgone": {"fetchedAt": one.get("fetched_at"), "rankedCount": one.get("ranked_count")},
        "playerDecks": player_stats,
        "sources": [
            {"name": "OPBounty", "url": "https://stats.tcgmatchmaking.com/", "support": "https://www.patreon.com/tcgmm"},
            {"name": "Card Kaizoku", "url": "https://www.cardkaizoku.com/", "support": "https://www.patreon.com/cardkaizoku"},
            {"name": "optcg.one", "url": "https://www.optcg.one/"},
        ],
    }
    dump(LATEST / "meta.json", meta)
    print(json.dumps(meta["status"]), file=sys.stderr)
    return 0 if any(v == "ok" for v in status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
