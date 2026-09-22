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
            "name": clean_name(e.get("displayName") or e.get("username")),
            "username": clean_name(e.get("username")) if e.get("username") and e.get("username") != e.get("displayName") else None,
            "bounty": e.get("rating"),
            "wins": e.get("wins"), "losses": e.get("losses"), "matches": e.get("matches"),
            "winRate": e.get("winRate"),
            "country": e.get("country"),
            "topLeaders": [
                {"code": strip_prefix(t.get("code")), "winRate": t.get("winRate"), "matches": t.get("matches")}
                for t in (e.get("topLeaders") or [])
            ],
        })
    return out


def build_pilots(opb: dict, players: list[dict]) -> dict[str, list[dict]]:
    """Leader-filtered ladder pages -> code -> [{id, name, leaderRank, rank, bounty, games, winRate, country}].
    games/winRate are the player's own with that leader (from their top-3 list); rank is the overall
    ladder rank when the player is in the pulled top range."""
    overall = {str(p["id"]): p["rank"] for p in players if p.get("id") is not None}
    out: dict[str, list[dict]] = {}
    for code, rows in (opb.get("pilots") or {}).items():
        if not isinstance(rows, list):
            continue
        shaped = []
        for e in rows:
            t = next((x for x in (e.get("topLeaders") or []) if strip_prefix(x.get("code")) == code), {})
            shaped.append({
                "id": str(e.get("playerId")), "name": clean_name(e.get("displayName") or e.get("username")),
                "leaderRank": e.get("rank"), "rank": overall.get(str(e.get("playerId"))), "bounty": e.get("rating"),
                "games": t.get("matches"), "winRate": t.get("winRate"), "country": e.get("country"),
            })
        out[strip_prefix(code)] = shaped
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
    path.parent.mkdir(parents=True, exist_ok=True)
    series = json.loads(path.read_text()) if path.exists() else []
    if series and series[-1][0] == point[0]:
        series[-1] = point
    else:
        series.append(point)
    path.write_text(json.dumps(series[-max_points:], separators=(",", ":")))


# ---------------------------------------------------------------- daily accumulation (30-day window)
def compact_daily(date: str, stats: list[dict], decklist: list[dict]) -> dict:
    """Reduce one day's leader stats + decklists to additive counts only.
    matchups: opp -> [games, wins, first_games, first_wins, second_games, second_wins]
    decks:    leader -> hash -> {"d": "4xOP01-016 3x...", "g": games, "w": wins, "p": pilots}"""
    total = 0
    leaders: dict[str, dict] = {}
    for row in stats or []:
        code = row.get("leaderKey")
        if not code:
            continue
        total = max(total, int(row.get("total_matches") or 0))
        mus = {}
        for m in row.get("matchups") or []:
            opp = m.get("opponentKey") or m.get("opponent")
            if not opp:
                continue
            mus[opp] = [m.get("total_games") or 0, m.get("wins") or 0, m.get("first_games") or 0, m.get("first_wins") or 0,
                        m.get("second_games") or 0, m.get("second_wins") or 0]
        leaders[code] = {"n": row.get("leaderName"), "g": row.get("number_of_matches") or 0, "w": row.get("wins") or 0, "m": mus}
    decks: dict[str, dict] = {}
    for row in decklist or []:
        code = row.get("leaderKey")
        if not code:
            continue
        bucket = decks.setdefault(code, {})
        for key in ("best_decklists", "most_played_decklists"):
            for d in row.get(key) or []:
                cards = parse_deck(d.get("decklist"), leader=code)
                if not cards:
                    continue
                h = deck_hash(cards)
                cur = bucket.get(h)
                g, w, p = d.get("total_games") or 0, d.get("wins") or 0, d.get("unique_pilots") or 0
                if cur is None or g > cur["g"]:  # the same list can appear in both arrays; keep one
                    bucket[h] = {"d": " ".join(f"{c['qty']}x{c['id']}" for c in cards), "g": g, "w": w, "p": p}
    return {"date": date, "total": total, "leaders": leaders, "decks": decks}


