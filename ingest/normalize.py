"""Turn raw upstream payloads into the small JSON files the site is built from."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

CARD_RE = re.compile(r"(\d+)\s*x\s*([A-Z]{1,3}\d{2,3}-\d{3}(?:_p\d+)?|P-\d{3})", re.I)


def strip_prefix(code: str | None) -> str | None:
    """OPBounty writes leaders as '1xOP09-062'; everyone else as 'OP09-062'."""
    if not code:
        return code
    return re.sub(r"^\d+x", "", code)


def parse_deck(text, leader: str | None = None) -> list[dict]:
    """Accepts a newline/space separated string or a list of 'NxCODE' tokens.
    Returns sorted [{id, qty}] with the leader line removed."""
    cards: dict[str, int] = defaultdict(int)
    if isinstance(text, list):
        # optcg.one: [["OP01-016", 4], ...]; OPBounty: ["4xOP01-016", ...]
        if text and isinstance(text[0], (list, tuple)):
            for cid, qty in text:
                cid = str(cid).upper()
                if leader and cid == leader.upper():
                    continue
                cards[cid] += int(qty)
            return [{"id": k, "qty": v} for k, v in sorted(cards.items())]
        text = " ".join(map(str, text))
    for qty, cid in CARD_RE.findall(text or ""):
        cid = cid.upper()
        if leader and cid == leader.upper():
            continue
        cards[cid] += int(qty)
    return [{"id": k, "qty": v} for k, v in sorted(cards.items())]


def deck_hash(cards: list[dict]) -> str:
    key = "|".join(f"{c['qty']}x{c['id']}" for c in cards)
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def clean_name(name: str | None) -> str | None:
    """Card Kaizoku pilots look like 'terry​#7367'; drop the discriminator."""
    if not name:
        return name
    return re.sub(r"​?#\d+$", "", name).strip()


def pct(x, digits=1):
    return None if x is None else round(float(x) * 100, digits)


# ---------------------------------------------------------------- players
def build_players(opb: dict) -> list[dict]:
    out = []
    for e in opb.get("leaderboard", []):
        out.append({
            "id": e.get("playerId"),
            "rank": e.get("rank"),
            "name": e.get("displayName") or e.get("username"),
            "bounty": e.get("rating"),
            "wins": e.get("wins"), "losses": e.get("losses"), "matches": e.get("matches"),
            "winRate": e.get("winRate"),
            "country": e.get("country"),
            "faction": "Marine" if e.get("isMarine") else "Pirate",
            "topLeaders": [
                {"code": strip_prefix(t.get("code")), "winRate": t.get("winRate"), "matches": t.get("matches")}
                for t in (e.get("topLeaders") or [])
            ],
        })
    return out


# ---------------------------------------------------------------- leaders
def build_leaders(opb: dict, kz: dict, card_db: dict) -> dict:
    leaders: dict[str, dict] = {}

    def slot(code):
        code = strip_prefix(code)
        if code not in leaders:
            c = card_db.get(code, {})
            leaders[code] = {"code": code, "name": c.get("name") or code, "color": c.get("color"),
                             "img": c.get("img"), "sources": {}, "matchups": {}}
        return leaders[code]

    for row in kz.get("stats") or []:
        L = slot(row["leaderKey"])
        L["name"] = row.get("leaderName") or L["name"]
        L["sources"]["kaizoku"] = {
            "matches": row.get("number_of_matches"), "wins": row.get("wins"),
            "winRate": pct(row.get("raw_win_rate")), "weightedWinRate": pct(row.get("weighted_win_rate")),
            "playRate": pct(row.get("play_rate"), 2),
            "firstWinRate": pct(row.get("first_win_rate")), "secondWinRate": pct(row.get("second_win_rate")),
        }
        for m in row.get("matchups") or []:
            opp = strip_prefix(m.get("opponentKey") or m.get("opponent"))
            L["matchups"].setdefault(opp, {})["kaizoku"] = {
                "games": m.get("total_games"), "winRate": pct(m.get("matchup_win_rate")),
                "firstWinRate": pct(m.get("first_win_rate")), "secondWinRate": pct(m.get("second_win_rate")),
                "firstGames": m.get("first_games"), "secondGames": m.get("second_games"),
            }

    for row in (opb.get("leaders") or {}).get("leaders", []):
        L = slot(row["leader"])
        L["sources"]["opbounty"] = {
            "matches": row.get("number_of_matches"), "wins": row.get("wins"), "losses": row.get("losses"),
            "winRate": row.get("winRate"), "popularity": row.get("popularity"), "avgDuration": row.get("avgDuration"),
        }
    for row in (opb.get("matchups") or {}).get("matchups", []):
        L = slot(row["leader"])
        for m in row.get("matchups") or []:
            opp = strip_prefix(m.get("opponent"))
            if not opp or opp == "Mobile":
                continue
            L["matchups"].setdefault(opp, {})["opbounty"] = {
                "games": m.get("games"), "winRate": m.get("winRate"), "wins": m.get("wins"), "losses": m.get("losses"),
            }
    return leaders


# ---------------------------------------------------------------- decks
def build_decks(code: str, opb: dict, kz: dict, one: dict) -> dict:
    decks: dict[str, dict] = {}

    def slot(cards):
        h = deck_hash(cards)
        if h not in decks:
            decks[h] = {"hash": h, "cards": cards, "size": sum(c["qty"] for c in cards), "sources": {}}
        return decks[h]

    opb_key = f"1x{code}"
    for d in (opb.get("decklists", {}).get(opb_key) or {}).get("decklists", []):
        cards = parse_deck(d.get("deck"), leader=code)
        if not cards:
            continue
        slot(cards)["sources"]["opbounty"] = {
            "games": d.get("games"), "wins": d.get("wins"), "losses": d.get("losses"),
            "winRate": d.get("winRate"), "avgDuration": d.get("avgDuration"),
        }

    kz_row = next((r for r in kz.get("decklist") or [] if r.get("leaderKey") == code), None)
    if kz_row:
        for key, label in (("best_decklists", "kaizokuBest"), ("most_played_decklists", "kaizokuPlayed")):
            for d in kz_row.get(key) or []:
                cards = parse_deck(d.get("decklist"), leader=code)
                if not cards:
                    continue
                slot(cards)["sources"][label] = {
                    "games": d.get("total_games"), "wins": d.get("wins"),
                    "winRate": pct(d.get("raw_win_rate")), "adjWinRate": pct(d.get("adj_win_rate")),
                    "pilots": d.get("unique_pilots"), "score": d.get("final_score") or d.get("deck_score"),
                }

    one_row = (one.get("leaders") or {}).get(code) or {}
    for d in one_row.get("ranked") or []:
        cards = parse_deck(d.get("composition"), leader=code)
        if not cards:
            continue
        s = slot(cards)["sources"].setdefault("optcgone", {"games": 0, "wins": 0, "losses": 0, "regions": []})
        s["games"] += d.get("matches") or 0
        s["wins"] += d.get("wins") or 0
        s["losses"] += d.get("losses") or 0
        if d.get("region") and d["region"] not in s["regions"]:
            s["regions"].append(d["region"])
    for d in decks.values():
        s = d["sources"].get("optcgone")
        if s and s["games"]:
            s["winRate"] = round(100 * s["wins"] / s["games"], 1)

    def total_games(d):
        return sum((s.get("games") or 0) for s in d["sources"].values())

    ranked = sorted(decks.values(), key=total_games, reverse=True)

    top_pilots = []
    lb = next((r for r in kz.get("leaderboard") or [] if r.get("leaderKey") == code), None)
    if lb:
        for p in lb.get("top_players") or []:
            top_pilots.append({"name": clean_name(p.get("player")), "rank": p.get("rank"), "wins": p.get("wins"),
                               "games": p.get("total_games"), "winRate": pct(p.get("raw_win_rate"))})
    return {"leader": code, "decks": ranked, "topPilots": top_pilots}


# ---------------------------------------------------------------- cards per leader
def build_cards(code: str, kz: dict) -> dict:
    out = {"leader": code, "cards": [], "openingHand": [], "tech": {}}
    row = next((r for r in kz.get("cardstats") or [] if r.get("leaderKey") == code), None)
    if row:
        out["leaderStats"] = {"games": row.get("games"), "winRate": pct(row.get("win_rate")),
                              "uniqueDecklists": row.get("unique_decklists"), "uniquePilots": row.get("unique_pilots")}
        for c in row.get("cards") or []:
            out["cards"].append({
                "id": c.get("card"), "games": c.get("games"), "usage": pct(c.get("usage_rate")),
                "winRate": pct(c.get("win_rate")), "winRateVsLeader": pct(c.get("win_rate_vs_leader"), 2),
                "avgCopies": round(c.get("avg_copies_when_included") or 0, 2),
                "firstWinRate": pct(c.get("first_win_rate")), "secondWinRate": pct(c.get("second_win_rate")),
                "quantities": [{"qty": q.get("quantity"), "games": q.get("games"), "winRate": pct(q.get("win_rate"))}
                               for q in (c.get("quantities") or [])],
            })
        out["cards"].sort(key=lambda c: c["games"] or 0, reverse=True)
    hands = next((r for r in kz.get("hands") or [] if r.get("leaderKey") == code), None)
    if hands:
        out["openingHand"] = [{"id": c.get("card"), "rate": pct(c.get("appearance_rate"))}
                              for c in (hands.get("common_cards") or [])][:20]
    for pair in kz.get("matchuptech") or []:
        if pair.get("winnerLeaderKey") != code:
            continue
        opp = pair.get("loserLeaderKey")
        techs = [t for t in (pair.get("tech_cards") or []) if t.get("has_strong_evidence")]
        techs.sort(key=lambda t: t.get("conservative_lift") or 0, reverse=True)
        if techs:
            out["tech"][opp] = [{"id": t.get("card"), "lift": pct(t.get("conservative_lift")),
                                 "winRateWith": pct(t.get("win_rate_with")), "winRateWithout": pct(t.get("win_rate_without")),
                                 "gamesWith": t.get("games_with")} for t in techs[:5]]
    return out


# ---------------------------------------------------------------- card db
def build_card_db(raw: list[dict]) -> dict:
    db = {}
    for c in raw or []:
        cid = c.get("cardNumber")
        if not cid or cid in db:
            continue
        db[cid] = {"name": c.get("cardName"), "type": c.get("cardType"), "cost": c.get("cost"),
                   "color": c.get("color"), "power": c.get("power"), "counter": c.get("counter"),
                   "rarity": c.get("rarity"), "img": c.get("bucketImg") or c.get("cardImg"),
                   "set": c.get("cardSet")}
    return db


# ---------------------------------------------------------------- history
def append_history(path: Path, point: list, max_points: int = 400):
    series = json.loads(path.read_text()) if path.exists() else []
    if series and series[-1][0] == point[0]:
        series[-1] = point
    else:
        series.append(point)
    path.write_text(json.dumps(series[-max_points:], separators=(",", ":")))
