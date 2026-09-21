"""Card Kaizoku CDN (cdn.cardkaizoku.com).

Weekly snapshots are indexed by manifest.json -> simStats.files[kind][dataset]; daily ("yesterday",
`_y_p`) snapshots are dated files kept for ~8 days. We pull the weekly set for the 7-day view and
accumulate the daily set into data/daily/ so longer windows can be summed locally.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..http import HttpError, get_json
from .. import normalize as N

CDN = "https://cdn.cardkaizoku.com"
DATASET = os.environ.get("KAIZOKU_DATASET", "op17_lw_p")  # last week, private lobbies
SET = DATASET.split("_")[0]                                 # e.g. op17
DAILY_DATASET = f"{SET}_y_p"                                 # yesterday, private lobbies
KINDS = ["stats", "decklist", "cardstats", "leaderboard", "matchuptech", "hands"]
DAILY_KINDS = ["stats", "decklist"]                          # only these are published daily
DAILY_LOOKBACK = int(os.environ.get("KAIZOKU_DAILY_LOOKBACK", "12"))


def fetch_all(state_path: Path, raw_dir: Path) -> dict:
    """Downloads changed weekly files into raw_dir, returns {kind: data} for all kinds (using cached
    raw files when unchanged). state_path remembers the manifest date per file."""
    manifest = get_json(f"{CDN}/manifest.json?t=0")
    files = manifest["simStats"]["files"]
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    out: dict = {"manifest_date": manifest["simStats"].get("date"), "dataset": DATASET, "files": {}}

    for kind in KINDS:
        entry = (files.get(kind) or {}).get(DATASET)
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


def fetch_daily(daily_dir: Path, lookback: int = DAILY_LOOKBACK) -> dict:
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
            stats = get_json(f"{CDN}/stats/stats_{DAILY_DATASET}_{stamp}.json?v={stamp}")
            decks = get_json(f"{CDN}/stats/decklist_{DAILY_DATASET}_{stamp}.json?v={stamp}")
        except HttpError as e:
            if e.status == 404:
                missing.append(game_date.isoformat())
                continue
            raise
        out.write_text(json.dumps(N.compact_daily(game_date.isoformat(), stats, decks), separators=(",", ":"), ensure_ascii=False))
        fetched.append(game_date.isoformat())
    have = sorted(p.stem for p in daily_dir.glob("*.json"))
    return {"fetched": fetched, "missing": missing, "have": len(have), "first": have[0] if have else None, "last": have[-1] if have else None}