def build_window(dailies: list[dict], card_db: dict) -> tuple[dict, dict]:
    """Sum compact dailies into (leaders, decks_by_leader) in the same shapes build_leaders/build_decks emit,
    so the site can render any window with the same components."""
    total = sum(int(d.get("total") or 0) for d in dailies)
    acc: dict[str, dict] = {}
    deck_acc: dict[str, dict] = {}
    for day in dailies:
        for code, L in (day.get("leaders") or {}).items():
            a = acc.setdefault(code, {"name": L.get("n"), "g": 0, "w": 0, "m": defaultdict(lambda: [0, 0, 0, 0, 0, 0])})
            a["name"] = L.get("n") or a["name"]
            a["g"] += L.get("g") or 0
            a["w"] += L.get("w") or 0
            for opp, v in (L.get("m") or {}).items():
                for i in range(6):
                    a["m"][opp][i] += v[i] if i < len(v) else 0
        for code, bucket in (day.get("decks") or {}).items():
            da = deck_acc.setdefault(code, {})
            for h, d in bucket.items():
                cur = da.setdefault(h, {"d": d["d"], "g": 0, "w": 0, "p": 0})
                cur["g"] += d.get("g") or 0
                cur["w"] += d.get("w") or 0
                cur["p"] = max(cur["p"], d.get("p") or 0)

    def rate(w, g, digits=1):
        return round(100 * w / g, digits) if g else None

    leaders: dict[str, dict] = {}
    for code, a in acc.items():
        c = card_db.get(code, {})
        g, w = a["g"], a["w"]
        fg = sum(v[2] for v in a["m"].values()); fw = sum(v[3] for v in a["m"].values())
        sg = sum(v[4] for v in a["m"].values()); sw = sum(v[5] for v in a["m"].values())
        matchups = {}
        for opp, v in sorted(a["m"].items(), key=lambda kv: -kv[1][0]):
            matchups[opp] = {"kaizoku": {"games": v[0], "winRate": rate(v[1], v[0]), "firstWinRate": rate(v[3], v[2]),
                                         "secondWinRate": rate(v[5], v[4]), "firstGames": v[2], "secondGames": v[4]}}
        leaders[code] = {
            "code": code, "name": a["name"] or c.get("name") or code, "color": c.get("color"), "img": c.get("img"),
            "sources": {"kaizoku": {"matches": g, "wins": w, "winRate": rate(w, g),
                                    "weightedWinRate": round(100 * (w + 50) / (g + 100), 1) if g else None,
                                    "playRate": round(100 * g / total, 2) if total else None,
                                    "firstWinRate": rate(fw, fg), "secondWinRate": rate(sw, sg)}},
            "matchups": matchups, "deckCount": len(deck_acc.get(code, {})), "cardCount": 0,
        }
    decks_by_leader: dict[str, dict] = {}
    for code, da in deck_acc.items():
        out = []
        for h, d in da.items():
            cards = parse_deck(d["d"])
            out.append({"hash": h, "cards": cards, "size": sum(c["qty"] for c in cards),
                        "sources": {"kaizokuBest": {"games": d["g"], "wins": d["w"], "winRate": rate(d["w"], d["g"]), "pilots": d["p"]}}})
        out.sort(key=lambda x: -x["sources"]["kaizokuBest"]["games"])
        decks_by_leader[code] = {"leader": code, "decks": out, "topPilots": []}
    return leaders, decks_by_leader


