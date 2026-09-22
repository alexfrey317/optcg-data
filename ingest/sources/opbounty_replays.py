"""Study games: full combat logs for the highest-bounty finished games of every leader matchup.

For every pair of contender leaders over the archive window we keep, per bucket, the GAMES_PER_BUCKET best
finished games, where the buckets are (winning leader, winner went first?) -> 4 per pair, 2 for mirrors, and
"best" means both players had a high bounty (ranked by the lower of the two).  Logs come from the same
Firebase Storage bucket the handle linker already reads (see opbounty_matches); we download whole logs here.

Output (data/replays/, gitignored, kept in the CI cache; logs are immutable upstream so a lost cache just refills):
  index.json           {"checked": {matchId: {"ok": bool, "why": str}}}  every log we ever looked at
  games/<id>.json.gz   parsed, anonymised game (see parse_log)
  best.json            {"window": {...}, "leaders": [...], "pairs": {"A|B": {"W:1": [summary...], ...}}}

Anonymisation: handles never leave the parser (actors become seat numbers), chat lines are dropped, room ids
and client versions are not kept.
"""
from __future__ import annotations

import gzip
import json
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..http import HttpError
from . import opbounty_matches as M

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "data" / "replays"
GAMES = OUT / "games"
RAWLOGS = OUT / "raw"  # gzipped combat logs of finished games, so a parser change never re-downloads

GAMES_PER_BUCKET = int(os.environ.get("OPB_REPLAY_PER_BUCKET", "5"))
WINDOW_DAYS = int(os.environ.get("OPB_REPLAY_DAYS", "30"))
MAX_LEADERS = int(os.environ.get("OPB_REPLAY_LEADERS", "32"))
MIN_SEAT_SHARE = 0.25  # % of seats over the window, same cutoff as the site's "rarely played" divider
MAX_DOWNLOADS = int(os.environ.get("OPB_REPLAY_MAX", "25000"))  # per run (~0.04 s each with 8 workers); the first fill needs ~20k, a daily top-up a few hundred
TRIES_PER_BUCKET = 4 * GAMES_PER_BUCKET  # concedes waste downloads; stop digging into low bounties
WORKERS = int(os.environ.get("OPB_HANDLE_WORKERS", "8"))
MIN_END_TURNS = 8  # a finished game saw at least four turns each ...
MAX_LOSER_LIFE = 2  # ... and the loser conceded at or near lethal, or the game ran long
LONG_GAME_TURNS = 12

MARK = re.compile(r'\[<mark><link="([^"]+)">[^<]*</link></mark>\]')
TAG = re.compile(r"</?(?:b|i|size(?:=\d+)?|color(?:=[^>]+)?)>")
ACTOR = re.compile(r"^\[(.+?)\] (.*)$")
PLY = re.compile(r"^RZ1\|PLY\|([12])\|(.+?)\|([A-Z0-9]+-\d+)\s*$")
CARD_ID = re.compile(r"\[([A-Z]{1,3}\d{2}-\d{3}(?:_p\d+)?)\]")
LIST = re.compile(r"^(Hand|Board|Trash) before Mulligan: \[(.*)\]$|^(Hand|Board|Trash): \[(.*)\]$")


def _kind(text: str, actor: int) -> str:
    t = text
    if t == "End Turn": return "end"
    if t == "Concedes!": return "concede"
    if t.startswith("Draw ") and "Don" in t: return "don"
    if t.startswith("Draw ") or t.startswith("Drew card"): return "draw"
    if t.startswith("Deploy "): return "deploy"
    if " attacking " in t: return "attack"
    if actor == 0 and "] vs " in t: return "combat"
    if t == "Attack Fails": return "fail"
    if " hit for " in t: return "hit"
    if t.endswith(" Blocks"): return "block"
    if " for Counter " in t or ": Activate Counter" in t: return "counter"
    if t.startswith("Attach ") or t.startswith("Activate "): return "don"
    if t.startswith("Trash ") or " Destroyed" in t or t.endswith(" Destroyed"): return "trash"
    if "Mulligan" in t or t.startswith("Placing Cards"): return "mull"
    if t.startswith("Chose to go") or t.startswith("Will select"): return "info"
    return "effect"


ALIASES = {"You", "Your Client", "Opponent"}
SKIP_LINE = ("Waiting for a Connection", "Version is")


