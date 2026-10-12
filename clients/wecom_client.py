"""WeCom (企业微信) self-built app messaging.

Docs: https://developer.work.weixin.qq.com/document/path/90236

IMPORTANT rendering caveat: ``markdown`` messages only render inside the WeCom
app. Members who receive app messages in regular WeChat (微信插件 / 互通) see
"暂不支持此消息类型". If your parents read messages in WeChat, set
WECOM_MESSAGE_TYPE=text (or textcard).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable

import requests

from clients.http import build_session
from messages import Article, Message, strip_html

log = logging.getLogger(__name__)

BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"
TOKEN_INVALID_CODES = {40001, 40014, 42001}  # invalid / expired access_token
TOKEN_SAFETY_MARGIN_S = 300

# Byte limits enforced by WeCom (UTF-8; one CJK char = 3 bytes).
TEXT_MAX_BYTES = 2048
MARKDOWN_MAX_BYTES = 2048
TEXTCARD_TITLE_MAX_BYTES = 128
TEXTCARD_DESC_MAX_BYTES = 512
NEWS_MAX_ARTICLES = 8
NEWS_TITLE_MAX_BYTES = 128
NEWS_DESC_MAX_BYTES = 512


class WeComError(RuntimeError):
    def __init__(self, errcode: int, errmsg: str):
        hint = ""
        if errcode == 60020:
            hint = " (add this server's public IP to the app's 企业可信IP list)"
        super().__init__(f"WeCom error {errcode}: {errmsg}{hint}")
        self.errcode = errcode
        self.errmsg = errmsg


def truncate_utf8(text: str, max_bytes: int, suffix: str = "…") -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    budget = max_bytes - len(suffix.encode("utf-8"))
    return raw[:budget].decode("utf-8", errors="ignore") + suffix


class WeComClient:
    def __init__(
        self,
        corp_id: str,
        agent_id: int,
        secret: str,
        session: requests.Session | None = None,
        timeout: float = 8.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not (corp_id and agent_id and secret):
            raise ValueError("corp_id, agent_id and secret are required")
        self._corp_id = corp_id
        self._agent_id = int(agent_id)
        self._secret = secret
        self._session = session or build_session()
        self._timeout = timeout
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    # -- auth ----------------------------------------------------------------

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Cached access_token (valid 7200s; refreshed 5 min early)."""
        with self._lock:
            if not force_refresh and self._token and self._clock() < self._expires_at:
                return self._token
            resp = self._session.get(
                f"{BASE_URL}/gettoken",
                params={"corpid": self._corp_id, "corpsecret": self._secret},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode", 0) != 0:
                raise WeComError(data.get("errcode", -1), data.get("errmsg", ""))
            self._token = data["access_token"]
            self._expires_at = self._clock() + int(data.get("expires_in", 7200)) - TOKEN_SAFETY_MARGIN_S
            return self._token

    # -- sending -------------------------------------------------------------

    def _send(self, to_users: Iterable[str], msgtype: str, body: dict[str, Any]) -> dict[str, Any]:
        users = [u for u in to_users if u]
        if not users:
            raise ValueError("no recipients")
        payload = {
            "touser": "|".join(users),
            "msgtype": msgtype,
            "agentid": self._agent_id,
            msgtype: body,
            # Server-side dedupe: guards against double sends from retries/restarts.
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 600,
        }
        for attempt in range(2):
            token = self.get_access_token(force_refresh=attempt > 0)
            resp = self._session.post(
                f"{BASE_URL}/message/send",
                params={"access_token": token},
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            code = data.get("errcode", 0)
            if code == 0:
                if data.get("invaliduser"):
                    log.warning("WeCom invalid users: %s", data["invaliduser"])
                return data
            if code in TOKEN_INVALID_CODES and attempt == 0:
                log.info("access_token rejected (%s); refreshing", code)
                continue
            raise WeComError(code, data.get("errmsg", ""))
        raise AssertionError("unreachable")

    def upload_image(self, data: bytes, filename: str = "image.png") -> str:
        """Upload a temporary image (valid 3 days, ≤10 MB, JPG/PNG) → media_id.
        Docs: POST /cgi-bin/media/upload?type=image, multipart field "media"."""
        mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
        for attempt in range(2):
            token = self.get_access_token(force_refresh=attempt > 0)
            resp = self._session.post(
                f"{BASE_URL}/media/upload", params={"access_token": token, "type": "image"},
                files={"media": (filename, data, mime)}, timeout=self._timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            code = body.get("errcode", 0)
            if code == 0 and body.get("media_id"):
                return body["media_id"]
            if code in TOKEN_INVALID_CODES and attempt == 0:
                continue
            raise WeComError(code, body.get("errmsg", "upload failed"))
        raise AssertionError("unreachable")

    def send_image(self, to_users: Iterable[str], media_id: str) -> dict[str, Any]:
        return self._send(to_users, "image", {"media_id": media_id})

    def send_text(self, to_users: Iterable[str], content: str) -> dict[str, Any]:
        return self._send(to_users, "text", {"content": truncate_utf8(content, TEXT_MAX_BYTES)})

    def send_markdown(self, to_users: Iterable[str], content: str) -> dict[str, Any]:
        return self._send(
            to_users, "markdown", {"content": truncate_utf8(content, MARKDOWN_MAX_BYTES)}
        )

    def send_textcard(
        self, to_users: Iterable[str], title: str, description: str, url: str, btntxt: str = "详情"
    ) -> dict[str, Any]:
        return self._send(
            to_users,
            "textcard",
            {
                "title": truncate_utf8(title, TEXTCARD_TITLE_MAX_BYTES),
                "description": truncate_utf8(description, TEXTCARD_DESC_MAX_BYTES),
                "url": url,
                "btntxt": btntxt,
            },
        )

    def send_news(self, to_users: Iterable[str], articles: Iterable[Article]) -> dict[str, Any]:
        """Image-text card: each article is its own tappable item with its own link and picture."""
        items = []
        for a in list(articles)[:NEWS_MAX_ARTICLES]:
            item = {
                "title": truncate_utf8(strip_html(a.title), NEWS_TITLE_MAX_BYTES),
                "description": truncate_utf8(strip_html(a.description), NEWS_DESC_MAX_BYTES),
                "url": a.url,
            }
            if a.picurl:
                item["picurl"] = a.picurl
            items.append(item)
        if not items:
            raise ValueError("news message needs at least one article")
        return self._send(to_users, "news", {"articles": items})

    def send_message(self, to_users: Iterable[str], message: Message, msg_type: str) -> dict[str, Any]:
        """Send a feature Message in the configured format.

        - markdown: WeCom-app-only markdown.
        - text:     plain text (works everywhere).
        - news:     one card article per item (dramas, a place, a route); plain text otherwise.
        - textcard: single-item cards as textcard; multi-item results are sent as news, because a
                    textcard has one link and a 512-byte description.
        No HTML is ever sent: descriptions are plain text.
        """
        to_users = list(to_users)
        if msg_type == "markdown":
            return self.send_markdown(to_users, message.markdown)
        if msg_type in ("news", "textcard"):
            if len(message.articles) > 1 or (msg_type == "news" and message.articles):
                return self.send_news(to_users, message.articles)
            article = message.articles[0] if message.articles else None
            url = article.url if article else message.url
            if url:
                title = article.title if article else message.title
                desc = article.description if article else message.text
                if msg_type == "news":
                    return self.send_news(to_users, [Article(title, desc, url)])
                return self.send_textcard(to_users, strip_html(title), strip_html(desc), url)
        return self.send_text(to_users, strip_html(message.text))
