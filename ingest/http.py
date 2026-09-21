"""Tiny HTTP layer: stdlib only, polite defaults, retry/backoff on 429 and 5xx."""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request

USER_AGENT = "optcg-data ingest (+https://github.com/alexfrey317/optcg-data; daily snapshot)"


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url


def request(url: str, *, method: str = "GET", headers: dict | None = None, body: bytes | None = None,
            retries: int = 5, min_interval: float = 0.0) -> tuple[int, dict, bytes]:
    """Return (status, headers, body). Retries on 429/5xx with exponential backoff.
    Returns status 304 with empty body when the server says Not Modified."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    delay = 2.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, method=method, headers=hdrs, data=body)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                if min_interval:
                    time.sleep(min_interval)
                return r.status, dict(r.headers), raw
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, dict(e.headers), b""
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = float(e.headers.get("Retry-After") or delay)
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            raise HttpError(e.code, url, e.read().decode(errors="replace")) from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise HttpError(0, url, str(e)) from None
    raise HttpError(0, url, "retries exhausted")


def get_json(url: str, **kw):
    status, _, raw = request(url, **kw)
    if status == 304:
        return None
    return json.loads(raw)


def post_json(url: str, payload=None, **kw):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    headers.update(kw.pop("headers", {}) or {})
    status, _, raw = request(url, method="POST", headers=headers, body=body, **kw)
    return json.loads(raw) if raw else {}