def _deck_ids(text: str | None) -> set[str]:
    return {t.split("x", 1)[1] for t in (text or "").split() if "x" in t}


PARSER_VERSION = 2
# RZ1 state rows: RZ1|seq|player|card|fromZone|fromIdx|toZone|toIdx|f1|f2|rested|f4|f5 followed by
# RZ1|CHK|seq|player|deck|hand|chars|life|donDeck|donActive|trash|stage|?|donAttached
ZONES = {0: "deck", 1: "hand", 2: "chars", 3: "life", 4: "donDeck", 5: "don", 6: "trash", 7: "stage", 9: "attached"}
MOVE = re.compile(r"^RZ1\|(\d+)\|([12])\|([^|]+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d)\|(\d)\|(\d)\|(\d)\|(\d)")
CHK = re.compile(r"^RZ1\|CHK\|(\d+)\|([12])\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)")
DREW = re.compile(r"^Drew card from deck: .*\[([A-Z0-9]+-\d+(?:_p\d+)?)\]$")


def parse_log(raw: str, match: dict) -> dict | None:
    """Turn a combat log into an anonymised, step-by-step game.

    Actors in a log are labels: handles, or "You"/"Opponent" when the uploading client wrote it, and the two kinds
    can mix (actions under aliases, state snapshots under handles).  Every label is resolved to a SIDE of the
    archive record (w = winner, l = loser) by leader, by who conceded, or by matching the cards it held against the
    two decklists; the side that took the first turn becomes seat 1.  The numeric player in the RZ1 state rows is
    tied to a label by matching draw rows to the "Drew card" line that follows them.  Returns None when anything
    cannot be resolved, so nothing is ever shown with the wrong attribution.

    steps: ["m", seat, card, fromZone, toZone, rested, deck, donDeck, donActive, donAttached]  card move (+ counts after it)
           ["e", seat, kind, text]                                                              narrative event (seat 0 = neutral)
           ["s", seat, hand[], chars[], trash[], life]                                          state snapshot (viewer resyncs)"""
    decks = match.get("_decks") or {}
    wl, ll = match["w"]["l"], match["l"]["l"]
    deck_w, deck_l = _deck_ids(decks.get(match["w"].get("d"))), _deck_ids(decks.get(match["l"].get("d")))
    rows: list[tuple] = []  # ("t", actor, text) | ("m", num, card, zf, zt, rested) | ("c", num, counts)
    leader_of: dict[str, str] = {}
    cards_of: dict[str, set[str]] = defaultdict(set)
    conceder = None; chose_first = None; first_end = None
    num_label: dict[str, str] = {}
    pending_draw: dict[str, str] = {}  # card id -> numeric player of an unmatched deck->hand row
    pending_text: dict[str, str] = {}  # card id -> label of an unmatched "Drew card" line (either order occurs)
    for line in raw.splitlines():
        line = line.rstrip()
        m = PLY.match(line)
        if m:
            leader_of[m.group(2).strip()] = m.group(3); num_label.setdefault(m.group(1), m.group(2).strip()); continue
        m = MOVE.match(line)
        if m:
            zf, zt = int(m.group(4)), int(m.group(6))
            if zf == 0 and zt == 0:
                continue  # deck shuffles
            rows.append(("m", m.group(2), m.group(3), zf, zt, int(m.group(10))))
            if zf == 0 and zt == 1:
                if m.group(3) in pending_text:
                    num_label.setdefault(m.group(2), pending_text.pop(m.group(3)))
                else:
                    pending_draw[m.group(3)] = m.group(2)
            continue
        m = CHK.match(line)
        if m:
            rows.append(("c", m.group(2), (int(m.group(3)), int(m.group(7)), int(m.group(8)), int(m.group(12)))))
            continue
        if not line or line.startswith("RZ1|") or "<size=" in line or line.startswith(SKIP_LINE) or line.endswith(" Has Connected"):
            continue
        actor = None; text = line
        m = ACTOR.match(line)
        if m:
            actor = m.group(1); text = m.group(2)
        text = TAG.sub("", MARK.sub(r"[\1]", text)).strip()
        rows.append(("t", actor, text))
        if actor is None:
            continue
        d = DREW.match(text)
        if d:
            if d.group(1) in pending_draw:
                num_label.setdefault(pending_draw.pop(d.group(1)), actor)
            else:
                pending_text[d.group(1)] = actor
        if text.startswith("Leader is "):
            ids = CARD_ID.findall(text)
            if ids: leader_of[actor] = ids[-1]
        elif text == "Concedes!":
            conceder = conceder or actor
        elif text.startswith("Chose to go "):
            chose_first = chose_first or (actor if text.endswith("First") else ("!" + actor))
        elif text == "End Turn":
            first_end = first_end or actor
        else:
            lm = LIST.match(text)
            if lm:
                cards_of[actor].update(c for c in (lm.group(2) or lm.group(4) or "").split(",") if c)

    labels = set(leader_of) | set(cards_of) | {r[1] for r in rows if r[0] == "t" and r[1]}
    side: dict[str, str] = {}
    if wl != ll:
        for lab, ld in leader_of.items():
            if ld == wl: side[lab] = "w"
            elif ld == ll: side[lab] = "l"
    if conceder:
        side.setdefault(conceder, "l")
    if deck_w and deck_l and deck_w != deck_l:
        for lab, cs in cards_of.items():
            if lab in side: continue
            sw, sl = len(cs & deck_w), len(cs & deck_l)
            if sw != sl: side[lab] = "w" if sw > sl else "l"
    for group in (labels & ALIASES, labels - ALIASES):
        known = {side[x] for x in group if x in side}
        if len(group) == 2 and len(known) == 1:
            for x in group:
                side.setdefault(x, "l" if "w" in known else "w")
    if any(lab not in side for lab in labels) or not {"w", "l"} <= set(side.values()):
        return None
    if chose_first:
        first_side = side[chose_first[1:]] if chose_first.startswith("!") else side[chose_first]
        if chose_first.startswith("!"): first_side = "l" if first_side == "w" else "w"
    elif first_end:
        first_side = side[first_end]
    else:
        return None
    seat_of_side = {first_side: 1, ("l" if first_side == "w" else "w"): 2}
    seat = {lab: seat_of_side[s] for lab, s in side.items()}
    # numeric player -> seat; both numbers must resolve to different seats
    num_seat = {n: seat[lab] for n, lab in num_label.items() if lab in seat}
    if len(num_seat) == 1:
        (n, s), = num_seat.items(); num_seat[{"1": "2", "2": "1"}[n]] = 3 - s
    if set(num_seat) != {"1", "2"} or len(set(num_seat.values())) != 2:
        return None
    winner = seat_of_side["w"]; loser = 3 - winner
    leaders = {seat_of_side["w"]: wl, seat_of_side["l"]: ll}

    steps: list[list] = []
    life = {1: None, 2: None}
    mull: dict[int, list[str]] = {}
    ends = 0; snap: dict[int, dict] = {1: {}, 2: {}}
    for r in rows:
        if r[0] == "m":
            steps.append(["m", num_seat[r[1]], r[2], r[3], r[4], r[5], None, None, None, None])
        elif r[0] == "c":
            if steps and steps[-1][0] == "m" and steps[-1][6] is None:
                steps[-1][6:10] = list(r[2])
        else:
            _, actor, text = r
            s = seat[actor] if actor else 0
            if s and text.startswith("Leader is "): continue
            lm = LIST.match(text)
            if lm and s:
                if lm.group(1):
                    mull[s] = [c for c in lm.group(2).split(",") if c]
                else:
                    snap[s][lm.group(3).lower()] = [c for c in lm.group(4).split(",") if c]
                continue
            if s and text.startswith("Life: "):
                life[s] = int(text[6:])
                steps.append(["s", s, snap[s].get("hand", []), snap[s].get("board", []), snap[s].get("trash", []), life[s]])
                snap[s] = {}
                continue
            if s and (text.startswith("Chose to go ") or text.startswith("Will select")):
                continue
            kind = _kind(text, s)
            if kind == "end": ends += 1
            steps.append(["e", s, kind, text])
    finished = ends >= MIN_END_TURNS and ((life[loser] is not None and life[loser] <= MAX_LOSER_LIFE) or ends >= LONG_GAME_TURNS)
    how = "concede" if conceder else ("lethal" if life[loser] == 0 else "unknown")
    players = []
    for s in (1, 2):
        m = match["w"] if s == winner else match["l"]
        players.append({"seat": s, "leader": leaders[s], "bounty": m["b"], "won": s == winner, "first": s == 1,
                        "deck": decks.get(m["d"]) if m.get("d") else None, "mulligan": mull.get(s)})
    return {"v": PARSER_VERSION, "id": match["id"], "ts": match["ts"], "date": match["ts"][:10], "players": players, "first": 1, "winner": winner,
            "endTurns": ends, "end": {"how": how, "loserLife": life[loser], "winnerLife": life[winner]},
            "finished": finished, "steps": steps}