# ---------------------------------------------------------------- OPBounty match archive windows
MAX_WINDOW_DECKS = 300
def build_archive_window(days: list[dict], card_db: dict, handles_by_day: dict[str, dict] | None = None,
                         links: dict | None = None) -> tuple[dict, dict]:
    """Sum data/matches day files into (leaders, decks_by_leader) using source key "ranked".

    leaders[code].sources.ranked = {matches, wins, winRate, weightedWinRate, playRate}
    leaders[code].matchups[opp].ranked = {games, winRate}
    decks_by_leader[code] = {"leader", "decks":[{hash, cards, size, sources:{ranked:{games,wins,winRate,pilots}}}],
                             "topPilots":[{handle,name,games,wins,winRate,bounty,ladderId}]}
    Pilots/handles come from the sampled combat-log heads (high-bounty games only)."""
    handles_by_day = handles_by_day or {}
    handle_to_ladder = {v["handle"]: k for k, v in (links or {}).items()}
    total = 0
    g = defaultdict(int); w = defaultdict(int)
    mu = defaultdict(lambda: defaultdict(lambda: [0, 0]))            # code -> opp -> [games, wins]
    dk = defaultdict(lambda: defaultdict(lambda: {"g": 0, "w": 0, "p": set()}))  # code -> hash -> stats
    deck_text: dict[str, str] = {}
    pilots = defaultdict(lambda: defaultdict(lambda: {"g": 0, "w": 0, "b": 0.0, "ts": ""}))  # code -> handle
    for day in days:
        deck_text.update(day.get("decks") or {})
        hmap = handles_by_day.get(day["date"], {})
        for m in day["matches"]:
            W, L = m["w"], m["l"]
            if not W.get("l") or not L.get("l"):
                continue  # a handful of documents lack a leader; they cannot be attributed
            total += 1
            g[W["l"]] += 1; g[L["l"]] += 1; w[W["l"]] += 1
            mu[W["l"]][L["l"]][0] += 1; mu[W["l"]][L["l"]][1] += 1
            mu[L["l"]][W["l"]][0] += 1
            seats = hmap.get(m["id"])
            sides = {}
            if seats and len(seats) == 2 and all(isinstance(s, list) for s in seats):
                (h1, l1), (h2, l2) = seats
                if W["l"] != L["l"]:
                    sides = {("w" if l1 == W["l"] else "l"): h1, ("w" if l2 == W["l"] else "l"): h2}
            for side, S in (("w", W), ("l", L)):
                if S["d"]:
                    d = dk[S["l"]][S["d"]]
                    d["g"] += 1; d["w"] += side == "w"
                    if side in sides:
                        d["p"].add(sides[side])
                if side in sides:
                    p = pilots[S["l"]][sides[side]]
                    p["g"] += 1; p["w"] += side == "w"
                    if (m["ts"] or "") >= p["ts"]:
                        p["ts"], p["b"] = m["ts"] or "", S["b"]

    def rate(x, n):
        return round(100 * x / n, 1) if n else None

    leaders: dict[str, dict] = {}
    for code in g:
        c = card_db.get(code, {})
        matchups = {}
        for opp, (mg, mw) in sorted(mu[code].items(), key=lambda kv: -kv[1][0]):
            matchups[opp] = {"ranked": {"games": mg, "winRate": rate(mw, mg)}}
        leaders[code] = {
            "code": code, "name": c.get("name") or code, "color": c.get("color"), "img": c.get("img"),
            "sources": {"ranked": {"matches": g[code], "wins": w[code], "winRate": rate(w[code], g[code]),
                                   "weightedWinRate": round(100 * (w[code] + 50) / (g[code] + 100), 1),
                                   "playRate": round(100 * g[code] / total, 2) if total else None}},
            "matchups": matchups, "deckCount": len(dk.get(code, {})), "cardCount": 0,
        }
    decks_by_leader: dict[str, dict] = {}
    for code in g:
        out = []
        for h, d in dk.get(code, {}).items():
            txt = deck_text.get(h)
            if not txt:
                continue
            cards = parse_deck(txt)
            out.append({"hash": h, "cards": cards, "size": sum(c["qty"] for c in cards),
                        "sources": {"ranked": {"games": d["g"], "wins": d["w"], "winRate": rate(d["w"], d["g"]), "pilots": len(d["p"]) or None}}})
        out.sort(key=lambda x: -x["sources"]["ranked"]["games"])
        out = out[:MAX_WINDOW_DECKS]  # the long tail is one-off lists; keep the site files small
        tp = []
        for h, p in pilots.get(code, {}).items():
            if p["g"] < 5:
                continue
            tp.append({"handle": h, "name": clean_name(h), "games": p["g"], "wins": p["w"], "winRate": rate(p["w"], p["g"]),
                       "bounty": round(p["b"]), "ladderId": handle_to_ladder.get(h)})
        tp.sort(key=lambda x: -((x["winRate"] or 0) * x["games"] + 50 * 20) / (x["games"] + 20) - (x["bounty"] - 3000) / 200)
        decks_by_leader[code] = {"leader": code, "decks": out, "topPilots": tp[:40], "deckCount": len(dk.get(code, {}))}
    return leaders, decks_by_leader


