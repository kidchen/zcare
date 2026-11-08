"""Family feedback loop: capture → acknowledge → review (you) → fix → tell the parent it's done.

* **Capture.** The chat assistant (same GLM call, no extra request) tags feedback with a ``kind``:
  place (这个太远了 — handled by preferences.py, applied at once) · content (新闻太多了, 路况不准) · bug (链接打不开)
  · wish (能不能告诉我菜价) · praise (这个剧真好看). Each item is stored with what it is about: the last message
  that parent received (``remember_delivery``).
* **Review.** New items appear in the 21:00 report; you manage them by chatting with 小助理 as admin:
  「反馈」 list open items · 「同意 3」 apply #3's proposed adjustment · 「改好了 3 说明」 close #3 and tell the
  parent · 「不改 3」 close quietly.
* **Self-correction (scaffold).** For "more / less" complaints GLM may propose an ``adjust`` of a bounded knob
  (news items, drama picks, walking limit). The code validates knob and direction and computes the new value
  within bounds. With FEEDBACK_AUTO_APPLY=false (default) it waits for 「同意」; with true it is applied at once
  and you are told what changed. Overrides live in state ("tuning") on top of .env.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from storage import StateStore

ITEMS_KEY = "feedback_v1"
TUNING_KEY = "tuning_v1"
LAST_DELIVERY_KEY = "last_delivery"
DIGEST_SENT_KEY = "feedback_digest_at"
KEEP_ITEMS = 300
ABOUT_MAX_AGE_S = 36 * 3600  # "这个" refers to something sent in the last day and a half
KINDS = ("place", "content", "bug", "wish", "praise")
TOPICS = ("traffic", "outing", "news", "tv", "weather", "chat", "other")
TOPIC_CN = {"traffic": "路况", "outing": "出行", "news": "新闻", "tv": "电视剧", "weather": "天气", "chat": "聊天",
            "other": "其他"}
KIND_CN = {"place": "地点偏好", "content": "内容", "bug": "故障", "wish": "心愿", "praise": "夸奖"}
ROLE_CN = {"father": "爸爸", "mother": "妈妈", "admin": "你"}


@dataclass(frozen=True)
class Knob:
    key: str
    setting: str  # Settings field it overrides
    label: str
    low: int
    high: int
    step: int
    unit: str = ""


KNOBS = {
    "digest_items": Knob("digest_items", "digest_max_items", "晚间新闻条数", 3, 8, 1, "条"),
    "tv_picks": Knob("tv_picks", "tv_picks", "好剧推荐部数", 2, 5, 1, "部"),
    "walk_limit": Knob("walk_limit", "outing_max_walk_m", "出行路线最多步行", 300, 1500, 200, "米"),
}

ACK = {"content": "好的，记下了！我把您的意见告诉孩子，改好了再告诉您～",
       "bug": "哎呀，给您添麻烦了！我已经告诉孩子，修好了马上告诉您～",
       "wish": "这个想法真好！我记下来告诉孩子，能做的话就给您加上～",
       "praise": "太好了，您喜欢就好！我也告诉孩子一声～"}


@dataclass(frozen=True)
class Item:
    id: int
    ts: float
    role: str
    text: str
    kind: str
    topic: str
    about: str = ""  # what they were reacting to, e.g. "晚间本地新闻 · 今晚5条本地新闻"
    status: str = "open"  # open | applied | fixed | dismissed
    proposal: dict | None = None  # {"knob", "from", "to"}
    note: str = ""  # your fix note, sent to the parent


KNOB_TOPIC = {"digest_items": "news", "tv_picks": "tv", "walk_limit": "outing"}
MORE_WORDS = ("太少", "少了", "不够", "多一点", "多些", "多推荐", "再多", "多给")
LESS_WORDS = ("太多", "多了", "少一点", "少些", "太长", "看不过来", "太远", "走太多", "走不动", "走路多")
BUG_WORDS = ("打不开", "点不开", "点不了", "没收到", "收不到", "看不了", "不显示", "出错", "坏了", "报错", "卡住")
WISH_WORDS = ("能不能", "可不可以", "可以不可以", "希望", "想要", "要是能", "加个", "增加", "也告诉我", "帮我也")
PRAISE_WORDS = ("好看", "不错", "挺好", "真好", "喜欢", "谢谢", "有用", "太棒", "很好")
NEGATIVE_WORDS = ("不好", "不喜欢", "没意思", "不行", "不准", "难看", "不爱")
TOPIC_WORDS = {"news": ("新闻", "新鲜事"), "tv": ("剧", "电视"), "traffic": ("路况", "堵", "开车"),
               "weather": ("天气", "下雨", "温度"), "outing": ("公园", "活动", "走路", "公交", "去哪")}


def parse_feedback(raw: Any, text: str = "") -> tuple[str, str, dict | None]:
    """GLM's feedback object → (kind, topic, adjust), verified against what the parent actually wrote.

    GLM proposes, code verifies (live probes: 「路况不准」 came back with a news-count adjustment, 「链接打不开」 as
    content): clear wording overrides the kind (bug / wish); a knob must belong to the topic, and the message itself
    must say more / less — otherwise no automatic adjustment."""
    raw = raw if isinstance(raw, dict) else {}
    kind = str(raw.get("kind", "")).strip().lower()
    topic = str(raw.get("topic", "")).strip().lower()
    if kind not in KINDS:
        kind = "place" if str(raw.get("reason", "")) in ("too_far", "visited", "not_interested", "like") else "content"
    if kind != "place" and text:
        if any(w in text for w in BUG_WORDS):
            kind = "bug"
        elif any(w in text for w in WISH_WORDS):
            kind = "wish"
        elif kind == "content" and any(w in text for w in PRAISE_WORDS) and not any(n in text for n in NEGATIVE_WORDS):
            kind = "praise"
    if text and (topic not in TOPICS or topic == "other"):
        topic = next((t for t, words in TOPIC_WORDS.items() if any(w in text for w in words)), "other")
    topic = topic if topic in TOPICS else "other"
    adjust = raw.get("adjust") if isinstance(raw.get("adjust"), dict) else None
    if adjust:
        knob, direction = adjust.get("knob"), adjust.get("direction")
        said = (any(w in text for w in MORE_WORDS) if direction == "more"
                else any(w in text for w in LESS_WORDS) if direction == "less" else False)
        if (kind != "content" or knob not in KNOBS or KNOB_TOPIC.get(knob) != topic or (text and not said)):
            adjust = None
    return kind, topic, adjust


class FeedbackStore:
    def __init__(self, state: StateStore, clock: Callable[[], float] = time.time):
        self._state, self._clock = state, clock

    # ------------------------------------------------------------ context

    def remember_delivery(self, role: str, what: str) -> None:
        """What a parent last received (scheduled or on demand) — the "这个" in their feedback."""
        last = dict(self._state.get(LAST_DELIVERY_KEY) or {})
        last[role] = {"at": self._clock(), "what": what[:60]}
        self._state.set(LAST_DELIVERY_KEY, last)

    def last_delivery(self, role: str) -> str:
        d = (self._state.get(LAST_DELIVERY_KEY) or {}).get(role) or {}
        return d.get("what", "") if self._clock() - float(d.get("at", 0)) < ABOUT_MAX_AGE_S else ""

    # ------------------------------------------------------------ items

    def items(self) -> list[Item]:
        out = []
        for raw in self._state.get(ITEMS_KEY) or []:
            try:
                out.append(Item(**raw))
            except TypeError:
                continue
        return out

    def _save(self, items: list[Item]) -> None:
        self._state.set(ITEMS_KEY, [i.__dict__ for i in items[-KEEP_ITEMS:]])

    def get(self, item_id: int) -> Item | None:
        return next((i for i in self.items() if i.id == item_id), None)

    def add(self, role: str, text: str, kind: str, topic: str, adjust: dict | None, settings: Any) -> Item:
        items = self.items()
        proposal = self._proposal(adjust, settings)
        item = Item(max((i.id for i in items), default=0) + 1, self._clock(), role, text[:200], kind, topic,
                    self.last_delivery(role), "open", proposal)
        self._save(items + [item])
        return item

    def update(self, item: Item) -> None:
        self._save([item if i.id == item.id else i for i in self.items()])

    def open_items(self) -> list[Item]:
        return [i for i in self.items() if i.status == "open" and i.kind != "praise"]

    def since(self, seconds: float) -> list[Item]:
        cutoff = self._clock() - seconds
        return [i for i in self.items() if i.ts >= cutoff]

    # ------------------------------------------------------------ async summary to you

    def digest_lines(self) -> list[str]:
        """Everything since the last summary: [headline, …lines] — [] if nothing happened."""
        since = float(self._state.get(DIGEST_SENT_KEY) or 0)
        new = [i for i in self.items() if i.ts > since]
        if not new:
            return []
        auto = [i for i in new if i.status == "applied"]
        todo = [i for i in new if i.status == "open" and i.kind in ("content", "bug", "wish")]
        praise = [i for i in new if i.kind == "praise"]
        head = f"新 {len(new)} 条" + (f"，自动调整 {len(auto)} 项" if auto else "") + (f"，需要你处理 {len(todo)} 条" if todo else "")
        lines = [head]
        if auto:
            lines += ["✅ 已自动调整（不合适回复「不改 编号」改回去）："] + [f"{item_line(i)} → {i.note}" for i in auto]
        if todo:
            lines += ["🛠 需要你改代码/内容（改好后回复「改好了 编号 说明」，会告诉家人）："] + [item_line(i) for i in todo]
        if praise:
            lines += ["💐 夸奖："] + [item_line(i) for i in praise]
        older = len([i for i in self.open_items() if i.ts <= since])
        if older:
            lines.append(f"另有 {older} 条之前的反馈还没处理（回复「反馈」查看）")
        return lines

    def mark_digest_sent(self) -> None:
        self._state.set(DIGEST_SENT_KEY, self._clock())

    # ------------------------------------------------------------ tuning

    def tuning(self) -> dict[str, int]:
        return {k: int(v) for k, v in (self._state.get(TUNING_KEY) or {}).items() if k in KNOBS}

    def value(self, knob: Knob, settings: Any) -> int:
        return self.tuning().get(knob.key, int(getattr(settings, knob.setting)))

    def _proposal(self, adjust: dict | None, settings: Any) -> dict | None:
        if not adjust:
            return None
        knob = KNOBS[adjust["knob"]]
        cur = self.value(knob, settings)
        new = max(knob.low, min(knob.high, cur + (knob.step if adjust["direction"] == "more" else -knob.step)))
        return {"knob": knob.key, "from": cur, "to": new} if new != cur else None

    def apply(self, item: Item) -> Item:
        """Apply the item's proposal (knob override) → the item, marked applied."""
        if not item.proposal:
            return item
        tuning = self.tuning()
        tuning[item.proposal["knob"]] = int(item.proposal["to"])
        self._state.set(TUNING_KEY, tuning)
        done = replace(item, status="applied", note=describe_change(item.proposal))
        self.update(done)
        return done

    def tuned(self, settings: Any) -> Any:
        """Settings with the approved overrides applied."""
        changes = {KNOBS[k].setting: v for k, v in self.tuning().items()}
        return replace(settings, **changes) if changes else settings


