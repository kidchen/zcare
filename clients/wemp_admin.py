"""we-mp-rss admin API: start a fresh WeChat Official Account login and fetch its QR code.

Endpoints (we-mp-rss core/base.py API_BASE = "/api/v1/wx"; apis/auth.py):
    POST /api/v1/wx/auth/token     form username/password → {"access_token": …}
    GET  /api/v1/wx/auth/qr/code   (Bearer) start a login session → QR written to static/wx_qrcode.png
    GET  /api/v1/wx/auth/qr/image  (Bearer) {"code": 0, "data": true} once the QR file exists
    GET  /api/v1/wx/auth/qr/status (Bearer) login status
    GET  /static/wx_qrcode.png     the QR image (public static file)
    DELETE /api/v1/wx/articles/clean-old?days=N   (Bearer) delete articles published > N days ago
        (hard delete only with ARTICLE.TRUE_DELETE=True on the server — set in docker-compose)
we-mp-rss polls WeChat every 2 s after a session starts until the QR is scanned or expires.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

import requests

from clients.http import build_session

log = logging.getLogger(__name__)
API = "/api/v1/wx"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


class WeMpAdminError(RuntimeError):
    pass


def is_image(data: bytes) -> bool:
    return data.startswith(PNG_MAGIC) or data.startswith(JPEG_MAGIC)


class WeMpAdminClient:
    def __init__(self, base_url: str, username: str, password: str, session: requests.Session | None = None,
                 timeout: float = 15.0, sleep: Callable[[float], None] = time.sleep):
        if not (base_url and username and password):
            raise ValueError("base_url, username and password are required")
        self._base = base_url.rstrip("/")
        self._user, self._password = username, password
        self._session = session or build_session(retries=1)
        self._timeout = timeout
        self._sleep = sleep
        self._token: str | None = None

    def _login(self) -> str:
        resp = self._session.post(f"{self._base}{API}/auth/token",
                                  data={"username": self._user, "password": self._password}, timeout=self._timeout)
        resp.raise_for_status()  # note: wrong credentials come back as HTTP 202 without a token
        token = (resp.json() or {}).get("access_token")
        if not token:
            raise WeMpAdminError("we-mp-rss login failed — check WEMP_ADMIN_PASSWORD")
        self._token = token
        return token

    def _get(self, path: str) -> dict:
        return self._call("GET", path)

    def _call(self, method: str, path: str, params: dict | None = None) -> dict:
        for attempt in range(2):
            token = self._token or self._login()
            resp = self._session.request(method, f"{self._base}{API}{path}", params=params,
                                         headers={"Authorization": f"Bearer {token}"}, timeout=self._timeout)
            if resp.status_code == 401 and attempt == 0:
                self._token = None  # expired JWT → log in again once
                continue
            resp.raise_for_status()
            return resp.json() or {}
        raise WeMpAdminError(f"unauthorized calling {path}")

    def request_qr(self, wait_s: float = 25.0, poll_s: float = 1.0) -> bytes:
        """Start a new WeChat login session and return its QR image bytes."""
        started = self._get("/auth/qr/code")
        log.info("we-mp-rss QR session requested: %s", str(started)[:120])
        waited = 0.0
        while waited <= wait_s:
            if (self._get("/auth/qr/image") or {}).get("data") is True:
                break
            self._sleep(poll_s)
            waited += poll_s
        else:
            raise WeMpAdminError("we-mp-rss did not produce a QR code in time")
        resp = self._session.get(f"{self._base}/static/wx_qrcode.png", params={"t": int(time.time())},
                                 timeout=self._timeout)
        resp.raise_for_status()
        if not is_image(resp.content):
            raise WeMpAdminError("QR endpoint did not return an image")
        return resp.content

    def clean_old(self, days: int, dry_run: bool = False) -> dict:
        """Delete articles published more than ``days`` days ago. Returns the server's data dict
        (contains the deleted / matched count)."""
        data = self._call("DELETE", "/articles/clean-old", params={"days": int(days), "dry_run": dry_run})
        if data.get("code", 0) != 0:
            raise WeMpAdminError(f"clean-old failed: {data.get('message')}")
        return data.get("data") or {}

    def status(self) -> object:
        return self._get("/auth/qr/status").get("data")
