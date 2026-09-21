"""Card Kaizoku per-player routes (used with permission).

  POST https://api.cardkaizoku.com/search        {"searchTerm": name, "playerId": name}
       -> recent sim matches for players whose name contains the term; each row has the sim
          playerId and the full handle 'Name​#1234'.
  GET  https://api.cardkaizoku.com/player-stats?playerId=..&startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
       -> one entry per (gameMode, isPrivate) bucket with deck_stats (full decklists), leader_stats,
          turn-order and coin-flip stats.

Linking a ladder row to a sim playerId is the fuzzy step: search is substring based and names are
not unique, so candidates are scored by how well their leaders match the ladder row's topLeaders.
Resolved ids are cached in data/latest/player_ids.json so the search only runs for new players.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..http import HttpError, get_json, post_json
from .. import normalize as N

API = "https://api.cardkaizoku.com"
TOP = int(os.environ.get("PLAYER_DECKS_TOP", "1000"))        # ladder rows to resolve
WINDOW_DAYS = int(os.environ.get("PLAYER_DECKS_DAYS", "28"))  # decklist window
INTERVAL = float(os.environ.get("KAIZOKU_INTERVAL", "0.4"))   # seconds between calls
RETRY_UNRESOLVED_DAYS = 7                                      # re-search names that failed to link

DISC_RE = re.compile(r"​?#\d+$")
WORKERS = int(os.environ.get("PLAYER_DECKS_WORKERS", "4"))


def base_name(handle: str | None) -> str:
    return DISC_RE.sub("", handle or "").strip().casefold()


def loose(name: str | None) -> str:
    """Sim names drop spaces/punctuation that ladder names keep ('B-Roll' vs 'BRoll')."""
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


# ---------------------------------------------------------------- linking
def search(term: str) -> list[dict]:
    try:
        rows = post_json(f"{API}/search", {"searchTerm": term, "playerId": term}, min_interval=INTERVAL)
    except HttpError as e:
        if e.status in (400, 404):
            return []
        raise
    return rows if isinstance(rows, list) else []


def resolve(player: dict) -> dict | None:
    """Return {"kzId", "handle", "score"} for an OPBounty ladder row, or None."""
    names = []
    for n in (player.get("name"), player.get("username")):
        if n and n.casefold() not in [x.casefold() for x in names]:
            names.append(n)
    wanted = {t["code"] for t in player.get("topLeaders") or [] if t.get("code")}
    weights = {t["code"]: (t.get("matches") or 1) for t in player.get("topLeaders") or []}

    cands: dict[str, dict] = {}
    terms = list(names)
    for n in names:  # 'B-Roll' -> also try 'BRoll'
        if loose(n) and loose(n) != n.casefold() and loose(n) not in [t.casefold() for t in terms]:
            terms.append(loose(n))
    for term in terms:
        rows = search(term)
        for row in rows:
            pid = row.get("playerId")
            if not pid:
                continue
            handle = row.get("playerName")
            if base_name(handle) not in [n.casefold() for n in names] and loose(base_name(handle)) not in [loose(n) for n in names]:
                continue
            c = cands.setdefault(pid, {"kzId": pid, "handle": row.get("playerName"), "leaders": defaultdict(int), "ranked": 0, "latest": ""})
            c["leaders"][row.get("playerLeader")] += 1
            if row.get("gameMode") == 0:
                c["ranked"] += 1
            c["latest"] = max(c["latest"], row.get("date") or "")
    if not cands:
        return None

    def score(c):
        overlap = sum(weights.get(code, 0) for code in c["leaders"] if code in wanted)
        return (overlap, c["ranked"], sum(c["leaders"].values()))

    best = max(cands.values(), key=score)
    s = score(best)
    if len(cands) > 1 and s[0] == 0:
        return None  # several accounts share the name and none plays the ladder row's leaders
    return {"kzId": best["kzId"], "handle": best["handle"], "score": s[0], "candidates": len(cands)}


# ---------------------------------------------------------------- stats
def player_stats(kz_id: str, start: str, end: str) -> list[dict]:
    try:
        data = get_json(f"{API}/player-stats?playerId={kz_id}&startDate={start}&endDate={end}", min_interval=INTERVAL)
    except HttpError as e:
        if e.status in (400, 404):
            return []
        raise
    return data if isinstance(data, list) else []


def pick_bucket(entries: list[dict]) -> dict | None:
    """OPBounty ladder games are ranked (gameMode 0) private-lobby games. Fall back to any ranked bucket."""
    ranked = [e for e in entries if e.get("gameMode") == 0]
    private = [e for e in ranked if e.get("isPrivate")]
    pool = private or ranked
    return max(pool, key=lambda e: e.get("total_matches") or 0) if pool else None


def _turns(t: dict | None) -> dict | None:
    if not t:
        return None
    fg, fw, sg, sw = (t.get("first_games") or 0), (t.get("first_wins") or 0), (t.get("second_games") or 0), (t.get("second_wins") or 0)
    return {"firstGames": fg, "firstWinRate": round(100 * fw / fg, 1) if fg else None,
            "secondGames": sg, "secondWinRate": round(100 * sw / sg, 1) if sg else None}


def shape(entry: dict, kz: dict, window: dict) -> dict:
    decks = []
    for d in entry.get("deck_stats") or []:
        leader = d.get("playerLeader")
        cards = N.parse_deck(d.get("decklist"), leader=leader)
        if not cards:
            continue
        matchups = sorted((d.get("matchup_stats") or []), key=lambda m: -(m.get("games_played") or 0))[:8]
        decks.append({
            "hash": N.deck_hash(cards), "cards": cards, "size": sum(c["qty"] for c in cards),
            "leader": leader, "leaderName": d.get("leaderName"),
            "games": d.get("games_played") or 0, "wins": d.get("wins") or 0, "winRate": d.get("win_rate"),
            "matchups": [{"opp": m.get("oppLeader"), "games": m.get("games_played"), "winRate": m.get("win_rate")} for m in matchups],
            "sources": {"player": {"games": d.get("games_played") or 0, "winRate": d.get("win_rate")}},
        })
    decks.sort(key=lambda d: -d["games"])
    leaders = []
    for l in entry.get("leader_stats") or []:
        leaders.append({"code": l.get("playerLeader"), "name": l.get("leaderName"), "games": l.get("games_played") or 0,
                        "wins": l.get("wins") or 0, "winRate": l.get("win_rate"), "turns": _turns(l.get("turn_order_stats")),
                        "matchups": [{"opp": m.get("oppLeader"), "games": m.get("games_played"), "winRate": m.get("win_rate")}
                                     for m in sorted((l.get("matchup_stats") or []), key=lambda m: -(m.get("games_played") or 0))[:10]]})
    leaders.sort(key=lambda l: -l["games"])
    cf = entry.get("coinflip_stats") or {}
    return {
        "kzId": kz["kzId"], "handle": N.clean_name(kz.get("handle")), "window": window,
        "isPrivate": bool(entry.get("isPrivate")), "matches": entry.get("total_matches") or 0,
        "wins": entry.get("total_wins") or 0, "winRate": entry.get("total_win_rate"), "avgTurns": entry.get("avg_turns"),
        "turns": _turns(entry.get("turn_order_stats")),
        "coinflip": {"won": cf.get("coinflip_wins") or 0, "lost": cf.get("coinflip_losses") or 0,
                     "winRateAfterWin": (round(100 * cf["match_wins_after_coinflip_win"] / cf["coinflip_wins"], 1) if cf.get("coinflip_wins") else None),
                     "winRateAfterLoss": (round(100 * cf["match_wins_after_coinflip_loss"] / cf["coinflip_losses"], 1) if cf.get("coinflip_losses") else None)},
        "leaders": leaders, "decks": decks,
    }


# ---------------------------------------------------------------- driver
def fetch_all(players: list[dict], ids_path: Path, out_dir: Path, today: str) -> dict:
    """Resolve + pull decks for the top TOP ladder rows. Writes out_dir/<opbId>.json per resolved player.
    Returns a status summary. Never raises for a single player."""
    ids: dict[str, dict] = json.loads(ids_path.read_text()) if ids_path.exists() else {}
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=WINDOW_DAYS)
    window = {"start": start.isoformat(), "end": end.isoformat(), "days": WINDOW_DAYS}
    stats = {"considered": 0, "resolved": 0, "searched": 0, "unresolved": 0, "rejected": 0, "withDecks": 0, "errors": 0}
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    import sys
    from concurrent.futures import ThreadPoolExecutor

    def one(p: dict) -> tuple[str, dict | None, dict]:
        """Returns (opb_id, updated id record, counters). Runs in a worker thread; touches no shared state."""
        c = defaultdict(int)
        opb_id = str(p.get("id") or "")
        rec = ids.get(opb_id)
        stale = rec and not rec.get("kzId") and (today > (datetime.fromisoformat(rec["checked"]) + timedelta(days=RETRY_UNRESOLVED_DAYS)).date().isoformat())
        if rec is None or rec.get("name") != p.get("name") or stale:
            try:
                r = resolve(p)
                c["searched"] += 1
            except Exception as e:  # noqa: BLE001
                c["errors"] += 1
                print(f"[players] search failed for {p.get('name')}: {e}", file=sys.stderr)
                return opb_id, rec, c
            rec = {"name": p.get("name"), "checked": today, **(r or {})}
        if not rec.get("kzId"):
            c["unresolved"] += 1
            return opb_id, rec, c
        try:
            entries = player_stats(rec["kzId"], window["start"], window["end"])
        except Exception as e:  # noqa: BLE001
            c["errors"] += 1
            print(f"[players] stats failed for {p.get('name')}: {e}", file=sys.stderr)
            return opb_id, rec, c
        bucket = pick_bucket(entries)
        if not bucket or not bucket.get("deck_stats"):
            c["resolved"] += 1
            return opb_id, rec, c
        # Verify the link with the full data: the ladder's top leaders must appear in this account's ranked leaders.
        wanted = {t["code"] for t in p.get("topLeaders") or [] if t.get("code")}
        have = {l.get("playerLeader") for l in bucket.get("leader_stats") or []}
        if wanted and have and not (wanted & have):
            c["rejected"] += 1
            rec = {"name": p.get("name"), "checked": today, "rejected": rec.get("kzId")}
            return opb_id, rec, c
        c["resolved"] += 1
        out = shape(bucket, rec, window)
        out["opbId"] = opb_id
        (out_dir / f"{opb_id}.json").write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
        c["withDecks"] += 1
        return opb_id, rec, c

    todo = [p for p in players[:TOP] if p.get("id")]
    stats["considered"] = len(todo)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for opb_id, rec, c in ex.map(one, todo):
            if rec is not None:
                ids[opb_id] = rec
            for k, v in c.items():
                stats[k] += v

    ids_path.write_text(json.dumps(ids, indent=0, ensure_ascii=False, sort_keys=True))
    stats["seconds"] = round(time.time() - t0)
    return stats