def describe_change(p: dict) -> str:
    knob = KNOBS[p["knob"]]
    return f"{knob.label}从 {p['from']}{knob.unit} 改成 {p['to']}{knob.unit}"


def item_line(i: Item) -> str:
    when = datetime.fromtimestamp(i.ts).strftime("%m-%d %H:%M")
    about = f"（关于：{i.about}）" if i.about else ""
    prop = f" · 建议：{describe_change(i.proposal)}，回复「同意 {i.id}」" if i.proposal and i.status == "open" else ""
    who = f"{ROLE_CN.get(i.role, i.role)}·{KIND_CN.get(i.kind, i.kind)}/{TOPIC_CN.get(i.topic, i.topic)}"
    return f"#{i.id} {when} {who}：「{i.text}」{about}{prop}"


def done_message(i: Item, name: str) -> str:
    """To the parent when you close their item."""
    day = datetime.fromtimestamp(i.ts)
    return (f"{name}，您 {day.month}月{day.day}日 说的「{i.text[:40]}」，孩子已经改好了"
            + (f"：{i.note}" if i.note else "") + "～谢谢您提意见，有什么不满意的随时告诉我！")


# ------------------------------------------------------------ admin commands

_CMD = re.compile(r"^\s*(反馈|同意|改好了|不改)\s*#?(\d+)?\s*(.*)$", re.S)


