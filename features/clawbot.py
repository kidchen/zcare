"""ClawBot accounts + receive loops: each person who scanned zcare's ClawBot QR chats with 小助理 in WeChat.

* Accounts live in ``data/clawbot_accounts.json`` (data volume, never in git): role (father / mother /
  admin), the bot token from the QR login, the scanner's WeChat user id. ``python main.py clawbot-login``
  adds one.
* One daemon thread per account long-polls ``getupdates`` and hands each user message to
  ``on_message(account, inbound, reply)``; ``reply(text)`` answers in the same chat (with the message's
  context_token). The cursor is saved, so a restart doesn't replay old messages.
* Only messages from the account's own scanner are accepted (anyone else is ignored and logged).
* Stale session (-14) → pause an hour; 3 failures in a row → back off 30 s; a long-dead session is
  reported once via ``on_problem`` so the admin can re-scan.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from clients.clawbot_client import BASE_URL, STALE_SESSION, ClawBotClient, ClawBotError, Inbound
from storage import StateStore

log = logging.getLogger(__name__)

ROLES = ("father", "mother", "admin")
STALE_PAUSE_S = 3600
BACKOFF_S = 30
RETRY_S = 2
MAX_FAILURES = 3


@dataclass(frozen=True)
class ClawAccount:
    role: str
    bot_token: str
    bot_id: str
    user_id: str  # the scanner's WeChat user id: the only sender we answer
    base_url: str = ""
    added_at: float = 0.0


class AccountStore:
    def __init__(self, path: Path):
        self._store = StateStore(path)

    def all(self) -> list[ClawAccount]:
        out = []
        for raw in self._store.get("accounts", []) or []:
            try:
                out.append(ClawAccount(**raw))
            except TypeError:
                log.warning("ignoring malformed ClawBot account entry")
        return out

    def add(self, account: ClawAccount) -> None:
        """One account per role (re-scanning replaces it)."""
        keep = [asdict(a) for a in self.all() if a.role != account.role]
        self._store.set("accounts", keep + [asdict(account)])

    def remove(self, role: str) -> bool:
        before = self.all()
        self._store.set("accounts", [asdict(a) for a in before if a.role != role])
        return len(before) != len(self.all())


Reply = Callable[[str], None]


class ClawBotRunner:
    def __init__(self, account: ClawAccount, on_message: Callable[[ClawAccount, Inbound, Reply], None],
                 cursor_file: Path, client: ClawBotClient | None = None,
                 on_problem: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.account = account
        self._client = client or ClawBotClient(account.bot_token, account.base_url or BASE_URL)
        self._on_message = on_message
        self._cursor_file = Path(cursor_file)
        self._on_problem = on_problem
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reported_stale = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name=f"clawbot-{self.account.role}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _cursor(self) -> str:
        try:
            return self._cursor_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _save_cursor(self, cursor: str) -> None:
        try:
            self._cursor_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cursor_file.with_suffix(".tmp")
            tmp.write_text(cursor, encoding="utf-8")
            tmp.replace(self._cursor_file)
        except OSError as exc:
            log.warning("clawbot cursor not saved: %s", exc)

    def reply_fn(self, inbound: Inbound) -> Reply:
        def reply(text: str) -> None:
            self._client.send_text(inbound.from_user, text, inbound.context_token)
        return reply

    # ------------------------------------------------------------ loop

    def poll_once(self, cursor: str, timeout_s: float) -> tuple[str, float]:
        msgs, cursor_next, timeout_next = self._client.get_updates(cursor, timeout_s)
        if cursor_next != cursor:
            self._save_cursor(cursor_next)
        for m in msgs:
            if m.from_user != self.account.user_id:
                log.warning("clawbot[%s]: ignoring message from an unknown sender", self.account.role)
                continue
            try:
                self._on_message(self.account, m, self.reply_fn(m))
            except Exception:  # noqa: BLE001 — one bad message must not stop the loop
                log.exception("clawbot[%s]: message handler failed", self.account.role)
        return cursor_next, timeout_next

    def run(self) -> None:
        cursor, timeout_s, failures = self._cursor(), 35.0, 0
        log.info("clawbot[%s]: listening", self.account.role)
        while not self._stop.is_set():
            try:
                cursor, timeout_s = self.poll_once(cursor, timeout_s)
                failures, self._reported_stale = 0, False
            except ClawBotError as exc:
                if exc.code == STALE_SESSION:
                    log.warning("clawbot[%s]: session stale (-14); pausing 1 hour", self.account.role)
                    if not self._reported_stale and self._on_problem is not None:
                        self._reported_stale = True
                        self._on_problem(f"ClawBot（{self.account.role}）会话过期，暂停 1 小时；如果一直这样，请重新扫码绑定")
                    self._sleep(STALE_PAUSE_S)
                    continue
                failures += 1
                log.warning("clawbot[%s]: %s (%d/%d)", self.account.role, exc, failures, MAX_FAILURES)
                if failures >= MAX_FAILURES:
                    failures = 0
                    self._sleep(BACKOFF_S)
                else:
                    self._sleep(RETRY_S)
            except Exception:  # noqa: BLE001 — keep listening no matter what
                log.exception("clawbot[%s]: unexpected error", self.account.role)
                self._sleep(BACKOFF_S)


def login(client: ClawBotClient, role: str, show_qr: Callable[[str], None], ask_code: Callable[[str], str],
          say: Callable[[str], None] = print, deadline_s: float = 480, clock=time.monotonic,
          sleep: Callable[[float], None] = time.sleep) -> ClawAccount:
    """Interactive QR login (the person scans with their own WeChat and confirms)."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    qrcode, link = client.get_login_qr()
    show_qr(link)
    end, verify, scanned = clock() + deadline_s, "", False
    base = BASE_URL
    while clock() < end:
        st = ClawBotClient(base_url=base, session=client._session).login_status(qrcode, verify)
        if st.status == "scaned" and not scanned:
            scanned = True
            say("已扫码，请在手机上确认…")
        if st.status == "need_verifycode":
            verify = ask_code("输入手机微信上显示的数字：" if not verify else "数字不对，请重新输入：")
        elif st.status == "scaned_but_redirect" and st.redirect_host:
            base = f"https://{st.redirect_host}"
        elif st.status == "confirmed":
            if not (st.bot_token and st.bot_id and st.user_id):
                raise ClawBotError("login confirmed but the server returned no token / ids")
            return ClawAccount(role, st.bot_token, st.bot_id, st.user_id, st.base_url or BASE_URL, time.time())
        elif st.status == "binded_redirect":
            raise ClawBotError("这个微信已经绑定过了（无需重复绑定）")
        elif st.status in ("expired", "verify_code_blocked"):
            raise ClawBotError(f"二维码{'已过期' if st.status == 'expired' else '验证失败次数太多'}，请重新运行")
        else:
            sleep(1)
    raise ClawBotError("超时未完成扫码")
