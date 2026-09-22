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

from . import link, normalize as N
from .http import get_json
from .sources import kaizoku, kaizoku_curves, kaizoku_players, opbounty, opbounty_matches, opbounty_profiles, opbounty_replays, opbounty_stats, optcgone

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
LATEST = ROOT / "data" / "latest"
HISTORY = ROOT / "data" / "history"
DAILY = ROOT / "data" / "daily"
# time windows the site offers, in finished UTC days; numbers come from the published stats (complete),
# pilots / mirror card impact from the replay archive (kept ARCHIVE_DAYS deep), 1st/2nd extras from Card Kaizoku
WINDOWS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
ARCHIVE_DAYS = 31


def dump(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))


def load(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", default="", help="comma list: opbounty,kaizoku,daily,optcgone,cards,players,matches,profiles,stats")
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

    def guard(name, fn, unavailable=False):
        """Run an optional stage that writes its own files and returns a status summary.
        Returns that summary, or None when the stage is skipped (--skip or `unavailable`) or fails."""
        if unavailable or name in skip:
            status[name] = "skipped"
            return None
        result = None
        try:
            result = fn()
            status[name] = "ok"
        except Exception as e:  # noqa: BLE001 - keep going; the rest of the site still rebuilds
            traceback.print_exc()
            status[name] = f"error: {e}"
        print(f"[{name}] {status[name]} {json.dumps(result) if result else ''} ({time.time() - t0:.0f}s)", file=sys.stderr)
        return result

    step("kaizoku", lambda: kaizoku.fetch_all(RAW / "kaizoku_state.json", RAW))
    daily_status = guard("daily", lambda: kaizoku.fetch_daily(DAILY))
    step("optcgone", optcgone.fetch_all)
    step("cards", lambda: get_json(f"{kaizoku.CDN}/card_data.json"))
    step("curves", lambda: kaizoku_curves.fetch_all(RAW / "kaizoku_curves_state.json", LATEST / "curves"))
    step("opbounty", opbounty.fetch_all)

    # OPBounty ranked match archive (Firestore). Needs OPB_FS_EMAIL/OPB_FS_PASSWORD; skipped otherwise.
    matches_status = guard("matches", opbounty_matches.fetch_all, unavailable=not opbounty_matches.EMAIL)

    # OPBounty published ranked stats (public bucket, complete): leaders, matchups, every decklist with its record
    stats_status = guard("stats", opbounty_stats.fetch_all)

    opb, kz, one = raw["opbounty"], raw["kaizoku"], raw["optcgone"]
    card_db = N.build_card_db(raw["cards"]) if raw.get("cards") else load(LATEST / "cards.json", {})

    players = N.build_players(opb)

    # OPBounty personal profiles (Firestore PublicUsers): complete season record + per-leader stats per ladder player
    profiles_status = guard("profiles", lambda: opbounty_profiles.fetch_all(players), unavailable=not opbounty_matches.EMAIL)
    leaders = N.build_leaders(opb, kz, card_db)
    used_cards: set[str] = set(leaders)
    pilots = N.build_pilots(opb, players)
    if pilots:  # keep the previous day's files when the leader-filtered ladder pull failed entirely
        for f in (LATEST / "pilots").glob("*.json") if (LATEST / "pilots").exists() else []:
            if f.stem not in pilots:
                f.unlink()
        for code, rows in pilots.items():
            dump(LATEST / "pilots" / f"{code}.json", rows)
            used_cards.add(code)

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

    # link ladder rows to sim handles via the match archive, and write per-player match histories
    archive_days = link.load_days(ARCHIVE_DAYS) if link.MATCHES.exists() else []
    # study games: full combat logs for the best finished games of every contender matchup (data/replays, cached in CI)
    replays_status = guard("replays", lambda: opbounty_replays.fetch_all([d for d in archive_days if d.get("date", "") < today][-opbounty_replays.WINDOW_DAYS:]),
                           unavailable=not opbounty_matches.EMAIL or not archive_days)
    links = {}
    if archive_days:
        try:
            series, deck_text = link.handle_series(archive_days)
            # the ladder snapshot time: now when we just pulled it, else whenever the cached one was pulled
            snap_ts = datetime.now(timezone.utc).isoformat(timespec="seconds") if status.get("opbounty") == "ok" \
                else (load(LATEST / "meta.json", {}).get("generatedAt") or datetime.now(timezone.utc).isoformat(timespec="seconds"))
            links = link.link(players, series, snap_ts[:19], load(LATEST / "handles.json", {}))
            link.write_player_matches(links, series, deck_text)
            dump(LATEST / "handles.json", links)
            status["link"] = f"ok {len(links)}/{len(players)} linked, {len(series)} handles"
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            status["link"] = f"error: {e}"
        print(f"[link] {status['link']} ({time.time() - t0:.0f}s)", file=sys.stderr)
    # per-player exact decklists (Card Kaizoku player routes)
    player_stats = guard("players", lambda: kaizoku_players.fetch_all(players, LATEST / "player_ids.json", LATEST / "players", today, links))
    # drop per-player files for players no longer in the pulled range, so stale decks never linger
    keep = {str(p["id"]) for p in players if p.get("id")}
    if (LATEST / "players").exists():
        for f in (LATEST / "players").glob("*.json"):
            if f.stem not in keep:
                f.unlink()
    for f in (LATEST / "players").glob("*.json") if (LATEST / "players").exists() else []:
        for d in load(f, {}).get("decks", []):
            used_cards.update(c["id"] for c in d["cards"])

    handles_by_day = {d["date"]: load(link.HANDLES / f"{d['date']}.json", {}) for d in archive_days}

    # windows: match archive (games, matchups, decks, pilots) + Card Kaizoku dailies (1st/2nd, card stats stay weekly)
    window_meta = {}
    daily_files = sorted(DAILY.glob("*.json")) if DAILY.exists() else []
    # windows cover the last N *finished* UTC days: today's partial day would make the numbers drift with the run time
    stats_days_all = [d for d in opbounty_stats.load_days(max(WINDOWS.values()) + 1) if d.get("date", "") < today]
    archive_full = [d for d in archive_days if d.get("date", "") < today]
    daily_full = [f for f in daily_files if f.stem < today]
    for wname, days in WINDOWS.items():
        kz_days = [load(f, {}) for f in daily_full[-days:]]
        ar_days = archive_full[-days:]
        st_days = stats_days_all[-days:]
        if not kz_days and not ar_days and not st_days:
            continue
        kz_leaders, kz_decks = N.build_window(kz_days, card_db) if kz_days else ({}, {})
        ar_leaders, ar_decks = N.build_archive_window(ar_days, card_db, handles_by_day, links) if ar_days else ({}, {})
        st_leaders, st_decks = N.build_stats_window(st_days, card_db) if st_days else ({}, {})
        r_leaders, r_decks = N.merge_stats_archive(st_leaders, ar_leaders, st_decks, ar_decks)
        w_leaders, w_decks = N.merge_windows(kz_leaders, r_leaders, kz_decks, r_decks)
        wdir = ROOT / "data" / "windows" / wname
        for code, dfile in w_decks.items():
            dump(wdir / "decks" / f"{code}.json", dfile)
            for d in dfile["decks"]:
                used_cards.update(c["id"] for c in d["cards"])
        # per-card stats for the window from the complete published files; Card Kaizoku's weekly tech/hands/pilots ride
        # along; mirror card impact comes from the archive days inside the window
        mirrors = N.build_mirrors(ar_days) if ar_days else {}
        trends = N.build_trends(st_days) if st_days else {}
        for code in st_leaders:
            cf = N.build_stats_cards(st_days, code)
            if not cf:
                continue
            weekly = load(LATEST / "cards" / f"{code}.json", {})
            cf["tech"] = weekly.get("tech") or {}
            cf["openingHand"] = weekly.get("openingHand") or []
            wk = {c["id"]: c for c in weekly.get("cards") or []}
            for c in cf["cards"]:
                c["pilots"] = (wk.get(c["id"]) or {}).get("pilots")
            cf["leaderStats"]["uniquePilots"] = (weekly.get("leaderStats") or {}).get("uniquePilots")
            cf["leaderStats"]["weeklyLists"] = (weekly.get("leaderStats") or {}).get("uniqueDecklists")
            cf["mirror"] = mirrors.get(code) or {"games": 0, "withDecks": 0, "cards": []}
            cf["mirror"]["archiveDays"] = len(ar_days)
            dump(wdir / "cards" / f"{code}.json", cf)
            used_cards.update(c["id"] for c in cf["cards"])
            used_cards.update(c["id"] for c in cf["mirror"]["cards"])
        for code, L in w_leaders.items():
            L["trend"] = trends.get(code) or []
        used_cards.update(w_leaders)
        dump(wdir / "leaders.json", w_leaders)
        span = st_days or ar_days or kz_days
        window_meta[wname] = {"days": len(span), "targetDays": days, "start": span[0].get("date"), "end": span[-1].get("date"),
                              "matches": sum(int(d.get("matches") or 0) for d in st_days) if st_days
                              else sum(len(d.get("matches") or []) for d in ar_days) if ar_days else sum(int(d.get("total") or 0) for d in kz_days),
                              "statsDays": len(st_days), "archiveDays": len(ar_days), "kaizokuDays": len(kz_days),
                              "archiveMatches": sum(len(d.get("matches") or []) for d in ar_days)}
        dump(wdir / "meta.json", window_meta[wname])

    # cards named in the curve plays need names/images too
    for f in (LATEST / "curves").glob("*.json") if (LATEST / "curves").exists() else []:
        for side in ("first", "second"):
            for t in load(f, {}).get(side) or []:
                for play in t.get("top", []) + [p for v in t.get("vs", {}).values() for p in v]:
                    used_cards.update(c for c in play["c"].split("+") if c and c != "NO_PLAY")

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
        "daily": daily_status,
        "matches": matches_status,
        "replays": replays_status,
        "profiles": profiles_status,
        "stats": stats_status,
        "linked": len(links),
        "windows": window_meta,
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
