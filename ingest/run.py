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
from .sources import kaizoku, kaizoku_players, opbounty, opbounty_matches, opbounty_profiles, opbounty_stats, optcgone

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
LATEST = ROOT / "data" / "latest"
HISTORY = ROOT / "data" / "history"
DAILY = ROOT / "data" / "daily"
WINDOWS = {"7d": 7, "30d": 30}  # built from the match archive + Card Kaizoku dailies; the site reads these


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

    step("kaizoku", lambda: kaizoku.fetch_all(RAW / "kaizoku_state.json", RAW))
    daily_status = None
    if "daily" in skip:
        status["daily"] = "skipped"
    else:
        try:
            daily_status = kaizoku.fetch_daily(DAILY)
            status["daily"] = "ok"
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            status["daily"] = f"error: {e}"
        print(f"[daily] {status['daily']} {json.dumps(daily_status) if daily_status else ''} ({time.time() - t0:.0f}s)", file=sys.stderr)
    step("optcgone", optcgone.fetch_all)
    step("cards", lambda: get_json(f"{kaizoku.CDN}/card_data.json"))
    step("opbounty", opbounty.fetch_all)

    # OPBounty ranked match archive (Firestore). Needs OPB_FS_EMAIL/OPB_FS_PASSWORD; skipped otherwise.
    matches_status = None
    if "matches" in skip or not opbounty_matches.EMAIL:
        status["matches"] = "skipped"
    else:
        try:
            matches_status = opbounty_matches.fetch_all()
            status["matches"] = "ok"
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            status["matches"] = f"error: {e}"
        print(f"[matches] {status['matches']} {json.dumps(matches_status) if matches_status else ''} ({time.time() - t0:.0f}s)", file=sys.stderr)

    # OPBounty published ranked stats (public bucket, complete): leaders, matchups, every decklist with its record
    stats_status = None
    if "stats" in skip:
        status["stats"] = "skipped"
    else:
        try:
            stats_status = opbounty_stats.fetch_all()
            status["stats"] = "ok"
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            status["stats"] = f"error: {e}"
        print(f"[stats] {status['stats']} {json.dumps(stats_status) if stats_status else ''} ({time.time() - t0:.0f}s)", file=sys.stderr)

    opb, kz, one = raw["opbounty"], raw["kaizoku"], raw["optcgone"]
    card_db =N.build_card_db(raw["cards"]) if raw.get("cards") else load(LATEST / "cards.json", {})

    players = N.build_players(opb)

    # OPBounty personal profiles (Firestore PublicUsers): complete season record + per-leader stats per ladder player
    profiles_status = None
    if "profiles" in skip or not opbounty_matches.EMAIL:
        status["profiles"] = "skipped"
    else:
        try:
            profiles_status = opbounty_profiles.fetch_all(players)
            status["profiles"] = "ok"
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            status["profiles"] = f"error: {e}"
        print(f"[profiles] {status['profiles']} {json.dumps(profiles_status) if profiles_status else ''} ({time.time() - t0:.0f}s)", file=sys.stderr)
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

    # link ladder rows to sim handles via the match archive, and write per-player match histories
    archive_days = link.load_days(max(WINDOWS.values()) + 1) if link.MATCHES.exists() else []
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
    handles_by_day = {d["date"]: load(link.HANDLES / f"{d['date']}.json", {}) for d in archive_days}

    # windows: match archive (games, matchups, decks, pilots) + Card Kaizoku dailies (1st/2nd, card stats stay weekly)
    window_meta = {}
    daily_files = sorted(DAILY.glob("*.json")) if DAILY.exists() else []
    stats_days_all = opbounty_stats.load_days(max(WINDOWS.values()) + 1)
    for wname, days in WINDOWS.items():
        kz_days = [load(f, {}) for f in daily_files[-days:]]
        ar_days = archive_days[-days:]
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
        used_cards.update(w_leaders)
        dump(wdir / "leaders.json", w_leaders)
        span = st_days or ar_days or kz_days
        window_meta[wname] = {"days": len(span), "targetDays": days, "start": span[0].get("date"), "end": span[-1].get("date"),
                              "matches": sum(int(d.get("matches") or 0) for d in st_days) if st_days
                              else sum(len(d.get("matches") or []) for d in ar_days) if ar_days else sum(int(d.get("total") or 0) for d in kz_days),
                              "statsDays": len(st_days), "archiveDays": len(ar_days), "kaizokuDays": len(kz_days),
                              "archiveMatches": sum(len(d.get("matches") or []) for d in ar_days)}
        dump(wdir / "meta.json", window_meta[wname])

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
