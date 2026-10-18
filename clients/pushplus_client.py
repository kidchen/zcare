"""PushPlus (pushplus.plus) — delivers to normal WeChat through its 服务号 "pushplus 推送加".

POST https://www.pushplus.plus/send  {token, title, content, template: "html", channel: "wechat", to?}
  * ``to`` = comma-separated 好友令牌 (friends who scanned your PushPlus QR); omitted = the token owner.
  * Free, real-name-verified account: 200 requests/day, 5/minute; identical content max 3×/hour.
  * ``code == 200`` only means PushPlus accepted the request — not that WeChat delivered it.
  * The notification shows ~12 characters of the title (emoji dropped); the full HTML opens on tap.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

import requests

from clients.http import build_session

log = logging.getLogger(__name__)
SEND_URL = "https://www.pushplus.plus/send"  # https: the token must not travel in clear text
TITLE_MAX_CHARS = 100
CONTENT_MAX_CHARS = 20_000
MAX_FRIENDS_PER_REQUEST = 10


class PushPlusError(RuntimeError):
    pass


class PushPlusClient:
    def __init__(self, token: str, session: requests.Session | None = None, timeout: float = 20.0):
        if not token:
            raise ValueError("PushPlus token is empty")
        self._token = token
        self._session = session or build_session(retries=1)
        self._timeout = timeout

    def send(self, title: str, html: str, to: Iterable[str] = ()) -> str:
        """Send one HTML message → PushPlus's message id. ``to`` empty → the token owner."""
        friends = [t for t in dict.fromkeys(to) if t]
        if len(friends) > MAX_FRIENDS_PER_REQUEST:
            raise PushPlusError(f"at most {MAX_FRIENDS_PER_REQUEST} friends per request, got {len(friends)}")
        if len(html) > CONTENT_MAX_CHARS:
            raise PushPlusError(f"content too long ({len(html)} > {CONTENT_MAX_CHARS} characters)")
        payload = {"token": self._token, "title": title[:TITLE_MAX_CHARS], "content": html,
                   "template": "html", "channel": "wechat"}
        if friends:
            payload["to"] = ",".join(friends)
        try:
            resp = self._session.post(SEND_URL, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            # never include the request (it carries the token) in the error
            raise PushPlusError(f"request failed: {type(exc).__name__}") from None
        if not isinstance(data, dict) or data.get("code") != 200:
            code = data.get("code") if isinstance(data, dict) else "?"
            msg = data.get("msg") if isinstance(data, dict) else ""
            raise PushPlusError(f"rejected: code {code} {msg}".strip())
        return str(data.get("data") or "")
