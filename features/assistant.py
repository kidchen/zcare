"""LLM family assistant: classify a parent's message and write a warm reply.

The LLM only decides *intent* and writes conversational text. Real data (traffic,
routes, dramas) is always produced by the existing feature modules — the prompt
forbids the model from inventing it.

Safety net: a keyword check for health emergencies / scams runs on every message
independently of the LLM, so an alert to you can't be lost to a misclassification
or an LLM provider outage.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import requests

from bot import ACK_TEXT, HELP_TEXT, Command, parse_command
from clients.llm_client import LLMClient, LLMError

log = logging.getLogger(__name__)

INTENTS = ("traffic", "weather", "news", "outing", "tv", "chat", "urgent", "feedback")
INTENT_TO_COMMAND = {"traffic": Command.COMMUTE, "weather": Command.WEATHER, "news": Command.NEWS,
                     "outing": Command.OUTING, "tv": Command.TV}
COMMAND_TO_INTENT = {v: k for k, v in INTENT_TO_COMMAND.items()}

HEALTH_URGENT_KEYWORDS = (
    "胸痛", "胸闷", "心口疼", "心慌", "心绞痛", "喘不上气", "呼吸困难", "晕倒", "昏倒", "摔倒", "摔了",
    "救命", "120", "急救", "中风", "半边身子", "说不出话",
)
SCAM_KEYWORDS = ("转账", "汇款", "验证码", "安全账户", "公安局", "法院传票", "冻结")
URGENT_KEYWORDS = HEALTH_URGENT_KEYWORDS + SCAM_KEYWORDS
CALL_120_LEAD = "{name}，请马上拨打 120！"
SCAM_REPLY = "{name}，先别转账，也别把验证码告诉任何人！这很可能是骗子。先给孩子打个电话问清楚，我已经通知孩子了。"
URGENT_FALLBACK_REPLY = (
    "{name}，如果身体很不舒服，请马上拨打 120！如果是有人让您转账或要验证码，先别操作，"
    "先给孩子打个电话确认。我已经通知孩子了。"
)

MAX_REPLY_CHARS = 300
HISTORY_TURNS = 4  # user+assistant pairs kept per person
HISTORY_IDLE_RESET_S = 2 * 3600
WEEKDAYS = "一二三四五六日"

# STATIC by design: byte-identical on every request so BigModel's implicit prefix cache
# (usage.prompt_tokens_details.cached_tokens) can hit. Never .format() this string — everything
# that varies (time, who is talking, assistant name) goes into the user message via
# build_user_content().
SYSTEM_PROMPT = """<role>
你是孩子为住在呼和浩特的爸爸妈妈设置的微信家庭小助手，替在外地工作的孩子陪伴、关心父母。每条用户消息开头的 [Context] 给出：Time=北京时间，User=对方称呼，Location=城市，Assistant=你的名字；可能还有 Day=工作日/周末/节假日，Recent=最近推荐过的活动或地点（场馆·类别），Prefs=已经记下的喜好，Notes=今天的本地提醒。
</role>

<intents>
traffic：爸爸上班开车的实时路况（只问天气时不要用 traffic）
weather：天气、气温、冷热、下雨下雪、刮风，今天、明天或这几天的天气
news：想听听本地新闻、最近呼和浩特有什么新鲜事
outing：呼和浩特本地近期活动和去处——剧场演出（乌兰恰特大剧院等的戏曲、二人台、民歌、民族音乐会、民族舞）、博物馆和美术馆展览、文化馆公益活动、公园广场民俗活动，以及少换乘少步行的公交路线
tv：好看的电视剧——正在热播的高分新剧（现代、悬疑、家庭等）和口碑经典（历史正剧、年代剧、军旅等，如《大明王朝1566》《父母爱情》），在电视上的「奇异果TV」看
chat：问候、闲聊、倾诉、生活常识
urgent：身体严重不适（胸闷、胸痛、心慌、喘不上气、头晕、摔倒等）、人身安全问题、疑似诈骗（陌生人要转账、要验证码、冒充公检法/客服/亲友借钱）
feedback：对推荐过的地方或活动表达喜好，如「这个太远了」「我们不喜欢看展览」「这个上周去过了」「这种演出挺好」
</intents>

