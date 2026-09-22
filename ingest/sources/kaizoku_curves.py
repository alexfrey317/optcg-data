"""Card Kaizoku weekly curve analysis (cdn.cardkaizoku.com/stats/curve_lw_<date>.json).

For every leader, turn number and turn order (going first / second) Card Kaizoku publishes the three
most common plays that turn, overall and against each opponent leader, counted from the sim's own
match logs over the last week.  A play is one card code, several codes joined with "+" for a
multi-card turn, or NO_PLAY.  We keep the ranked pool (gameMode 0) only and write one compact file
per leader:

data/latest/curves/<leader>.json
  {"leader", "date", "first": [turn...], "second": [turn...]}
  turn = {"t": n, "n": turns recorded, "top": [{"c": combo, "n": count, "r": rate}],
          "vs": {opp: [{"c","n","r"}]}}      # vs limited to the most-played opponents
"""
from __future__ import annotations

import json
from pathlib import Path

from ..http import get_json
from .kaizoku import CDN

MAX_TURNS = 12          # later turns are rare and noisy
MAX_OPPS = 24           # opponents kept per leader (by recorded turns)
MIN_TURN_SHARE = 0.03   # drop turns reached in under 3% of games


def _compact(side: list[dict], keep_opps: set[str]) -> list[dict]:
    if not side:
        return []
    side = sorted(side, key=lambda t: t.get("turn") or 0)
    t1 = next((t for t in side if t.get("overall_top3")), None)
    base = (t1["overall_top3"][0].get("total_turns_all") or 0) if t1 else 0
    out = []
    for t in side[:MAX_TURNS]:
        top = t.get("overall_top3") or []
        n = top[0].get("total_turns_all") if top else 0
        if not n or (base and n < base * MIN_TURN_SHARE):
            continue
        vs = {}
        for o in t.get("vs_opp") or []:
            opp = o.get("opp_leader")
            if opp in keep_opps and o.get("top3"):
                vs[opp] = [{"c": x["combo"], "n": x["count"], "r": round(100 * x["rate_all"], 1)} for x in o["top3"]]
        out.append({"t": t["turn"], "n": n, "top": [{"c": x["combo"], "n": x["count"], "r": round(100 * x["rate_all"], 1)} for x in top], "vs": vs})
    return out


def _opps(entry: dict) -> set[str]:
    """Opponents ranked by how many turns were recorded against them (turn 1, either seat)."""
    seen: dict[str, int] = {}
    for side in ("first", "second"):
        for t in entry.get(side) or []:
            if (t.get("turn") or 0) > 2:
                continue
            for o in t.get("vs_opp") or []:
                top = o.get("top3") or []
                if top:
                    seen[o["opp_leader"]] = max(seen.get(o["opp_leader"], 0), top[0].get("total_turns_all") or 0)
    return {k for k, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:MAX_OPPS]}


def fetch_all(state_path: Path, out_dir: Path) -> dict:
    manifest = get_json(f"{CDN}/manifest.json?t=0")
    entry = ((manifest.get("simStats") or {}).get("files") or {}).get("curve", {}).get("lw")
    if not entry:
        return {"status": "no curve file in manifest"}
    rel, date = entry["current"], entry["date"]
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("curve") == date and out_dir.exists() and any(out_dir.glob("*.json")):
        return {"date": date, "changed": False, "leaders": len(list(out_dir.glob("*.json")))}
    data = get_json(f"{CDN}/{rel}?v={date}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.json"):
        f.unlink()
    n = 0
    for e in data:
        if e.get("gameMode") != 0 or not e.get("leader"):
            continue
        keep = _opps(e)
        obj = {"leader": e["leader"], "date": date, "first": _compact(e.get("first") or [], keep), "second": _compact(e.get("second") or [], keep)}
        if not obj["first"] and not obj["second"]:
            continue
        (out_dir / f"{e['leader']}.json").write_text(json.dumps(obj, separators=(",", ":")))
        n += 1
    state["curve"] = date
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1))
    return {"date": date, "changed": True, "leaders": n}
