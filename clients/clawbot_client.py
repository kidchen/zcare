"""WeChat ClawBot (微信ClawBot / iLink bot API) — chat with 小助理 inside normal WeChat.

Protocol mirrored from Tencent's official MIT plugin @tencent-weixin/openclaw-weixin 2.4.10
(src/api/api.ts, src/auth/login-qr.ts, docs/protocol.md). JSON over HTTPS, outbound only:

  login   POST ilink/bot/get_bot_qrcode?bot_type=3        → {qrcode, qrcode_img_content (link to show as QR)}
          GET  ilink/bot/get_qrcode_status?qrcode=…       → {status: wait|scaned|need_verifycode|confirmed|
                                                             expired|scaned_but_redirect|binded_redirect…,
                                                             bot_token, ilink_bot_id, ilink_user_id, baseurl}
  receive POST ilink/bot/getupdates {get_updates_buf}     long-poll (≤35 s) → {msgs, get_updates_buf, ret, errcode}
  reply   POST ilink/bot/sendmessage {msg:{to_user_id, client_id, message_type:2, message_state:2,
                                           item_list:[{type:1, text_item:{text}}], context_token}}

Rules we follow (the server contract isn't fully documented):
  * reply with the inbound message's ``context_token``;
  * errcode/ret -14 = stale session → pause the account for an hour (the official plugin does the same);
  * proactive sends need a recent user message (PushPlus docs: within 24 h, and after every 10 bot
    messages) — so zcare only REPLIES here; scheduled messages stay on PushPlus.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

import requests

from clients.http import build_session

log = logging.getLogger(__name__)

BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
APP_ID = "bot"  # iLink-App-Id declared by the official plugin
PROTOCOL_VERSION = "2.4.10"  # plugin version whose protocol we mirror
BOT_AGENT = "zcare/0.1"  # base_info.bot_agent: honest self-identification (observability only)
LONG_POLL_S = 35.0
API_TIMEOUT_S = 15.0
STALE_SESSION = -14
TEXT, VOICE = 1, 3
USER_MESSAGE, BOT_MESSAGE = 1, 2
FINISHED = 2
MAX_TEXT_CHARS = 2000


class ClawBotError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _client_version(version: str) -> int:
    major, minor, patch = (int(p) for p in (version.split(".") + ["0", "0"])[:3])
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


@dataclass(frozen=True)
class Inbound:
    message_id: str
    from_user: str
    text: str  # text message, or the voice message's speech-to-text
    context_token: str


@dataclass(frozen=True)
class LoginStatus:
    status: str
    bot_token: str = ""
    bot_id: str = ""
    user_id: str = ""  # the WeChat user who scanned
    base_url: str = ""
    redirect_host: str = ""


def parse_inbound(msgs: list[dict[str, Any]]) -> list[Inbound]:
    out = []
    for m in msgs or []:
        if not isinstance(m, dict) or m.get("message_type") != USER_MESSAGE:
            continue
        parts = []
        for item in m.get("item_list") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == TEXT:
                parts.append(str((item.get("text_item") or {}).get("text") or ""))
            elif item.get("type") == VOICE:
                parts.append(str((item.get("voice_item") or {}).get("text") or ""))
        text = "".join(parts).strip()
        if text and m.get("from_user_id"):
            out.append(Inbound(str(m.get("message_id") or ""), str(m["from_user_id"]), text,
                               str(m.get("context_token") or "")))
    return out


class ClawBotClient:
    def __init__(self, token: str = "", base_url: str = BASE_URL, session: requests.Session | None = None):
        self._token = token
        self._base = (base_url or BASE_URL).rstrip("/")
        self._session = session or build_session(retries=0)

    # ------------------------------------------------------------ transport

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "iLink-App-Id": APP_ID,
             "iLink-App-ClientVersion": str(_client_version(PROTOCOL_VERSION)),
             "AuthorizationType": "ilink_bot_token",
             "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode()}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _post(self, endpoint: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        payload = {**body, "base_info": {"channel_version": PROTOCOL_VERSION, "bot_agent": BOT_AGENT}}
        try:
            resp = self._session.post(f"{self._base}/{endpoint}", data=json.dumps(payload, ensure_ascii=False).encode(),
                                      headers=self._headers(), timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise ClawBotError(f"{endpoint}: {type(exc).__name__}") from None  # never echo the token
        return data if isinstance(data, dict) else {}

    def _get(self, endpoint: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
        try:
            resp = self._session.get(f"{self._base}/{endpoint}", params=params, headers=self._headers(), timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise ClawBotError(f"{endpoint}: {type(exc).__name__}") from None
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ login

    def get_login_qr(self) -> tuple[str, str]:
        """→ (qrcode id, link to render as a QR code for WeChat to scan)."""
        data = self._post(f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}", {"local_token_list": []}, API_TIMEOUT_S)
        if not data.get("qrcode") or not data.get("qrcode_img_content"):
            raise ClawBotError("no QR code in response")
        return str(data["qrcode"]), str(data["qrcode_img_content"])

    def login_status(self, qrcode: str, verify_code: str = "") -> LoginStatus:
        params = {"qrcode": qrcode, **({"verify_code": verify_code} if verify_code else {})}
        try:
            d = self._get("ilink/bot/get_qrcode_status", params, LONG_POLL_S + 5)
        except ClawBotError:
            return LoginStatus("wait")  # gateway timeouts are normal during the long poll
        return LoginStatus(str(d.get("status") or "wait"), str(d.get("bot_token") or ""), str(d.get("ilink_bot_id") or ""),
                           str(d.get("ilink_user_id") or ""), str(d.get("baseurl") or ""), str(d.get("redirect_host") or ""))

    # ------------------------------------------------------------ messages

    def get_updates(self, cursor: str, timeout_s: float = LONG_POLL_S) -> tuple[list[Inbound], str, float]:
        """Long-poll → (new user messages, next cursor, server-suggested next timeout in s)."""
        try:
            d = self._post("ilink/bot/getupdates", {"get_updates_buf": cursor or ""}, timeout_s + 5)
        except ClawBotError as exc:
            if "Timeout" in str(exc):
                return [], cursor, timeout_s  # normal end of a long poll
            raise
        code = d.get("errcode") or d.get("ret") or 0
        if code:
            raise ClawBotError(f"getupdates failed: {code} {d.get('errmsg') or ''}".strip(), int(code))
        nxt = d.get("longpolling_timeout_ms")
        return (parse_inbound(d.get("msgs") or []), str(d.get("get_updates_buf") or cursor),
                float(nxt) / 1000 if isinstance(nxt, (int, float)) and nxt > 0 else timeout_s)

    def send_text(self, to_user: str, text: str, context_token: str = "") -> None:
        msg = {"from_user_id": "", "to_user_id": to_user, "client_id": f"zcare:{int(time.time() * 1000)}-{secrets.token_hex(4)}",
               "message_type": BOT_MESSAGE, "message_state": FINISHED,
               "item_list": [{"type": TEXT, "text_item": {"text": text[:MAX_TEXT_CHARS]}}]}
        if context_token:
            msg["context_token"] = context_token
        d = self._post("ilink/bot/sendmessage", {"msg": msg}, API_TIMEOUT_S)
        if d.get("ret") or d.get("errcode"):
            code = d.get("errcode") or d.get("ret")
            raise ClawBotError(f"sendmessage failed: {code} {d.get('errmsg') or ''}".strip(), int(code))