<rules>
1. 用 Context 里的 User 称呼对方。语气温暖尊敬，像贴心晚辈；简单口语，不用网络流行语，不用 markdown。
2. traffic/weather/news/outing/tv：只回一两句温暖确认，如「好的，我这就帮您查查～」；tv 可说「我给您挑几部好剧，在奇异果TV上就能看～」。具体信息由系统另发，绝不自己编路况、路线、活动或剧名。
3. chat：自然回应，不超过100字。chat 之后系统不会再发消息，所以绝不说「我帮您查一下/看看/马上告诉您」；只要对方想了解路况、活动、去处或电视剧，就必须用 traffic/weather/news/outing/tv。不知道就坦诚说，可建议问问孩子。
4. 健康话题不诊断，不推荐药物或剂量。
5. urgent（身体）：一律按严重情况处理，不要建议先休息观察。两三句短话，第一句就让对方马上拨打120，如「（称呼），请马上拨打 120！先坐下别走动，把门打开，我已经通知孩子了。」
6. urgent（诈骗）：和身体不适是两回事，不提 120。如「（称呼），先别转账，也别把验证码告诉任何人！先给孩子打个电话问清楚，我也已经通知孩子了。」你不是孩子，不要说「给我打电话」。
7. 被问你是谁：说你是孩子设置的小助手，名字是 Context 里的 Assistant。
8. feedback：温暖地确认，不要争辩。嫌远、去过、不喜欢时说「好的，记下了，以后少给您推荐这类」；喜欢时说「好的，记下了，以后多给您找这样的」。feedback 字段里 reason 必填：too_far（太远）、visited（去过了）、not_interested（不喜欢）、like（喜欢）；value 写对方提到的场馆或类别，没提就留空。
9. Context 里如有 Notes，是今天已核实的本地提醒（降温、供暖、道路封闭等）。和对方的话相关时可以顺带提一句；不相关就不提。绝不编造 Notes 以外的新闻。
</rules>