# ---------------------------------------------------------------- OPBounty published stats windows (complete)
def build_stats_window(days: list[dict], card_db: dict) -> tuple[dict, dict]:
    """Sum data/stats day files (OPBounty's own complete aggregates) into (leaders, decks_by_leader)
    under source key "ranked": games, wins, first/second win rates, matchups, and every distinct list
    with its own record.  No pilots here; merge_stats_archive adds those from the replay archive."""
    total = 0
    L = defaultdict(lambda: {"g": 0, "w": 0, "fw": 0, "fl": 0, "sw": 0, "sl": 0, "dur": 0.0})
    mu = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0, 0]))
    D: dict[str, dict] = {}
    text: dict[str, str] = {}
    for day in days:
        total += int(day.get("matches") or 0)
        for B in (day.get("brackets") or {}).values():
            for code, s in (B.get("leaders") or {}).items():
                a = L[code]
                for k in ("g", "w", "fw", "fl", "sw", "sl"):
                    a[k] += int(s.get(k) or 0)
                a["dur"] += float(s.get("dur") or 0)
                for opp, row in (s.get("mu") or {}).items():
                    m = mu[code][opp]
                    for j in range(6):
                        m[j] += int(row[j] if j < len(row) else 0)
        text.update(day.get("deckText") or {})
        for h, s in (day.get("decks") or {}).items():
            d = D.setdefault(h, {"l": s["l"], "g": 0, "w": 0, "fw": 0, "fl": 0, "sw": 0, "sl": 0})
            for k in ("g", "w", "fw", "fl", "sw", "sl"):
                d[k] += int(s.get(k) or 0)

    def rate(x, n):
        return round(100 * x / n, 1) if n else None

    leaders: dict[str, dict] = {}
    for code, a in L.items():
        c = card_db.get(code, {})
        matchups = {}
        for opp, m in sorted(mu[code].items(), key=lambda kv: -kv[1][0]):
            g, w, fw, fl, sw, sl = m
            matchups[opp] = {"ranked": {"games": g, "winRate": rate(w, g), "firstWinRate": rate(fw, fw + fl), "secondWinRate": rate(sw, sw + sl),
                                        "firstGames": fw + fl, "secondGames": sw + sl}}
        leaders[code] = {
            "code": code, "name": c.get("name") or code, "color": c.get("color"), "img": c.get("img"),
            "sources": {"ranked": {"matches": a["g"], "wins": a["w"], "winRate": rate(a["w"], a["g"]),
                                   "weightedWinRate": round(100 * (a["w"] + 500) / (a["g"] + 1000), 1),
                                   "playRate": round(100 * a["g"] / total, 2) if total else None,
                                   "firstWinRate": rate(a["fw"], a["fw"] + a["fl"]), "secondWinRate": rate(a["sw"], a["sw"] + a["sl"]),
                                   "firstGames": a["fw"] + a["fl"], "secondGames": a["sw"] + a["sl"],
                                   "avgDuration": round(a["dur"] / a["g"], 1) if a["g"] else None}},
            "matchups": matchups, "deckCount": 0, "cardCount": 0,
        }
    by_leader: dict[str, list] = defaultdict(list)
    for h, d in D.items():
        txt = text.get(h)
        if not txt:
            continue
        cards = parse_deck(txt)
        by_leader[d["l"]].append({"hash": h, "cards": cards, "size": sum(c["qty"] for c in cards),
                                  "sources": {"ranked": {"games": d["g"], "wins": d["w"], "winRate": rate(d["w"], d["g"]),
                                                         "firstWinRate": rate(d["fw"], d["fw"] + d["fl"]), "secondWinRate": rate(d["sw"], d["sw"] + d["sl"])}}})
    decks_by_leader: dict[str, dict] = {}
    for code in leaders:
        out = sorted(by_leader.get(code, []), key=lambda x: -x["sources"]["ranked"]["games"])
        leaders[code]["deckCount"] = len(out)
        decks_by_leader[code] = {"leader": code, "decks": out[:MAX_WINDOW_DECKS], "topPilots": [], "deckCount": len(out)}
    return leaders, decks_by_leader


