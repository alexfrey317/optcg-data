"""OPBounty stats dashboard (stats.tcgmatchmaking.com).

Anonymous 2h JWT from POST /auth/session. Budget: ~12 requests per burst per IP,
so every call is spaced by OPB_INTERVAL seconds. Leaderboard pages cap at 200 rows.
"""
from __future__ import annotations

import os

from ..http import get_json, post_json

BASE = "https://stats.tcgmatchmaking.com"
INTERVAL = float(os.environ.get("OPB_INTERVAL", "2.0"))
LADDER_MODE = os.environ.get("OPB_MODE", "mode_0")  # Standard
TOP_PAGES = int(os.environ.get("OPB_TOP_PAGES", "5"))  # 5 x 200 = top 1000
PER_PAGE = 200
PILOT_LEADERS = int(os.environ.get("OPB_PILOT_LEADERS", "120"))  # leader-filtered ladder pages to pull (one request each)


class OPBounty:
    def __init__(self):
        self.token = post_json(f"{BASE}/auth/session")["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def _get(self, path: str):
        return get_json(f"{BASE}{path}", headers=self.headers, min_interval=INTERVAL)

    def sets(self) -> list[dict]:
        return self._get("/api/stats/sets")["sets"]

    def leaderboard(self, pages: int = TOP_PAGES, mode: str = LADDER_MODE) -> list[dict]:
        entries: list[dict] = []
        for page in range(1, pages + 1):
            data = self._get(f"/api/leaderboard/{mode}?page={page}&per_page={PER_PAGE}")
            entries.extend(data.get("entries", []))
            if page >= int(data.get("totalPages") or page):
                break
        return entries

    def leaders(self, set_name: str) -> dict:
        return self._get(f"/api/leaders/{set_name}")

    def matchups(self, set_name: str) -> dict:
        return self._get(f"/api/matchups/{set_name}")

    def decklists(self, set_name: str, leader_code: str) -> dict:
        return self._get(f"/api/decklists/{set_name}/{leader_code}")

    def leaderboard_meta(self, mode: str = LADDER_MODE) -> dict:
        return self._get("/api/leaderboard/meta")

    def leaderboard_for_leader(self, code: str, mode: str = LADDER_MODE, per_page: int = PER_PAGE) -> list[dict]:
        """Ladder rows whose most-played leaders include `code`, ranked among themselves (one page)."""
        data = self._get(f"/api/leaderboard/{mode}?page=1&per_page={per_page}&leader={code}")
        return data.get("entries", [])


def fetch_all() -> dict:
    """One daily pull. Returns raw dict; normalize.py turns it into site data."""
    api = OPBounty()
    sets = api.sets()
    set_name = next((s["name"] for s in sets if s["name"] == "lw"), sets[0]["name"])
    leaders = api.leaders(set_name)
    out = {
        "set": set_name,
        "sets": sets,
        "leaderboard": api.leaderboard(),
        "leaders": leaders,
        "matchups": api.matchups(set_name),
        "decklists": {},
    }
    for row in leaders.get("leaders", []):
        code = row["leader"]
        try:
            out["decklists"][code] = api.decklists(set_name, code)
        except Exception as e:  # one leader failing must not sink the run
            out["decklists"][code] = {"error": str(e)}
    # best pilots per leader: the ladder filtered by leader (complete, ranked by bounty among that leader's players)
    out["pilots"] = {}
    try:
        codes = [l["code"] for l in api.leaderboard_meta().get("leaders", []) if l.get("code")]
    except Exception as e:  # noqa: BLE001
        codes = []
        out["pilotsError"] = str(e)
    for code in codes[:PILOT_LEADERS]:
        try:
            out["pilots"][code] = api.leaderboard_for_leader(code)
        except Exception as e:  # noqa: BLE001
            out["pilots"][code] = {"error": str(e)}
    return out
