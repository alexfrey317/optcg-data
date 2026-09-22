"""OPBounty published ranked statistics (public CloudFront bucket), used with OPBounty's permission.

OPBounty's server aggregates every ranked game into stats files, roughly one per six hours per rank
bracket, at https://d2spmnr3w7rm2f.cloudfront.net/stats/raw/<day>/mode_0/<bracket>/statsNNNN.json
(index: /stats/files.json).  Each file is base64+gzip JSON with, for its games:
  number_of_matches                        games in the chunk (each game once, both sides below)
  leaders_presence[]                       per leader: games, wins, first/second records, duration, and
                                           per-opponent games/wins/first/second (subject* arrays)
  decklists[].lists[]                      every distinct 50-card list: games, wins, first/second, duration
  cards_presence[]                         per leader: copies of each card across its games (unused here)
Unlike the replay archive this is complete: it is the same data OPBounty's own leader pages show.
It has no player identities; the archive supplies pilots.

data/stats/YYYY-MM-DD.json.gz  (never deleted)
  {"date", "chunks":[keys merged so far], "final", "matches",
   "brackets": {bracket: {"matches", "leaders": {code: {"g","w","fw","fl","sw","sl","dur",
                                                        "mu": {opp: [g, w, fw, fl, sw, sl]}}}}},
   "decks": {hash: {"l": leader, "g","w","fw","fl","sw","sl"}}, "deckText": {hash: "4xOP01-016 ..."},
   "cards": {leader: {"NxCODE": [games, wins, keepFirst, keepSecond, winsInHandFirst, lossesInHandFirst,
                                 winsInHandSecond, lossesInHandSecond]}}}
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import normalize as N
from ..http import request

BASE = "https://d2spmnr3w7rm2f.cloudfront.net/stats"
MODE = "mode_0"
DAYS = int(os.environ.get("OPB_STATS_DAYS", "3"))
WORKERS = int(os.environ.get("OPB_STATS_WORKERS", "8"))
REFRESH = os.environ.get("OPB_STATS_REFRESH") == "1"
ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "data" / "stats"


def _decode(raw: bytes):
    try:
        obj = json.loads(raw)
        if isinstance(obj, str):
            obj = json.loads(gzip.decompress(base64.b64decode(obj)))
        return obj
    except (ValueError, UnicodeDecodeError):
        return json.loads(gzip.decompress(base64.b64decode(raw)))


def index() -> dict[str, list[str]]:
    """day -> [keys] for ranked (mode_0) chunks."""
    _, _, raw = request(f"{BASE}/files.json")
    by_day: dict[str, list[str]] = defaultdict(list)
    for item in json.loads(raw):
        k = item.get("Key") or ""
        parts = k.split("/")
        if len(parts) == 4 and parts[1] == MODE:
            by_day[parts[0]].append(k)
    return {d: sorted(ks) for d, ks in by_day.items()}


def fetch_chunk(key: str) -> dict:
    _, _, raw = request(f"{BASE}/raw/{key}", retries=3)
    return _decode(raw)


def _code(x) -> str | None:
    if not isinstance(x, str) or not x or "obile" in x:
        return None
    return N.strip_prefix(x)


def day_path(day: str) -> Path:
    return OUT / f"{day}.json.gz"


def load_day(day: str) -> dict | None:
    p = day_path(day)
    return json.loads(gzip.decompress(p.read_bytes())) if p.exists() else None


def save_day(obj: dict):
    OUT.mkdir(parents=True, exist_ok=True)
    day_path(obj["date"]).write_bytes(gzip.compress(json.dumps(obj, separators=(",", ":")).encode(), mtime=0))


def empty_day(day: str) -> dict:
    return {"date": day, "chunks": [], "final": False, "matches": 0, "brackets": {}, "decks": {}, "deckText": {}, "cards": {}}


def merge_chunk(day_obj: dict, key: str, chunk: dict):
    """Add one stats file to the day aggregate (all counters are additive)."""
    bracket = key.split("/")[2]
    B = day_obj["brackets"].setdefault(bracket, {"matches": 0, "leaders": {}})
    n = int(chunk.get("number_of_matches") or 0)
    B["matches"] += n
    day_obj["matches"] += n
    for lp in chunk.get("leaders_presence") or []:
        code = _code(lp.get("leader"))
        if not code:
            continue
        L = B["leaders"].setdefault(code, {"g": 0, "w": 0, "fw": 0, "fl": 0, "sw": 0, "sl": 0, "dur": 0.0, "mu": {}})
        L["g"] += int(lp.get("number_of_matches") or 0); L["w"] += int(lp.get("wins") or 0)
        L["fw"] += int(lp.get("first_wins") or 0); L["fl"] += int(lp.get("first_losses") or 0)
        L["sw"] += int(lp.get("second_wins") or 0); L["sl"] += int(lp.get("second_losses") or 0)
        L["dur"] = round(L["dur"] + float(lp.get("duration") or 0), 1)
        subj = lp.get("subject") or []
        arrs = [lp.get(k) or [] for k in ("presence", "subject_wins", "subject_first_wins", "subject_first_losses", "subject_second_wins", "subject_second_losses")]
        for i, s in enumerate(subj):
            opp = _code(s)
            if not opp:
                continue
            row = L["mu"].setdefault(opp, [0, 0, 0, 0, 0, 0])
            for j, arr in enumerate(arrs):
                if i < len(arr):
                    row[j] += int(arr[i] or 0)
    for cp in chunk.get("cards_presence") or []:
        code = _code(cp.get("leader"))
        if not code:
            continue
        C = day_obj.setdefault("cards", {}).setdefault(code, {})
        subj = cp.get("subject") or []
        arrs = [cp.get(k) or [] for k in ("subject_matches", "subject_wins", "subject_keep_first", "subject_keep_second",
                                             "subject_wins_in_hand_first", "subject_losses_in_hand_first",
                                             "subject_wins_in_hand_second", "subject_losses_in_hand_second")]
        for i, s in enumerate(subj):
            if not isinstance(s, str) or "obile" in s or s == cp.get("leader"):
                continue
            row = C.setdefault(s, [0] * 8)   # key is 'NxCODE': games with exactly N copies
            for j, arr in enumerate(arrs):
                if i < len(arr):
                    row[j] += int(arr[i] or 0)
    for dl in chunk.get("decklists") or []:
        code = _code(dl.get("leader"))
        if not code:
            continue
        for lst in dl.get("lists") or []:
            cards = N.parse_deck([str(c) for c in (lst.get("deck") or [])], leader=code)
            if not cards:
                continue
            h = N.deck_hash(cards)
            D = day_obj["decks"].setdefault(h, {"l": code, "g": 0, "w": 0, "fw": 0, "fl": 0, "sw": 0, "sl": 0})
            D["g"] += int(lst.get("number_of_matches") or 0); D["w"] += int(lst.get("wins") or 0)
            D["fw"] += int(lst.get("first_wins") or 0); D["fl"] += int(lst.get("first_losses") or 0)
            D["sw"] += int(lst.get("second_wins") or 0); D["sl"] += int(lst.get("second_losses") or 0)
            day_obj["deckText"].setdefault(h, " ".join("%dx%s" % (c["qty"], c["id"]) for c in cards))
    day_obj["chunks"].append(key)


def fetch_all(days: int = DAYS) -> dict:
    """Bring the last `days` UTC days up to date with the published chunk index. Incremental: only
    chunks not yet merged are downloaded. A day is final once it is at least two days old (its last
    chunk lands in the early hours of the next day)."""
    idx = index()
    today = datetime.now(timezone.utc).date()
    status = {}
    for back in range(days - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        obj = load_day(day) or empty_day(day)
        if obj.get("final") and not REFRESH:
            status[day] = {"matches": obj["matches"], "chunks": len(obj["chunks"]), "cached": True}
            continue
        if REFRESH and obj["chunks"]:
            obj = empty_day(day)
        todo = [k for k in idx.get(day, []) if k not in set(obj["chunks"])]
        if todo:
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                for key, chunk in zip(todo, ex.map(fetch_chunk, todo)):
                    merge_chunk(obj, key, chunk)
        obj["final"] = day <= (today - timedelta(days=2)).isoformat()
        save_day(obj)
        status[day] = {"matches": obj["matches"], "chunks": len(obj["chunks"]), "new": len(todo), "decks": len(obj["decks"])}
    return status


def load_days(days: int) -> list[dict]:
    files = sorted(OUT.glob("*.json.gz"))[-days:] if OUT.exists() else []
    return [json.loads(gzip.decompress(f.read_bytes())) for f in files]


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else DAYS
    print(json.dumps(fetch_all(n), indent=1))
