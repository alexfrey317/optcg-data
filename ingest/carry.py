"""Carry ladder records across OPBounty seasons.

OPBounty resets its ranked ladder at the start of a season (seen 2026-10-27: every player back to 0W/0L, bounties
compressed). Our ladder keeps adding instead: the day a reset is detected, every player's last pre-reset record (their
profile when it was current, else their ladder row) is banked here, and from then on the site shows banked + current.

data/latest/carry.json
  {"seasons": [{"detected": "YYYY-MM-DD", "players": N}],
   "players": {id: {"wins", "losses", "matches", "leaders": {code: {"games", "wins"}}}}}

Player rows get two extra fields: "season" (this season only, straight from the ladder) and "carry" (the banked part),
while wins / losses / matches / winRate / topLeaders become the sum.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

RESET_SHARE = 0.5  # a reset is when at least this share of players seen on both days lost games overnight


def _rec(p: dict) -> dict:
    """A ladder row's current-season part (rows already carrying have it under 'season')."""
    return p.get("season") or {"wins": p.get("wins"), "losses": p.get("losses"), "matches": p.get("matches"),
                               "winRate": p.get("winRate"), "topLeaders": p.get("topLeaders") or []}


def reset_detected(players: list[dict], prev: list[dict]) -> bool:
    before = {str(p["id"]): _rec(p) for p in prev if p.get("id") is not None}
    pairs = [(before[str(p["id"])], p) for p in players if p.get("id") is not None and str(p["id"]) in before]
    pairs = [(b, c) for b, c in pairs if (b.get("matches") or 0) > 0]
    if len(pairs) < 100:
        return False
    dropped = sum(1 for b, c in pairs if (c.get("matches") or 0) < (b.get("matches") or 0))
    return dropped / len(pairs) >= RESET_SHARE


def _bank(carry: dict, pid: str, wins, losses, matches, leaders: list[tuple[str, int, int]]):
    c = carry["players"].setdefault(pid, {"wins": 0, "losses": 0, "matches": 0, "leaders": {}})
    c["wins"] += int(wins or 0); c["losses"] += int(losses or 0); c["matches"] += int(matches or 0)
    for code, g, w in leaders:
        L = c["leaders"].setdefault(code, {"games": 0, "wins": 0})
        L["games"] += int(g or 0); L["wins"] += int(w or 0)


def bank_season(carry: dict, prev: list[dict], profiles_dir: Path, today: str) -> int:
    """Add every previous-day player's season record to the bank. The profile wins when it reported at least as
    many games as the ladder (same rule the site uses), because the ladder only counts the top three leaders."""
    n = 0
    for p in prev:
        if p.get("id") is None:
            continue
        pid, rec = str(p["id"]), _rec(p)
        prof = None
        f = profiles_dir / f"{pid}.json"
        if f.exists():
            try:
                prof = json.loads(f.read_text())
            except ValueError:
                prof = None
        if prof and (prof.get("games") or 0) >= (rec.get("matches") or 0) and (prof.get("games") or 0) > 0:
            leaders = [(l["code"], l.get("games"), l.get("wins")) for l in prof.get("leaders") or [] if l.get("code")]
            _bank(carry, pid, prof.get("wins"), prof.get("losses"), prof.get("games"), leaders)
            n += 1
        elif (rec.get("matches") or 0) > 0:
            leaders = [(t["code"], t.get("matches"), round((t.get("matches") or 0) * (t.get("winRate") or 0) / 100))
                       for t in rec.get("topLeaders") or [] if t.get("code")]
            _bank(carry, pid, rec.get("wins"), rec.get("losses"), rec.get("matches"), leaders)
            n += 1
    carry["seasons"].append({"detected": today, "players": n})
    return n


def apply(players: list[dict], carry: dict):
    """Rewrite ladder rows in place as banked + this season."""
    for p in players:
        season = _rec(p)
        p["season"] = season
        c = carry["players"].get(str(p.get("id")))
        if not c:
            p.pop("carry", None)
            continue
        p["carry"] = {"wins": c["wins"], "losses": c["losses"], "matches": c["matches"], "leaders": c["leaders"]}
        p["wins"] = int(season.get("wins") or 0) + c["wins"]
        p["losses"] = int(season.get("losses") or 0) + c["losses"]
        p["matches"] = int(season.get("matches") or 0) + c["matches"]
        p["winRate"] = round(100 * p["wins"] / p["matches"], 1) if p["matches"] else 0.0
        merged: dict[str, dict] = {}
        for t in season.get("topLeaders") or []:
            g = int(t.get("matches") or 0)
            merged[t["code"]] = {"games": g, "wins": round(g * (t.get("winRate") or 0) / 100)}
        for code, L in c["leaders"].items():
            m = merged.setdefault(code, {"games": 0, "wins": 0})
            m["games"] += L["games"]; m["wins"] += L["wins"]
        p["topLeaders"] = [{"code": code, "matches": m["games"], "winRate": round(100 * m["wins"] / m["games"], 1) if m["games"] else 0.0}
                           for code, m in sorted(merged.items(), key=lambda kv: -kv[1]["games"]) if m["games"]][:3]


def apply_pilots(pilots: dict[str, list[dict]], carry: dict):
    """Leader-filtered ladder rows (games / winRate with one leader) become banked + this season too."""
    for code, rows in pilots.items():
        for r in rows:
            L = (carry["players"].get(str(r.get("id"))) or {}).get("leaders", {}).get(code)
            if not L:
                continue
            g = int(r.get("games") or 0)
            w = round(g * (r.get("winRate") or 0) / 100) + L["wins"]
            r["games"] = g + L["games"]
            r["winRate"] = round(100 * w / r["games"], 1) if r["games"] else 0.0


def run(players: list[dict], prev: list[dict], carry_path: Path, profiles_dir: Path) -> tuple[dict, dict]:
    """Detect a reset against yesterday's rows, bank if needed, then apply the bank. Returns (status, bank)."""
    carry = {"seasons": [], "players": {}}
    if carry_path.exists():
        carry = json.loads(carry_path.read_text())
    today = datetime.now(timezone.utc).date().isoformat()
    banked = 0
    if reset_detected(players, prev) and not any(s["detected"] == today for s in carry["seasons"]):
        banked = bank_season(carry, prev, profiles_dir, today)
        carry_path.write_text(json.dumps(carry, separators=(",", ":")))
    apply(players, carry)
    return {"seasons": len(carry["seasons"]), "banked": banked, "carrying": sum(1 for p in players if p.get("carry"))}, carry
