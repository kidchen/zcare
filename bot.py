"""Keyword command bot for messages parents send to the WeCom app.

``ChatBot.handle`` is fast and side-effect free (besides in-memory bookkeeping):
it decides the instant passive reply and which slow job, if any, to run in the
background. Slow jobs then push their result via the normal send API, because
WeCom only waits ~5 s for the HTTP response and retries otherwise.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

from clients.wecom_callback import IncomingMessage

log = logging.getLogger(__name__)


class Command(str, Enum):
    COMMUTE = "commute"
    OUTING = "outing"
    TV = "tv"
    WEATHER = "weather"
    NEWS = "news"
    HELP = "help"


# First match wins, so more specific intents come first.
KEYWORDS: tuple[tuple[Command, tuple[str, ...]], ...] = (
    (Command.COMMUTE, ("路况", "堵车", "堵不堵", "开车")),
    (Command.WEATHER, ("天气", "气温", "温度", "下雨", "下雪", "冷不冷", "热不热", "降温", "刮风", "预报")),
    (Command.NEWS, ("新闻", "新鲜事", "本地消息", "有啥事", "发生了什么")),
    (Command.TV, ("剧", "电视")),
    (Command.OUTING, ("出门", "去哪", "逛", "公交", "散步", "什么活动", "有活动", "展览", "好玩", "景点",
                      "演出", "二人台", "音乐会", "博物馆", "文化馆")),
)

HELP_TEXT = (
    "您好！给我发这些字就行：\n"
    "· 「路况」— 现在开车去单位堵不堵\n"
    "· 「天气」— 这几天的天气和穿衣提醒\n"
    "· 「新闻」— 今天呼和浩特的新鲜事\n"
    "· 「出门」— 今天去哪儿逛逛、怎么坐公交\n"
    "· 「电视剧」— 最近好看的剧"
)

ACK_TEXT = {
    Command.COMMUTE: "好的，正在查路况，稍等几秒～",
    Command.WEATHER: "好的，我看看这几天的天气～",
    Command.NEWS: "好的，我给您挑几条今天的本地新闻～",
    Command.OUTING: "好的，正在帮您找地方和公交路线，稍等～",
    Command.TV: "好的，正在找好看的剧，大概要十几秒～",
}

COOLDOWN_TEXT = "刚刚已经查过啦，过一分钟再试试～"
ADMIN_QR_KEYWORDS = ("扫码", "二维码", "重新授权", "重新登录")
ADMIN_QR_ACK = "好的，正在生成公众号后台登录二维码，几秒后发给你，请尽快扫码（二维码几分钟内有效）。"
ADMIN_QR_COOLDOWN_TEXT = "二维码刚发过，请先扫描上一张；1 分钟后可以再要一张。"
RATE_LIMIT_TEXT = "今天聊得真开心～我先歇一会儿，过一阵子再陪您聊。有急事请直接给孩子打电话哦。"


def parse_command(text: str) -> Command:
    normalized = text.strip().lower()
    for command, keywords in KEYWORDS:
        if any(k in normalized for k in keywords):
            return command
    return Command.HELP


@dataclass(frozen=True)
class Reaction:
    reply: str | None  # immediate passive reply text (None = empty 200 response)
    command: Command | None = None  # slow job to run in background for msg.from_user
    chat: bool = False  # hand the text to the LLM assistant in the background
    admin_command: str | None = None  # e.g. "wemp_qr" (admin only)


class _TtlSet:
    """Bounded, time-limited set for de-duplicating WeCom retries by MsgId."""

    def __init__(self, ttl_s: float, max_items: int, clock: Callable[[], float]):
        self._ttl, self._max, self._clock = ttl_s, max_items, clock
        self._items: OrderedDict[str, float] = OrderedDict()

    def add_if_new(self, key: str) -> bool:
        now = self._clock()
        while self._items and next(iter(self._items.values())) < now - self._ttl:
            self._items.popitem(last=False)
        if key in self._items:
            return False
        self._items[key] = now
        if len(self._items) > self._max:
            self._items.popitem(last=False)
        return True


class ChatBot:
    def __init__(
        self,
        allowed_users: Iterable[str],
        cooldown_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        llm_enabled: bool = False,
        llm_max_per_hour: int = 30,
        admin_user: str = "",
    ):
        self._allowed = {u for u in allowed_users if u}
        self._admin = admin_user
        self._last_qr = float("-inf")
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._llm_enabled = llm_enabled
        self._llm_max_per_hour = llm_max_per_hour
        self._llm_calls: dict[str, deque[float]] = {}
        self._seen = _TtlSet(ttl_s=600, max_items=1000, clock=clock)
        self._last_run: dict[tuple[str, Command], float] = {}
        self._lock = threading.Lock()

    def handle(self, msg: IncomingMessage) -> Reaction:
        if msg.msg_type != "text":
            return Reaction(None)  # events (enter_agent etc.), images, voice: ignore
        if msg.from_user not in self._allowed:
            log.warning("ignoring message from non-family user %r", msg.from_user)
            return Reaction(None)
        with self._lock:
            if msg.msg_id and not self._seen.add_if_new(msg.msg_id):
                log.info("duplicate delivery of MsgId %s ignored", msg.msg_id)
                return Reaction(None)
            if self._admin and msg.from_user == self._admin and any(k in msg.content for k in ADMIN_QR_KEYWORDS):
                now = self._clock()
                if now - self._last_qr < self._cooldown_s:
                    return Reaction(ADMIN_QR_COOLDOWN_TEXT)
                self._last_qr = now
                return Reaction(ADMIN_QR_ACK, admin_command="wemp_qr")
            if self._llm_enabled:
                if not self._llm_budget_ok_locked(msg.from_user):
                    return Reaction(RATE_LIMIT_TEXT)
                return Reaction(None, chat=True)  # LLM replies via the send API
            command = parse_command(msg.content)
            if command is Command.HELP:
                return Reaction(HELP_TEXT)
            if not self._start_locked(msg.from_user, command):
                return Reaction(COOLDOWN_TEXT)
        log.info("command %s from %s", command.value, msg.from_user)
        return Reaction(ACK_TEXT[command], command)

    def try_start(self, user: str, command: Command) -> bool:
        """Claim the per-user cooldown for a job; False if it ran too recently."""
        with self._lock:
            return self._start_locked(user, command)

    def _start_locked(self, user: str, command: Command) -> bool:
        now = self._clock()
        last = self._last_run.get((user, command))
        if last is not None and now - last < self._cooldown_s:
            return False
        self._last_run[(user, command)] = now
        return True

    def _llm_budget_ok_locked(self, user: str) -> bool:
        calls = self._llm_calls.setdefault(user, deque())
        now = self._clock()
        while calls and calls[0] < now - 3600:
            calls.popleft()
        if len(calls) >= self._llm_max_per_hour:
            log.warning("LLM hourly budget reached for %s", user)
            return False
        calls.append(now)
        return True
