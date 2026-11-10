"""
Minimal read-only NetBox REST client (stdlib only).

What it must not do: return a partial result. v1's pager returned whatever it had when a page
failed, and a partial device roster is indistinguishable from devices that were removed. Here any
failed page raises, and the caller emits nothing for that endpoint this cycle (SD7).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

PAGE_LIMIT = 1000
MAX_PAGES = 500


class NetboxError(Exception):
    pass


class NetboxClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30, opener=None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Token {self.token}", "Accept": "application/json"})
        try:
            with self._open(req, timeout=self.timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 — any failure means "could not ask", see module doc
            raise NetboxError(f"GET {urllib.parse.urlsplit(url).path} failed: {e}") from e

    def _rebase(self, next_url: str) -> str:
        """NetBox builds `next` from its own idea of its hostname. Behind a reverse proxy or NAT
        that host is often unreachable from the ActiveGate (measured on a real NetBox 4.6 install, `next`
        pointed at an internal host:port). Keep the configured base, take only path+query."""
        parts = urllib.parse.urlsplit(next_url)
        base = urllib.parse.urlsplit(self.base)
        return urllib.parse.urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))

    def all(self, path: str, **params) -> list[dict]:
        params.setdefault("limit", PAGE_LIMIT)
        url = f"{self.base}/api{path}?{urllib.parse.urlencode(params)}"
        out = []
        for _ in range(MAX_PAGES):
            page = self._get(url)
            out.extend(page.get("results", []))
            nxt = page.get("next")
            if not nxt:
                return out
            url = self._rebase(nxt)
        raise NetboxError(f"{path}: more than {MAX_PAGES} pages; refusing to report a truncated set")