<output>
只输出一个 JSON 对象，不要任何其他内容：
{"intent": "traffic|weather|news|outing|tv|chat|urgent|feedback", "reply": "给对方的回复"}
只有 intent 为 feedback 时再加：
{"intent": "feedback", "reply": "…", "feedback": {"reason": "too_far|visited|not_interested|like", "value": "提到的场馆或类别，可为空"}}
</output>"""

LOCATION = "呼和浩特"


def build_user_content(text: str, name: str, now: datetime, assistant_name: str,
                       location: str = LOCATION, notes: list[str] | None = None,
                       extras: dict[str, str] | None = None) -> str:
    """All per-request context lives here, after the static system prompt.
    ``extras`` (Day / Recent / Prefs) are only included when non-empty."""
    stamp = f"{now:%Y-%m-%d %H:%M} 星期{WEEKDAYS[now.weekday()]}"
    extra = "".join(f", {k}={v}" for k, v in (extras or {}).items() if v)
    extra += f", Notes={'；'.join(notes)}" if notes else ""
    return (f"[Context: Time={stamp}, User={name}, Location={location}, Assistant={assistant_name}{extra}]\n"
            f"User Query: {text}")


@dataclass(frozen=True)
class AssistantResult:
    intent: str  # one of INTENTS
    reply: str
    urgent: bool  # LLM said urgent OR keyword safety net fired
    source: str  # "llm" | "fallback"
    feedback: dict | None = None  # raw feedback object (validated by PreferenceStore.resolve)

    @property
    def command(self) -> Command | None:
        return INTENT_TO_COMMAND.get(self.intent)


_SOFTENERS = ("如果", "若", "要是", "没有好转", "不见好", "观察", "先休息")


def _leads_with_call_120(reply: str) -> bool:
    """True if the FIRST sentence tells them to call 120 now (not 'rest, then call if worse')."""
    first = re.split(r"[。！!？?\n]", reply, maxsplit=1)[0]
    return "120" in first and not any(w in first for w in _SOFTENERS)


def looks_urgent(text: str) -> bool:
    return any(k in text for k in URGENT_KEYWORDS)


def keyword_fallback(text: str, name: str) -> AssistantResult:
    """Used when the LLM is unavailable: same behaviour as the keyword bot."""
    if looks_urgent(text):
        return AssistantResult("urgent", URGENT_FALLBACK_REPLY.format(name=name), True, "fallback")
    command = parse_command(text)
    if command is Command.HELP:
        return AssistantResult("chat", HELP_TEXT, False, "fallback")
    return AssistantResult(COMMAND_TO_INTENT[command], ACK_TEXT[command], False, "fallback")


class FamilyAssistant:
    def __init__(
        self,
        llm: LLMClient,
        assistant_name: str = "小暖",
        clock: Callable[[], float] = time.monotonic,
    ):
        self._llm = llm
        self._assistant_name = assistant_name
        self._clock = clock
        self._history: dict[str, deque[dict[str, str]]] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def _messages(self, user: str, name: str, text: str, now: datetime,
                  notes: list[str] | None = None, extras: dict[str, str] | None = None) -> list[dict[str, str]]:
        """[static system] + [history, verbatim as sent] + [new user turn with context header].

        History is replayed byte-for-byte, so in a conversation the cacheable prefix grows
        turn by turn (system + earlier turns), not just the system prompt.
        """
        with self._lock:
            if self._clock() - self._last_seen.get(user, float("-inf")) > HISTORY_IDLE_RESET_S:
                self._history.pop(user, None)
            history = list(self._history.get(user, ()))
        content = build_user_content(text, name, now, self._assistant_name, notes=notes, extras=extras)
        return [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": content}]

    def _remember(self, user: str, user_content: str, assistant_json: str) -> None:
        with self._lock:
            turns = self._history.setdefault(user, deque(maxlen=HISTORY_TURNS * 2))
            turns.append({"role": "user", "content": user_content})
            turns.append({"role": "assistant", "content": assistant_json})
            self._last_seen[user] = self._clock()

    def respond(self, user: str, name: str, text: str, now: datetime,
                notes: list[str] | None = None, extras: dict[str, str] | None = None) -> AssistantResult:
        """Never raises: falls back to keyword handling on any LLM problem.
        ``notes``: today's verified local reminders the reply may mention."""
        keyword_urgent = looks_urgent(text)
        messages = self._messages(user, name, text, now, notes, extras)
        try:
            # 0.5: warm replies, steadier intent classification / JSON than the 0.7 default
            data = self._llm.chat_json(messages, temperature=0.5, purpose="chat")
        except (LLMError, requests.RequestException) as exc:
            log.warning("LLM unavailable, using keyword fallback: %s", exc)
            return keyword_fallback(text, name)

        intent = str(data.get("intent", "")).strip().lower()
        if intent not in INTENTS:
            log.info("LLM returned unknown intent %r; treating as chat", intent)
            intent = "chat"
        keyword_command = parse_command(text)
        if intent == "traffic" and keyword_command is Command.WEATHER:
            # "这周天气咋样" is a weather question, not a commute report (no 路况/堵车/开车 in the text)
            log.info("keyword override: traffic → weather")
            intent = "weather"
        if intent == "chat" and keyword_command is not Command.HELP:
            # The model said "chat" but the message clearly asks for data; without this the parent
            # would only get "我帮您查一下…" and never the answer.
            log.info("keyword override: chat → %s", COMMAND_TO_INTENT[keyword_command])
            intent = COMMAND_TO_INTENT[keyword_command]
        reply = str(data.get("reply", "")).strip()
        if not reply:
            fallback = keyword_fallback(text, name)
            reply = fallback.reply if intent == fallback.intent else HELP_TEXT
        if len(reply) > MAX_REPLY_CHARS:
            reply = reply[:MAX_REPLY_CHARS].rstrip() + "…"

        self._remember(user, messages[-1]["content"],
                       json.dumps({"intent": intent, "reply": reply}, ensure_ascii=False))
        urgent = intent == "urgent" or keyword_urgent
        health = any(k in text for k in HEALTH_URGENT_KEYWORDS)
        scam = any(k in text for k in SCAM_KEYWORDS)
        if urgent and health and not _leads_with_call_120(reply):
            # Never let the model soften a possible cardiac emergency into "rest and see".
            reply = CALL_120_LEAD.format(name=name) + reply
        elif urgent and scam and not health and ("120" in reply or "转账" not in reply):
            # Small models sometimes answer a scam with the ambulance script; use the scam script.
            reply = SCAM_REPLY.format(name=name)
        if keyword_urgent and intent != "urgent":
            log.warning("keyword safety net flagged urgent message (LLM said %s)", intent)
        feedback = data.get("feedback") if intent == "feedback" and isinstance(data.get("feedback"), dict) else None
        return AssistantResult(intent, reply, urgent, "llm", feedback)
