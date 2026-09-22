"""OPBounty personal profiles (Firestore `PublicUsers/<ladderId>`), used with OPBounty's permission.

The OPBounty app keeps one document per player.  Its `Western` field (the main ranked ladder) is a
gzip+base64 JSON blob the player's own client writes whenever they open their profile:
  Wins / Losses / Winrate / Duration        season record over every ranked game
  Leaders[≤3]                               games, wins, losses, first/second records for the top 3 leaders
  Graph.y                                   bounty after every game, oldest first (no timestamps)
  Public_matches[≤9]                        newest games: both decklists, bounties, deltas, timestamp, duration
  Timestamp                                 when the blob was written (unix seconds)

Unlike the replay archive this is complete for the player, so it is the source of truth for how much
and how well a player plays each leader.  It only exists for players who use the app (about 93% of
the top 1000) and lags until they next open it.

Outputs:
  data/latest/profiles/<ladderId>.json   compact profile (see shape())
  data/matches/public/YYYY-MM-DD.json    every public match row ever seen, keyed by match index (never deleted)
"""
from __future__ import annotations

import base64
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import normalize as N
from ..http import request
from .opbounty_matches import FS, PROJECT, Client

ROOT = Path(__file__).resolve().parent.parent.parent
PROFILES = ROOT / "data" / "latest" / "profiles"
PUBLIC = ROOT / "data" / "matches" / "public"
BATCH = 100
MIN_MATCH_TIME = 15  # seconds; the app ignores shorter "wins" (opponent never really joined)
MODE = 0             # Western = standard ranked


def _unpack(s: str):
    """The blob is Godot's str(Dictionary): JSON-like, but with nan, <null> and \\' escapes."""
    txt = gzip.decompress(base64.b64decode(s)).decode("utf-8", errors="replace")
    txt = txt.replace(": nan", ": null").replace(": inf", ": null").replace("<null>", "null").replace("\\'", "'")
    return json.loads(txt)


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _code(x) -> str | None:
    """'MY1xOP17-039' / '1xOP17-039' -> 'OP17-039'."""
    if not isinstance(x, str) or not x:
        return None
    x = x[2:] if x.startswith("MY") else x
    return N.strip_prefix(x)


def _deck(arr, decks: dict) -> tuple[str | None, str | None]:
    """(leader, deck hash) from a Public_matches deck array; registers the list text in `decks`."""
    if not isinstance(arr, list) or not arr:
        return None, None
    leader = _code(arr[0])
    cards = N.parse_deck([str(c)[2:] if str(c).startswith("MY") else str(c) for c in arr], leader=leader)
    if not cards:
        return leader, None
    h = N.deck_hash(cards)
    decks.setdefault(h, N.deck_text(cards))
    return leader, h


def _outcome(me: str, opp: str) -> bool | None:
    lost_states = ("loss", "dc", "selfdc")
    if me == "win" and opp in lost_states:
        return True
    if me in lost_states and opp == "win":
        return False
    return None  # cancelled / joined / both dc: not a game


def shape_match(m: list, decks: dict) -> dict | None:
    """Public_matches row -> {idx, ts, dur, mode, p1:{id,nick,b,delta,status,leader,deck}, p2:{...}, winner}."""
    if not isinstance(m, list) or len(m) < 26:
        return None
    l1, d1 = _deck(m[17], decks)
    l2, d2 = _deck(m[18], decks)
    won1 = _outcome(str(m[5]), str(m[11]))
    return {
        "idx": int(_num(m[12])), "ts": str(m[16]), "dur": round(_num(m[14]), 1), "mode": int(_num(m[15])),
        "p1": {"id": str(m[24]), "nick": N.clean_name(str(m[1])), "b": round(_num(m[2]), 2), "delta": round(_num(m[20]), 2), "status": str(m[5]), "leader": l1, "deck": d1},
        "p2": {"id": str(m[25]), "nick": N.clean_name(str(m[7])), "b": round(_num(m[8]), 2), "delta": round(_num(m[21]), 2), "status": str(m[11]), "leader": l2, "deck": d2},
        "winner": None if won1 is None else ("p1" if won1 else "p2"),
    }


