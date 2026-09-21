"""Link OPBounty ladder rows to sim handles using the match archive.

A ladder row gives (rating, top leaders, display name) at snapshot time.  A sim handle, from the
combat-log heads we sample for high-bounty games, gives a time series of (timestamp, bounty, leader,
result).  The bounty stored on a match is the player's ladder rating around that game, so the handle
whose last bounty before the snapshot sits closest to the row's rating is the same person, whatever
the two sites call them.  Name similarity and leader overlap add confidence and break ties.

Also produces the per-handle match history the site uses for player pages.

Outputs:
  data/latest/handles.json   {ladderId: {handle, name, confidence, gap, lastSeen, lastBounty, games}}
  data/latest/matches/<ladderId>.json   per linked player: games with side, opp, bounty, deck hash
"""
from __future__ import annotations

import gzip
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATCHES = ROOT / "data" / "matches"
HANDLES = MATCHES / "handles"
LATEST = ROOT / "data" / "latest"
DISC_RE = re.compile(r"​?#\d+\s*$")
BOUNTY_TOL = 40.0   # max |rating - last bounty| considered at all
EXACT_DAYS = 5      # look for exact rating hits in the most recent N days of games
PLACEHOLDERS = {"your client", "opponent", "mobile", ""}  # some client builds log these instead of the real handle


def real_handle(h) -> bool:
    return isinstance(h, str) and h.strip().casefold() not in PLACEHOLDERS


def base(h: str) -> str:
    return DISC_RE.sub("", h or "").strip()


