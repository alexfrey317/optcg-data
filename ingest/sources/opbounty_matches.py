"""OPBounty ranked match archive (Firebase Firestore + Storage), used with OPBounty's permission.

The OPBounty desktop/mobile app uploads one document per finished ranked match to the Firestore
collection `Replays` in project opbounty-3623c: leaders, both bounties (same scale as the ladder
rating), both decklists as "NxCODE" text, timestamp, ISO week, and the Storage path of the combat
log.  The app reads that collection with a shared client sign-in that ships inside the app; we use
the same sign-in (credentials via env / CI secrets) and only ever read.

What we keep (data/matches/YYYY-MM-DD.json.gz, one file per UTC game day, never deleted):
  {"date", "matches":[{"id","ts","mode","w":{"l":leader,"b":bounty,"d":deckhash|null},
                       "l":{...}, "log":storagePath}], "decks":{hash:"4xOP01-016 3xOP01-024 ..."}}
Handles for high-bounty games are pulled from the first bytes of the combat log (Range request) into
data/matches/handles/YYYY-MM-DD.json  {matchId: [handle1, handle2]}  so ladder players can be linked
by handle + bounty.

Env: OPB_FS_EMAIL, OPB_FS_PASSWORD (required), OPB_FS_KEY (defaults to the app's public web key),
     OPB_MATCH_DAYS (backfill window, default 3), OPB_HANDLE_MIN_BOUNTY (default 1900).
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import normalize as N
from ..http import HttpError, request

PROJECT = "opbounty-3623c"
BUCKET = "opbounty-3623c.firebasestorage.app"
API_KEY = os.environ.get("OPB_FS_KEY", "AIzaSyC9qxZxJZbt2NJkjSyU9b3KJUfRHVFPuVs")  # public web key baked into the app
EMAIL = os.environ.get("OPB_FS_EMAIL")
PASSWORD = os.environ.get("OPB_FS_PASSWORD")
FS = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"
PAGE = 300
DAYS = int(os.environ.get("OPB_MATCH_DAYS", "3"))
HANDLE_MIN_BOUNTY = float(os.environ.get("OPB_HANDLE_MIN_BOUNTY", "1000"))  # ~rank 3,500; #1000 sits ~2100, #10000 ~230
LOG_HEAD_BYTES = 4000
WORKERS = int(os.environ.get("OPB_HANDLE_WORKERS", "8"))
REFRESH = os.environ.get("OPB_MATCH_REFRESH") == "1"  # re-pull finished days (schema change)

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "data" / "matches"
HANDLES = OUT / "handles"
ZW = "​"
PLY_RE = re.compile(r"^RZ1\|PLY\|([12])\|(.+?)\|([A-Z0-9]+-\d+)\s*$", re.M)
CONNECT_RE = re.compile(r"^(.+?) Has Connected\s*$", re.M)


class Client:
    def __init__(self):
        if not (EMAIL and PASSWORD):
            raise RuntimeError("OPB_FS_EMAIL / OPB_FS_PASSWORD not set")
        self.token = None
        self.expiry = 0.0

    def _auth(self):
        if self.token and time.time() < self.expiry - 120:
            return
        body = json.dumps({"email": EMAIL, "password": PASSWORD, "returnSecureToken": True}).encode()
        _, _, raw = request(f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={API_KEY}",
                            method="POST", headers={"Content-Type": "application/json"}, body=body)
        data = json.loads(raw)
        self.token = data["idToken"]
        self.expiry = time.time() + int(data.get("expiresIn", "3600"))

    def post(self, path: str, obj) -> list | dict:
        self._auth()
        body = json.dumps(obj).encode()
        _, _, raw = request(f"{FS}{path}", method="POST", body=body,
                            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        return json.loads(raw)

    def log_head(self, storage_path: str, nbytes: int = LOG_HEAD_BYTES) -> str:
        self._auth()
        url = f"https://firebasestorage.googleapis.com/v0/b/{BUCKET}/o/{urllib.parse.quote(storage_path, safe='')}?alt=media"
        try:
            _, _, raw = request(url, headers={"Authorization": f"Bearer {self.token}", "Range": f"bytes=0-{nbytes - 1}",
                                              "Accept": "*/*", "Accept-Encoding": "identity"}, retries=2)
        except HttpError as e:
            if e.status == 404:
                return ""
            raise
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- firestore helpers
def _v(fields: dict, name: str, default=None):
    f = fields.get(name)
    if not f:
        return default
    for k in ("stringValue", "doubleValue", "integerValue", "booleanValue"):
        if k in f:
            return float(f[k]) if k == "doubleValue" else (int(f[k]) if k == "integerValue" else f[k])
    return default


def day_query(day: str, cursor=None) -> dict:
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    q = {"structuredQuery": {
        "from": [{"collectionId": "Replays"}],
        "where": {"compositeFilter": {"op": "AND", "filters": [
            {"fieldFilter": {"field": {"fieldPath": "game_mode"}, "op": "EQUAL", "value": {"stringValue": "0"}}},
            {"fieldFilter": {"field": {"fieldPath": "timestamp"}, "op": "GREATER_THAN_OR_EQUAL", "value": {"stringValue": day}}},
            {"fieldFilter": {"field": {"fieldPath": "timestamp"}, "op": "LESS_THAN", "value": {"stringValue": nxt}}}]}},
        "orderBy": [{"field": {"fieldPath": "timestamp"}, "direction": "ASCENDING"},
                    {"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}],
        "limit": PAGE}}
    if cursor:
        q["structuredQuery"]["startAt"] = {"values": cursor, "before": False}
    return q


def fetch_day(client: Client, day: str) -> list[dict]:
    """Every ranked match document with a timestamp on `day` (UTC), raw Firestore fields."""
    docs, cursor = [], None
    while True:
        res = client.post(":runQuery", day_query(day, cursor))
        page = [r["document"] for r in res if isinstance(r, dict) and "document" in r]
        docs.extend(page)
        if len(page) < PAGE:
            break
        last = page[-1]
        cursor = [last["fields"]["timestamp"], {"referenceValue": last["name"]}]
    return docs


# ---------------------------------------------------------------- shaping
def compact_deck(text, leader, decks: dict) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        cards = N.parse_deck(text, leader=leader if isinstance(leader, str) else None)
    except Exception as e:  # noqa: BLE001 - one odd document must not sink the day
        print(f"[matches] unparseable deck ({e!r}): {text[:80]!r}", file=sys.stderr)
        return None
    if not cards:
        return None
    h = N.deck_hash(cards)
    if h not in decks:
        decks[h] = " ".join("%dx%s" % (c["qty"], c["id"]) for c in cards)
    return h


def shape_day(day: str, docs: list[dict]) -> dict:
    decks: dict[str, str] = {}
    matches = []
    for d in docs:
        f = d["fields"]
        wl, ll = _v(f, "winner_leader"), _v(f, "loser_leader")
        matches.append({
            "id": d["name"].rsplit("/", 1)[-1],
            "ts": _v(f, "timestamp"),                 # uploader's local clock: fine for ordering one client, not comparable
            "ct": (d.get("createTime") or "")[:19],   # Firestore server time (UTC): the reliable clock
            "w": {"l": wl, "b": round(_v(f, "winner_bounty", 0.0), 2), "d": compact_deck(_v(f, "winner_deck"), wl, decks)},
            "l": {"l": ll, "b": round(_v(f, "loser_bounty", 0.0), 2), "d": compact_deck(_v(f, "loser_deck"), ll, decks)},
            "log": _v(f, "path"),
        })
    matches.sort(key=lambda m: m["ts"] or "")
    return {"date": day, "matches": matches, "decks": decks}


LEADER_RE = re.compile(r"^\[(.+?)\] Leader is .*?<link=\"([A-Z0-9]+-\d+)\">", re.M)


def parse_handles(head: str) -> list[list[str | None]] | None:
    """[[handle, leader], [handle, leader]] in seat order from the first bytes of a combat log.
    The leader lets the linker tell which side (winner/loser) each handle played."""
    found = {m.group(1): [m.group(2).replace(ZW, ""), m.group(3)] for m in PLY_RE.finditer(head)}
    if len(found) == 2:
        return [found["1"], found["2"]]
    names = [m.group(1).replace(ZW, "") for m in CONNECT_RE.finditer(head)]
    leaders = {m.group(1).replace(ZW, ""): m.group(2) for m in LEADER_RE.finditer(head)}
    if len(names) >= 2:
        return [[n, leaders.get(n)] for n in names[:2]]
    return None


# ---------------------------------------------------------------- io
def day_path(day: str) -> Path:
    return OUT / f"{day}.json.gz"


def load_day(day: str) -> dict | None:
    p = day_path(day)
    return json.loads(gzip.decompress(p.read_bytes())) if p.exists() else None


def save_day(obj: dict):
    OUT.mkdir(parents=True, exist_ok=True)
    day_path(obj["date"]).write_bytes(gzip.compress(json.dumps(obj, separators=(",", ":")).encode(), mtime=0))


def fetch_handles(client: Client, day_obj: dict, min_bounty: float = HANDLE_MIN_BOUNTY) -> dict:
    """Handles for matches where either bounty clears the ladder threshold. Incremental."""
    HANDLES.mkdir(parents=True, exist_ok=True)
    p = HANDLES / f"{day_obj['date']}.json"
    have: dict = json.loads(p.read_text()) if p.exists() else {}
    def stale(v) -> bool:  # earlier format stored plain handle strings without seat leaders: refetch those
        return isinstance(v, list) and any(isinstance(x, str) for x in v)

    todo = [m for m in day_obj["matches"] if m["log"] and (m["id"] not in have or stale(have[m["id"]]))
            and max(m["w"]["b"], m["l"]["b"]) >= min_bounty]
    got = 0

    def one(m):
        try:
            head = client.log_head(m["log"])
        except HttpError:
            return m["id"], None, False
        hs = parse_handles(head) if head else None
        return m["id"], hs, True

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for mid, hs, ok in ex.map(one, todo):
            if ok:
                have[mid] = hs  # None = log missing/unparseable; recorded so we do not retry forever
            got += bool(hs)
    p.write_text(json.dumps(have, separators=(",", ":"), ensure_ascii=False))
    return {"wanted": len(todo), "got": got, "total": len(have)}


def fetch_all(days: int = DAYS, handles: bool = True) -> dict:
    """Pull the last `days` UTC days (today included, so today is re-pulled while it fills up)."""
    client = Client()
    today = datetime.now(timezone.utc).date()
    status = {}
    for back in range(days - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        existing = load_day(day)
        complete_past_day = existing is not None and day < today.isoformat() and existing.get("final") and not REFRESH
        if complete_past_day:
            status[day] = {"matches": len(existing["matches"]), "cached": True}
        else:
            docs = fetch_day(client, day)
            obj = shape_day(day, docs)
            if day < today.isoformat():
                obj["final"] = True
            save_day(obj)
            existing = obj
            status[day] = {"matches": len(docs), "decks": len(obj["decks"])}
        if handles:
            status[day]["handles"] = fetch_handles(client, existing)
    return status


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else DAYS
    print(json.dumps(fetch_all(n), indent=1))