def build_stats_cards(days: list[dict], code: str) -> dict | None:
    """Per-card usage and results for one leader from the published stats (complete, daily).
    Returns {"leader", "cards": [...], "leaderStats": {...}} in the site's CardFile shape, or None."""
    acc: dict[str, list[int]] = defaultdict(lambda: [0] * 8)
    lg = lw = 0
    lists: set[str] = set()
    for day in days:
        for B in (day.get("brackets") or {}).values():
            s = (B.get("leaders") or {}).get(code)
            if s:
                lg += int(s.get("g") or 0); lw += int(s.get("w") or 0)
        for key, row in ((day.get("cards") or {}).get(code) or {}).items():
            a = acc[key]
            for j in range(8):
                a[j] += int(row[j] if j < len(row) else 0)
        for h, d in (day.get("decks") or {}).items():
            if d.get("l") == code:
                lists.add(h)
    if not lg or not acc:
        return None
    by_card: dict[str, dict] = {}
    for key, (g, w, *_rest) in acc.items():
        m = CARD_RE.match(key)
        if not m or not g:
            continue
        qty, cid = int(m.group(1)), m.group(2).upper()
        c = by_card.setdefault(cid, {"g": 0, "w": 0, "copies": 0, "q": []})
        c["g"] += g; c["w"] += w; c["copies"] += qty * g
        c["q"].append({"qty": qty, "games": g, "winRate": round(100 * w / g, 1)})
    leader_wr = 100 * lw / lg
    cards = []
    for cid, c in by_card.items():
        wr = 100 * c["w"] / c["g"]
        cards.append({"id": cid, "games": c["g"], "usage": round(100 * c["g"] / lg, 1), "winRate": round(wr, 1),
                      "winRateVsLeader": round(wr - leader_wr, 2), "avgCopies": round(c["copies"] / c["g"], 2),
                      "firstWinRate": None, "secondWinRate": None, "quantities": sorted(c["q"], key=lambda q: q["qty"])})
    cards.sort(key=lambda c: -c["games"])
    return {"leader": code, "cards": cards, "openingHand": [], "tech": {},
            "leaderStats": {"games": lg, "winRate": round(leader_wr, 1), "uniqueDecklists": len(lists), "uniquePilots": None}}


def merge_stats_archive(st_leaders: dict, ar_leaders: dict, st_decks: dict, ar_decks: dict) -> tuple[dict, dict]:
    """Published stats are the numbers; the replay archive only adds who piloted what (pilots per list,
    top pilots per leader).  Leaders/lists only the archive knows are dropped: the published stats
    cover every game, so anything missing there did not happen in ranked."""
    if not st_leaders:
        return ar_leaders, ar_decks
    decks = {}
    for code, st in st_decks.items():
        ar = ar_decks.get(code, {})
        pilots = {d["hash"]: d["sources"].get("ranked", {}).get("pilots") for d in ar.get("decks", [])}
        for d in st["decks"]:
            p = pilots.get(d["hash"])
            if p:
                d["sources"]["ranked"]["pilots"] = p
        decks[code] = {**st, "topPilots": ar.get("topPilots", [])}
    return st_leaders, decks


def merge_windows(kz_leaders: dict, ar_leaders: dict, kz_decks: dict, ar_decks: dict) -> tuple[dict, dict]:
    """Archive numbers win; Card Kaizoku contributes 1st/2nd win rates (not in the archive) and any
    leaders/decks the archive lacks."""
    leaders = {}
    for code in set(kz_leaders) | set(ar_leaders):
        base = dict(ar_leaders.get(code) or kz_leaders[code])
        base["sources"] = {**(kz_leaders.get(code, {}).get("sources") or {}), **(ar_leaders.get(code, {}).get("sources") or {})}
        mus = {}
        for opp in set((kz_leaders.get(code, {}).get("matchups") or {})) | set((ar_leaders.get(code, {}).get("matchups") or {})):
            mus[opp] = {**(kz_leaders.get(code, {}).get("matchups", {}).get(opp) or {}), **(ar_leaders.get(code, {}).get("matchups", {}).get(opp) or {})}
        base["matchups"] = dict(sorted(mus.items(), key=lambda kv: -max((s.get("games") or 0) for s in kv[1].values())))
        leaders[code] = base
    decks = {}
    for code in set(kz_decks) | set(ar_decks):
        ar = ar_decks.get(code, {"leader": code, "decks": [], "topPilots": []})
        have = {d["hash"] for d in ar["decks"]}
        # Card Kaizoku sees the whole sim, so its distinct-pilot count per list beats the archive's sampled one
        kz_p = {d["hash"]: (d["sources"].get("kaizokuBest") or {}).get("pilots") for d in kz_decks.get(code, {}).get("decks", [])}
        for d in ar["decks"]:
            r = d["sources"].get("ranked")
            if r is not None and kz_p.get(d["hash"]):
                r["pilots"] = max(int(r.get("pilots") or 0), int(kz_p[d["hash"]]))
        extra = [d for d in kz_decks.get(code, {}).get("decks", []) if d["hash"] not in have]
        decks[code] = {"leader": code, "decks": ar["decks"] + extra, "topPilots": ar["topPilots"],
                       "deckCount": max(ar.get("deckCount", 0), len(ar["decks"]) + len(extra))}
        if code in leaders:
            leaders[code]["deckCount"] = decks[code]["deckCount"]
    return leaders, decks