def loose(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").casefold())


def load_days(days: int) -> list[dict]:
    files = sorted(MATCHES.glob("*.json.gz"))[-days:]
    return [json.loads(gzip.decompress(f.read_bytes())) for f in files]


def handle_series(dailies: list[dict]) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """handle -> rows sorted by ts; each row: {ts,id,side,b,leader,opp,oppB,deck,oppDeck,won}.
    Also returns the deck dictionary (hash -> 'NxCODE ...') across the loaded days."""
    series: dict[str, list[dict]] = defaultdict(list)
    decks: dict[str, str] = {}
    for day in dailies:
        decks.update(day.get("decks") or {})
        hpath = HANDLES / f"{day['date']}.json"
        if not hpath.exists():
            continue
        hmap = json.loads(hpath.read_text())
        for m in day["matches"]:
            seats = hmap.get(m["id"])
            if not seats or len(seats) != 2 or not all(isinstance(s, list) and len(s) == 2 for s in seats):
                continue  # missing, or an older handle file without seat leaders
            w, l = m["w"], m["l"]
            (h1, l1), (h2, l2) = seats
            if not (real_handle(h1) and real_handle(h2)):
                continue
            if w["l"] != l["l"]:
                sides = {h1: "w" if l1 == w["l"] else "l", h2: "w" if l2 == w["l"] else "l"}
            else:
                # mirror: log path is "<winner>/<loser>_<wb>_<lb>.log" but seats are unknown; leave for
                # bounty-proximity resolution below
                sides = {h1: None, h2: None}
            for h in (h1, h2):
                series[h].append({"ts": m["ts"], "ct": m.get("ct"), "id": m["id"], "_side": sides[h], "_w": w, "_l": l})
    for h, rows in series.items():
        rows.sort(key=lambda r: (r.get("ct") or r["ts"] or ""))
        prev = None
        for r in rows:
            side = r.pop("_side")
            w, l = r.pop("_w"), r.pop("_l")
            if side is None:  # mirror: side whose bounty is nearer our last known bounty
                side = "w" if prev is None or abs(w["b"] - prev) <= abs(l["b"] - prev) else "l"
            me, opp = (w, l) if side == "w" else (l, w)
            r.update({"side": side, "b": me["b"], "leader": me["l"], "deck": me["d"], "opp": opp["l"], "oppB": opp["b"],
                      "oppDeck": opp["d"], "won": side == "w"})
            prev = me["b"]
    return series, decks


def link(players: list[dict], series: dict[str, list[dict]], snapshot_ts: str, previous: dict | None = None) -> dict:
    """Ladder row -> sim handle.

    The bounty stored on a match is the player's rating right after that game, to two decimals, and the
    ladder snapshot carries the same two-decimal rating.  So the handle whose last game before the
    snapshot left it at exactly the ladder rating is that player ("exact").  When the player has played
    again between their last sampled game and the snapshot the numbers drift; then we accept a handle
    whose base name matches the ladder name or username within BOUNTY_TOL points ("name").  Links found
    on earlier days are kept while they stay plausible, so a player who does not play today is not lost.
    """
    previous = previous or {}
    last: dict[str, tuple[str, float, set[str]]] = {}
    for h, rows in series.items():
        before = [r for r in rows if (r.get("ct") or r["ts"] or "") <= snapshot_ts] or rows
        last[h] = (before[-1]["ts"], before[-1]["b"], {r["leader"] for r in before})
    # every (bounty -> handle) pair recorded in the recent window; the bounty on a side is that player's
    # rating around the game, to the cent, so an exact hit against the ladder rating is near-unique
    recent_days = sorted({(r.get("ct") or r["ts"] or "")[:10] for rows in series.values() for r in rows})[-EXACT_DAYS:]
    by_bounty: dict[float, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for h, rows in series.items():
        for r in rows:
            if (r.get("ct") or r["ts"] or "")[:10] in recent_days:
                by_bounty[round(r["b"], 2)][h] += 1

    out, used = {}, set()
    ordered = sorted(players, key=lambda p: -(p.get("matches") or 0))
    # pass 1: exact rating equality with any recent recorded bounty of the handle
    for p in ordered:
        if p.get("id") is None:
            continue
        rating = round(float(p.get("bounty") or 0), 2)
        want = {t["code"] for t in p.get("topLeaders") or []}
        names = {loose(p.get("name")), loose(p.get("username"))} - {""}
        cands = {h: n for h, n in by_bounty.get(rating, {}).items() if h not in used}
        if not cands:
            continue

        def good(h):  # evidence beyond the cent hit: the handle plays the player's leaders, or shares the name
            return bool(want & last[h][2]) or loose(base(h)) in names

        strong = [h for h in cands if good(h)]
        if len(strong) == 1:
            h = strong[0]
        elif not strong and len(cands) == 1 and not want:
            h = next(iter(cands))  # hidden-stat ladder row: nothing to check against, accept the unique hit
        else:
            continue  # coincidence risk: two handles hit the same cent, or the only hit plays other leaders
        used.add(h)
        out[str(p["id"])] = {"handle": h, "name": base(h), "confidence": "exact", "gap": 0.0, "hits": cands[h],
                             "lastSeen": last[h][0], "lastBounty": round(last[h][1], 2), "games": len(series[h])}
    # pass 2: name match within tolerance
    for p in ordered:
        pid = str(p.get("id"))
        if p.get("id") is None or pid in out:
            continue
        rating = float(p.get("bounty") or 0)
        names = {loose(p.get("name")), loose(p.get("username"))} - {""}
        want = {t["code"] for t in p.get("topLeaders") or []}
        same = [h for h in last if h not in used and loose(base(h)) in names]
        if not same:
            continue
        overl = [h for h in same if want & last[h][2]]
        pick = overl[0] if len(overl) == 1 else (same[0] if len(same) == 1 and abs(last[same[0]][1] - rating) <= BOUNTY_TOL else None)
        if not pick:
            continue
        ts, b, _ = last[pick]
        used.add(pick)
        out[pid] = {"handle": pick, "name": base(pick), "confidence": "name", "gap": round(abs(b - rating), 2),
                    "lastSeen": ts, "lastBounty": round(b, 2), "games": len(series[pick])}
    # pass 3: carry forward earlier links that are still plausible
    for pid, prev in previous.items():
        if pid in out or prev.get("handle") in used or prev.get("handle") not in last:
            continue
        p = next((x for x in players if str(x.get("id")) == pid), None)
        if not p:
            continue
        ts, b, _ = last[prev["handle"]]
        if abs(b - float(p.get("bounty") or 0)) <= BOUNTY_TOL:
            used.add(prev["handle"])
            out[pid] = {**prev, "confidence": prev.get("confidence", "exact").rstrip("*") + "*", "gap": round(abs(b - float(p.get("bounty") or 0)), 2),
                        "lastSeen": ts, "lastBounty": round(b, 2), "games": len(series[prev["handle"]])}
    return out


def write_player_matches(links: dict, series: dict, decks: dict):
    out_dir = LATEST / "matches"
    out_dir.mkdir(parents=True, exist_ok=True)
    keep = set()
    for pid, info in links.items():
        rows = series.get(info["handle"], [])
        used = {r["deck"] for r in rows if r["deck"]} | {r["oppDeck"] for r in rows if r["oppDeck"]}
        obj = {"handle": info["handle"], "name": info["name"], "games": rows,
               "decks": {h: decks[h] for h in used if h in decks}}
        (out_dir / f"{pid}.json").write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
        keep.add(f"{pid}.json")
    for f in out_dir.glob("*.json"):
        if f.name not in keep:
            f.unlink()


def run(days: int = 31) -> dict:
    players = json.loads((LATEST / "players.json").read_text())
    meta = json.loads((LATEST / "meta.json").read_text()) if (LATEST / "meta.json").exists() else {}
    snap = (meta.get("generatedAt") or "9999")[:19]
    series, decks = handle_series(load_days(days))
    prev = json.loads((LATEST / "handles.json").read_text()) if (LATEST / "handles.json").exists() else {}
    links = link(players, series, snap, prev)
    write_player_matches(links, series, decks)
    (LATEST / "handles.json").write_text(json.dumps(links, separators=(",", ":"), ensure_ascii=False))
    conf = defaultdict(int)
    for v in links.values():
        conf[v["confidence"]] += 1
    return {"linked": len(links), "of": len(players), "handles": len(series), **conf}


if __name__ == "__main__":
    import sys
    print(json.dumps(run(int(sys.argv[1]) if len(sys.argv) > 1 else 31)))
