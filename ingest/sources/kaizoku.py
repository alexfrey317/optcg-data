"""Card Kaizoku CDN (cdn.cardkaizoku.com).

Weekly snapshots are indexed by manifest.json -> simStats.files[kind][dataset]; daily ("yesterday",
`_y_p`) snapshots are dated files kept for ~8 days. We pull the weekly set for the 7-day view and
accumulate the daily set into data/daily/ so longer windows can be summed locally.

Datasets are named after the newest legal set (op17_lw_p, eb05_lw_p, ...) and Kaizoku stops updating the old
name the day a set releases (seen 2026-11-07, OP17 -> EB05). The current format is therefore detected from the
manifest on every run (see current_format) and remembered in data/latest/format.json with the day it started, so
the windows can be clamped to the format and the next set is picked up without a code change.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..http import HttpError, get_json
from .. import normalize as N

CDN = "https://cdn.cardkaizoku.com"
DATASET = os.environ.get("KAIZOKU_DATASET")                 # override the detected format, e.g. "op17_lw_p"
SET_RE = re.compile(r"^(op|eb|st)\d{2}$")                     # set-named datasets; "west", "east", "no_mihawk" etc. are not formats
KINDS = ["stats", "decklist", "cardstats", "leaderboard", "matchuptech", "hands"]
DAILY_KINDS = ["stats", "decklist"]                          # only these are published daily
DAILY_LOOKBACK = int(os.environ.get("KAIZOKU_DAILY_LOOKBACK", "12"))


def _weekly_games(entry: dict) -> int:
    try:
        rows = get_json(f"{CDN}/{entry['current']}?v={entry.get('date')}")
    except HttpError:
        return 0
    return max((int(r.get("total_matches") or 0) for r in rows or []), default=0)


def current_format(files: dict, format_path: Path, today: str) -> dict:
    """Which set-named `<set>_lw_p` dataset is the current Standard format, and since when.

    Every set dataset still being updated on the manifest date is a candidate (the pre-release set gets its own, so
    there can be two); the current one is the newest set, by set number within OP/EB. The first day each set was
    seen on the manifest is kept in format.json so that, when the current set changes, the format start is the day
    before its first weekly file (a file dated D holds games through D-1)."""
    stats = files.get("stats") or {}
    latest = max((e.get("date") or "" for e in stats.values()), default="")
    state = json.loads(format_path.read_text()) if format_path.exists() else {"set": None, "dataset": None, "start": None, "seen": {}}
    seen = state.setdefault("seen", {})
    for ds, e in stats.items():
        parts = ds.split("_")
        if len(parts) == 3 and parts[1:] == ["lw", "p"] and SET_RE.match(parts[0]):
            first = f"{e['date'][:4]}-{e['date'][4:6]}-{e['date'][6:]}" if e.get("date") else today
            seen[parts[0]] = min(seen.get(parts[0], first), first)
    live = {ds.split("_")[0]: e for ds, e in stats.items()
            if ds.endswith("_lw_p") and ds.count("_") == 2 and SET_RE.match(ds.split("_")[0]) and e.get("date") == latest}
    if DATASET:
        chosen = DATASET.split("_")[0]
    elif live:
        # the pre-release set has a dataset too (op18 while eb05 is Standard), so the Standard set is the one players are
        # actually on: the candidate with the most games in its weekly file. After a release the new set overtakes within days.
        chosen = max(live, key=lambda st: _weekly_games(live[st]))
    else:
        chosen = state.get("set") or "op17"
    if chosen != state.get("set"):
        first_seen = seen.get(chosen, today)
        start = (datetime.fromisoformat(first_seen) - timedelta(days=1)).date().isoformat()
        state.update({"set": chosen, "dataset": f"{chosen}_lw_p", "start": start, "changed": today})
    state["dataset"] = f"{chosen}_lw_p"
    format_path.parent.mkdir(parents=True, exist_ok=True)
    format_path.write_text(json.dumps(state, indent=1, sort_keys=True))
    return state


def fetch_all(state_path: Path, raw_dir: Path, format_path: Path) -> dict:
    """Downloads changed weekly files into raw_dir, returns {kind: data} for all kinds (using cached
    raw files when unchanged). state_path remembers the manifest date per file."""
    manifest = get_json(f"{CDN}/manifest.json?t=0")
    files = manifest["simStats"]["files"]
    fmt = current_format(files, format_path, datetime.now(timezone.utc).date().isoformat())
    dataset = fmt["dataset"]
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("dataset") != dataset:  # a new format: every cached weekly file belongs to the old one
        state = {"dataset": dataset}
    out: dict = {"manifest_date": manifest["simStats"].get("date"), "dataset": dataset, "format": fmt, "files": {}}

    for kind in KINDS:
        entry = (files.get(kind) or {}).get(dataset)
        if not entry:
            continue
        rel, date = entry["current"], entry["date"]
        local = raw_dir / f"kaizoku_{kind}.json"
        if state.get(kind) == date and local.exists():
            out["files"][kind] = {"path": rel, "date": date, "changed": False}
            out[kind] = json.loads(local.read_text())
            continue
        data = get_json(f"{CDN}/{rel}?v={date}")
        local.write_text(json.dumps(data))
        state[kind] = date
        out["files"][kind] = {"path": rel, "date": date, "changed": True}
        out[kind] = data

    state_path.write_text(json.dumps(state, indent=1))
    return out


def fetch_daily(daily_dir: Path, set_name: str, lookback: int = DAILY_LOOKBACK) -> dict:
    """Fetch any daily snapshots from the last `lookback` days that we do not already hold, compact them,
    and write data/daily/YYYY-MM-DD.json. A file dated D holds the games played on D-1; we key by D-1
    (the day the games happened). Returns {fetched: [...], missing: [...], have: n}."""
    daily_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    fetched, missing = [], []
    for back in range(1, lookback + 1):
        file_date = today - timedelta(days=back - 1)          # snapshot date printed in the filename
        game_date = file_date - timedelta(days=1)
        out = daily_dir / f"{game_date.isoformat()}.json"
        if out.exists():
            continue
        stamp = file_date.strftime("%Y%m%d")
        try:
            stats = get_json(f"{CDN}/stats/stats_{set_name}_y_p_{stamp}.json?v={stamp}")
            decks = get_json(f"{CDN}/stats/decklist_{set_name}_y_p_{stamp}.json?v={stamp}")
        except HttpError as e:
            if e.status == 404:
                missing.append(game_date.isoformat())
                continue
            raise
        out.write_text(json.dumps(N.compact_daily(game_date.isoformat(), stats, decks), separators=(",", ":"), ensure_ascii=False))
        fetched.append(game_date.isoformat())
    have = sorted(p.stem for p in daily_dir.glob("*.json"))
    return {"fetched": fetched, "missing": missing, "have": len(have), "first": have[0] if have else None, "last": have[-1] if have else None}