# ---------------------------------------------------------------- selection
def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def bucket_key(pair: tuple[str, str], winner_leader: str, winner_first: bool) -> str:
    return f"{winner_leader}:{1 if winner_first else 2}"


def contenders(days: list[dict]) -> list[str]:
    seats = defaultdict(int); total = 0
    for d in days:
        for m in d["matches"]:
            seats[m["w"]["l"]] += 1; seats[m["l"]["l"]] += 1; total += 2
    ranked = sorted(seats, key=lambda c: -seats[c])
    return [c for c in ranked if 100 * seats[c] / total >= MIN_SEAT_SHARE][:MAX_LEADERS] if total else []


def summary(g: dict) -> dict:
    p = {x["seat"]: x for x in g["players"]}
    w, l = p[g["winner"]], p[3 - g["winner"]]
    return {"id": g["id"], "ts": g["ts"], "w": {"l": w["leader"], "b": round(w["bounty"]), "first": w["first"]},
            "l": {"l": l["leader"], "b": round(l["bounty"])}, "lo": round(min(w["bounty"], l["bounty"])),
            "turns": g["endTurns"], "end": g["end"]}


def fetch_all(days: list[dict], client: M.Client | None = None) -> dict:
    """days: archive day objects (opbounty_matches shape), oldest first, the whole window."""
    client = client or M.Client()
    GAMES.mkdir(parents=True, exist_ok=True); RAWLOGS.mkdir(parents=True, exist_ok=True)
    index = _load_json(OUT / "index.json", {"checked": {}})
    checked: dict = index["checked"]
    pool = contenders(days)
    pool_set = set(pool)
    decks: dict[str, str] = {}
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for d in days:
        decks.update(d.get("decks") or {})
        for m in d["matches"]:
            a, b = m["w"]["l"], m["l"]["l"]
            if a in pool_set and b in pool_set and m.get("log"):
                buckets[(tuple(sorted((a, b))), a, None)].append(m)  # winner-first unknown until parsed; split below
    # winner-first is only known from the log, so candidates are ranked per (pair, winner) and sorted into
    # first/second buckets as they are parsed; each (pair, winner) needs GAMES_PER_BUCKET for both seats.
    for k in buckets:
        buckets[k].sort(key=lambda m: -min(m["w"]["b"], m["l"]["b"]))

    have: dict[tuple, dict[bool, list[dict]]] = defaultdict(lambda: {True: [], False: []})
    def load_game(mid: str) -> dict | None:
        p = GAMES / f"{mid}.json.gz"
        if not p.exists(): return None
        try:
            return json.loads(gzip.decompress(p.read_bytes()))
        except (OSError, ValueError):
            return None
    for (pair, wl, _), ms in buckets.items():
        for m in ms:
            c = checked.get(m["id"])
            if c and c.get("v") != PARSER_VERSION:
                rawp = RAWLOGS / f"{m['id']}.log.gz"
                if c.get("ok") and rawp.exists():  # re-parse the kept log with the current parser
                    try:
                        g = parse_log(gzip.decompress(rawp.read_bytes()).decode("utf-8", errors="replace"), dict(m, _decks=decks))
                    except Exception:  # noqa: BLE001
                        g = None
                    if g and g["finished"]:
                        (GAMES / f"{m['id']}.json.gz").write_bytes(gzip.compress(json.dumps(g, separators=(",", ":"), ensure_ascii=False).encode(), mtime=0))
                        checked[m["id"]] = {"ok": True, "why": g["end"]["how"], "turns": g["endTurns"], "life": g["end"]["loserLife"], "v": PARSER_VERSION}
                        c = checked[m["id"]]
                    else:
                        checked.pop(m["id"], None); c = None
                elif c.get("ok"):
                    checked.pop(m["id"], None); c = None  # stored under an old parser and no raw log kept: fetch again
            if c and c.get("ok"):
                g = load_game(m["id"])
                if g:
                    have[(pair, wl)][g["winner"] == 1].append(g)
                else:
                    checked.pop(m["id"], None)  # cache lost the file: allow a re-download

    downloads = 0; parsed = 0; finished = 0
    def need(pair, wl):
        h = have[(pair, wl)]
        return len(h[True]) < GAMES_PER_BUCKET or len(h[False]) < GAMES_PER_BUCKET

    def one(m):
        try:
            raw = client.log_head(m["log"], nbytes=64_000_000)
        except HttpError as e:
            return m, None, f"http {e.status}"
        if not raw:
            return m, None, "missing"
        m = dict(m, _decks=decks, _raw=raw)
        try:
            g = parse_log(raw, m)
        except Exception as e:  # noqa: BLE001 - one odd log must not stop the run
            return m, None, f"parse: {type(e).__name__}"
        return m, g, "ok" if g else "unmapped"

    for rnd in range(3):  # widen the search for buckets still short after each round
        todo = []
        for (pair, wl, _), ms in buckets.items():
            if not need(pair, wl): continue
            tries = 0
            for m in ms:
                if tries >= TRIES_PER_BUCKET * (rnd + 1): break
                if m["id"] in checked: tries += 1; continue
                todo.append(m); tries += 1
                if tries >= TRIES_PER_BUCKET * (rnd + 1): break
        todo = todo[: max(0, MAX_DOWNLOADS - downloads)]
        if not todo: break
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for m, g, why in ex.map(one, todo):
                downloads += 1
                if g is None:
                    if why != "missing" and not why.startswith("http"):
                        checked[m["id"]] = {"ok": False, "why": why}
                    elif why == "missing":
                        checked[m["id"]] = {"ok": False, "why": why}
                    continue
                parsed += 1
                checked[m["id"]] = {"ok": g["finished"], "why": g["end"]["how"], "turns": g["endTurns"], "life": g["end"]["loserLife"], "v": PARSER_VERSION}
                if g["finished"]:
                    finished += 1
                    (RAWLOGS / f"{m['id']}.log.gz").write_bytes(gzip.compress(m["_raw"].encode("utf-8"), mtime=0))
                    (GAMES / f"{m['id']}.json.gz").write_bytes(gzip.compress(json.dumps(g, separators=(",", ":"), ensure_ascii=False).encode(), mtime=0))
                    pair = tuple(sorted((m["w"]["l"], m["l"]["l"])))
                    have[(pair, m["w"]["l"])][g["winner"] == 1].append(g)

    # best.json: the site's index
    pairs: dict[str, dict[str, list[dict]]] = {}
    keep: set[str] = set()
    for (pair, wl), h in have.items():
        for wf, gs in h.items():
            gs.sort(key=lambda g: -min(p["bounty"] for p in g["players"]))
            top = gs[:GAMES_PER_BUCKET]
            if not top: continue
            pairs.setdefault("|".join(pair), {})[bucket_key(pair, wl, wf)] = [summary(g) for g in top]
            keep.update(g["id"] for g in top)
    start = days[0]["date"] if days else None; end = days[-1]["date"] if days else None
    (OUT / "best.json").write_text(json.dumps({"window": {"start": start, "end": end, "days": len(days)}, "leaders": pool, "perBucket": GAMES_PER_BUCKET, "pairs": pairs},
                                              separators=(",", ":"), ensure_ascii=False))
    # prune: games outside the window that no bucket references
    cutoff = ((datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS + 15)).date()).isoformat()
    pruned = 0
    for p in GAMES.glob("*.json.gz"):
        mid = p.name[: -len(".json.gz")]
        if mid in keep: continue
        c = checked.get(mid) or {}
        g = load_game(mid)
        if g and g["date"] < cutoff:
            p.unlink(); pruned += 1
            (RAWLOGS / f"{mid}.log.gz").unlink(missing_ok=True)
    (OUT / "index.json").write_text(json.dumps(index, separators=(",", ":")))
    return {"leaders": len(pool), "pairs": len(pairs), "buckets": sum(len(v) for v in pairs.values()), "games": len(keep),
            "downloads": downloads, "parsed": parsed, "finished": finished, "pruned": pruned, "checked": len(checked)}
