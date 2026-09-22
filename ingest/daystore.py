"""Per-day gzipped JSON store: <dir>/YYYY-MM-DD.json.gz, one object per UTC day.

Used for the OPBounty match archive (data/matches) and the published stats (data/stats)."""
from __future__ import annotations

import gzip
import json
from pathlib import Path


class DayStore:
    def __init__(self, root: Path):
        self.root = root

    def path(self, day: str) -> Path:
        return self.root / f"{day}.json.gz"

    def load(self, day: str) -> dict | None:
        p = self.path(day)
        return json.loads(gzip.decompress(p.read_bytes())) if p.exists() else None

    def save(self, obj: dict):
        self.root.mkdir(parents=True, exist_ok=True)
        self.path(obj["date"]).write_bytes(gzip.compress(json.dumps(obj, separators=(",", ":")).encode(), mtime=0))

    def load_days(self, days: int) -> list[dict]:
        """The most recent `days` day files, oldest first."""
        files = sorted(self.root.glob("*.json.gz"))[-days:] if self.root.exists() else []
        return [json.loads(gzip.decompress(f.read_bytes())) for f in files]