@dataclass(frozen=True)
class AdminCommand:
    action: str  # list | approve | fixed | dismiss
    item_id: int | None = None
    note: str = ""


def parse_admin_command(text: str) -> AdminCommand | None:
    m = _CMD.match(text or "")
    if not m:
        return None
    word, num, rest = m.group(1), m.group(2), m.group(3).strip()
    if word == "反馈":
        return AdminCommand("list") if not num and not rest else None
    if not num:
        return None
    return AdminCommand({"同意": "approve", "改好了": "fixed", "不改": "dismiss"}[word], int(num), rest[:80])


def report_lines(store: FeedbackStore, window_s: float = 86400) -> tuple[str, list[str]]:
    """(short line for the WeCom text, markdown lines for the full report)."""
    new = [i for i in store.since(window_s) if i.kind != "place"]
    open_ = store.open_items()
    short = f"家人反馈：新 {len(new)} 条，待处理 {len(open_)} 条" + ("（回复「反馈」查看）" if open_ else "")
    md = ["**📝 家人反馈**"]
    md += [f"- 🆕 {item_line(i)}" for i in new] or ["- 今天没有新反馈"]
    older = [i for i in open_ if i not in new]
    if older:
        md += ["", "待处理："] + [f"- {item_line(i)}" for i in older[:20]]
    md += ["", "处理：「同意 编号」应用建议的调整 ·「改好了 编号 说明」告诉家人 ·「不改 编号」"]
    return short, md
