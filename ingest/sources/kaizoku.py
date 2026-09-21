"""Card Kaizoku CDN (cdn.cardkaizoku.com).

Daily snapshots indexed by manifest.json -> simStats.files[kind][dataset].
Files are ~1-17 MB; we skip any whose manifest date is unchanged since last run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..http import get_json

CDN = "https://cdn.cardkaizoku.com"
DATASET = os.environ.get("KAIZOKU_DATASET", "op17_lw_p")  # last week, private lobbies
KINDS = ["stats", "decklist", "cardstats", "leaderboard", "matchuptech", "hands"]


def fetch_all(state_path: Path, raw_dir: Path) -> dict:
    """Downloads changed files into raw_dir, returns {kind: data} for all kinds (using cached
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