def shape(uid: str, blob: dict, updated: str | None) -> dict:
    decks: dict[str, str] = {}
    leaders = []
    for L in blob.get("Leaders") or []:
        if not isinstance(L, dict):
            continue
        code = _code(L.get("Leader"))
        if not code or "obile" in code:
            continue
        g, w = int(L.get("Games") or 0), int(L.get("Wins") or 0)
        fw, fl = L.get("first_wins"), L.get("first_losses")
        sw, sl = L.get("second_wins"), L.get("second_losses")
        leaders.append({
            "code": code, "games": g, "wins": w, "losses": int(L.get("Losses") or 0),
            "winRate": N.rate(w, g),
            "avgDuration": round(float(L.get("Duration") or 0) / g, 1) if g else None,
            "first": None if fw is None else {"games": int(fw) + int(fl or 0), "winRate": N.rate(int(fw), int(fw) + int(fl or 0))},
            "second": None if sw is None else {"games": int(sw) + int(sl or 0), "winRate": N.rate(int(sw), int(sw) + int(sl or 0))},
        })
    recent = []
    for m in blob.get("Public_matches") or []:
        try:
            r = shape_match(m, decks)
        except Exception as e:  # noqa: BLE001 - one odd row must not drop the profile
            print(f"[profiles] bad match row for {uid}: {e!r}", file=sys.stderr)
            continue
        if r and r["mode"] == MODE:
            recent.append(r)
    ts = blob.get("Timestamp")
    wins, losses = int(blob.get("Wins") or 0), int(blob.get("Losses") or 0)
    graph = [round(float(v), 1) for v in ((blob.get("Graph") or {}).get("y") or []) if isinstance(v, (int, float))]
    return {
        "id": uid, "updated": (updated or "")[:19],
        "writtenAt": datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")[:19] if ts else None,
        "wins": wins, "losses": losses, "games": wins + losses,
        "winRate": N.rate(wins, wins + losses),
        "avgDuration": blob.get("Duration") if isinstance(blob.get("Duration"), (int, float)) else None,
        "leaders": sorted(leaders, key=lambda x: -x["games"]),
        "graph": graph, "recent": recent, "decks": decks,
    }


def batch_get(client: Client, ids: list[str]) -> dict[str, dict]:
    client._auth()
    out = {}
    for i in range(0, len(ids), BATCH):
        body = json.dumps({"documents": [f"projects/{PROJECT}/databases/(default)/documents/PublicUsers/{x}" for x in ids[i:i + BATCH]],
                           "mask": {"fieldPaths": ["Western", "Position"]}}).encode()
        _, _, raw = request(f"{FS}:batchGet", method="POST", body=body,
                            headers={"Content-Type": "application/json", "Authorization": f"Bearer {client.token}"})
        for r in json.loads(raw):
            doc = r.get("found")
            if doc:
                out[doc["name"].rsplit("/", 1)[-1]] = doc
    return out


def record_public(rows: list[dict], decks: dict):
    """Append public match rows to the permanent per-day store (keyed by match index)."""
    PUBLIC.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["ts"][:10], []).append(r)
    added = 0
    for day, rs in by_day.items():
        p = PUBLIC / f"{day}.json"
        obj = json.loads(p.read_text()) if p.exists() else {"date": day, "matches": {}, "decks": {}}
        for r in rs:
            k = str(r["idx"])
            if k not in obj["matches"]:
                added += 1
            obj["matches"][k] = r
            for side in ("p1", "p2"):
                h = r[side]["deck"]
                if h and h in decks:
                    obj["decks"][h] = decks[h]
        p.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    return added


def fetch_all(players: list[dict]) -> dict:
    client = Client()
    ids = [str(p["id"]) for p in players if p.get("id") is not None]
    docs = batch_get(client, ids)
    PROFILES.mkdir(parents=True, exist_ok=True)
    keep, fresh, rows, decks = set(), 0, [], {}
    now = datetime.now(timezone.utc)
    for uid, doc in docs.items():
        ws = (doc.get("fields") or {}).get("Western", {}).get("stringValue")
        if not ws:
            continue
        try:
            prof = shape(uid, _unpack(ws), doc.get("updateTime"))
        except Exception as e:  # noqa: BLE001 - one odd blob must not sink the step
            print(f"[profiles] bad blob for {uid}: {e!r}", file=sys.stderr)
            continue
        (PROFILES / f"{uid}.json").write_text(json.dumps(prof, separators=(",", ":"), ensure_ascii=False))
        keep.add(f"{uid}.json")
        if prof["writtenAt"] and (now - datetime.fromisoformat(prof["writtenAt"]).replace(tzinfo=timezone.utc)).days < 7:
            fresh += 1
        rows.extend(prof["recent"])
        decks.update(prof["decks"])
    for f in PROFILES.glob("*.json"):
        if f.name not in keep:
            f.unlink()  # dropped out of the pulled ladder range
    added = record_public(rows, decks)
    return {"ladder": len(ids), "docs": len(docs), "profiles": len(keep), "freshWeek": fresh, "publicRows": len(rows), "publicNew": added}


if __name__ == "__main__":
    players = json.loads((ROOT / "data" / "latest" / "players.json").read_text())
    print(json.dumps(fetch_all(players), indent=1))
