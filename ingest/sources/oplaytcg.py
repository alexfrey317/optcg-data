"""Merge an OPlayTCG collector dump (see ingest/oplaytcg_collect.js) into data/latest/oplaytcg.json.

    python -m ingest.sources.oplaytcg dump.json

The dump is produced by hand in a browser (Cloudflare blocks unattended clients), so this is not part of the daily
run: the merged file is committed and the site reads it. Decks are keyed by (tournament, user); ladder ranks are
refreshed from the dump's ladder for every stored deck, so old decks follow their pilot's current rank.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "data" / "latest" / "oplaytcg.json"


def merge(existing: dict, dump: dict, today: str) -> dict:
    decks = {(d["t"], d["user"]): d for d in existing.get("decks", [])}
    for d in dump["decks"]:
        decks[(d["t"], d["user"])] = d
    ladder = dump.get("ladder") or {}
    for d in decks.values():
        row = ladder.get(d["user"])
        d["rank"] = row["rank"] if row else None
        d["rating"] = row["rating"] if row else None
    tournaments = {t["id"]: t for t in existing.get("tournaments", [])}
    tournaments.update({t["id"]: t for t in dump["tournaments"]})
    lo = min(existing.get("scanned", dump["scanned"])[0], dump["scanned"][0])
    hi = max(existing.get("scanned", dump["scanned"])[1], dump["scanned"][1])
    return {"updated": today, "scanned": [lo, hi], "ladderTop": dump["ladderTop"],
            "tournaments": sorted(tournaments.values(), key=lambda t: t["id"]),
            "decks": sorted(decks.values(), key=lambda d: (d["t"], d["user"]))}


def main(argv: list[str]) -> None:
    dump = json.loads(Path(argv[0]).read_text())
    while isinstance(dump, str):  # browser exports arrive JSON-encoded once more
        dump = json.loads(dump)
    existing = json.loads(OUT.read_text()) if OUT.exists() else {}
    out = merge(existing, dump, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    OUT.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    print(f"{len(out['decks'])} decks from {len(out['tournaments'])} tournaments, ids {out['scanned'][0]}-{out['scanned'][1]}")


if __name__ == "__main__":
    main(sys.argv[1:])
