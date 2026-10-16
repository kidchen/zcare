"""Lightweight family preference memory, learned from chat feedback.

Mom and Dad say things like "这个太远了", "我们不喜欢看这个展览", "这个上周去过了". The assistant
classifies these as intent "feedback" (with a target that must match something we actually
recommended recently, or a known category), and we store it here. Outing selection then skips
disliked venues and pushes disliked categories down.

Stored in the shared state file:
  prefs_v1   = {"venues": {name: {"reason", "until"}}, "categories": {cat: {"reason", "until", "count"}},
                "liked_categories": {cat: count}, "too_far_count": int}
  last_recs  = {"at": ts, "items": [{"title", "venue", "category"}]}
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from storage import StateStore

PREFS_KEY = "prefs_v1"
LAST_RECS_KEY = "last_recs"
RECENT_RECS_MAX_AGE_S = 3 * 86400  # "这个" only refers to things recommended in the last 3 days
REASONS = ("too_far", "visited", "not_interested", "like")
DISLIKE_DAYS = {"visited": 30, "too_far": 180, "not_interested": 90}
NEAR_ONLY_AFTER = 2  # after 2 "too far" complaints, prefer routes ≤ NEAR_MAX_MINUTES
NEAR_MAX_MINUTES = 45
KNOWN_CATEGORIES = ("演出", "展览", "电影", "讲座", "民俗", "博物馆", "美术馆", "艺术馆", "公园", "图书馆",
                    "寺庙广场", "展览活动")
FEEDBACK_KEYWORDS = {
    "too_far": ("太远", "远了", "不方便", "折腾"),
    "visited": ("去过", "看过了", "逛过"),
    "not_interested": ("不喜欢", "没意思", "不感兴趣", "不想去", "不爱看"),
}
_NOISE = re.compile(r"[\s（）()·\-—_]+")
_REGION = re.compile(r"^(内蒙古自治区|内蒙古|呼和浩特市|呼和浩特|呼市)")


def mentioned(venue: str, text: str) -> bool:
    """Parents say the short name: "美术馆没意思" refers to 呼和浩特市美术馆."""
    core = _REGION.sub("", _norm(venue))
    return bool(venue) and (venue in text or (len(core) >= 2 and core in text))


def _norm(name: str) -> str:
    return _NOISE.sub("", name or "")


def same_place(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a in b or b in a)


@dataclass(frozen=True)
class Feedback:
    target: str  # "venue" | "category"
    value: str
    reason: str  # too_far | visited | not_interested | like


def keyword_feedback(text: str) -> str | None:
    """Reason implied by the wording, for the no-LLM fallback."""
    for reason, words in FEEDBACK_KEYWORDS.items():
        if any(w in text for w in words):
            return reason
    return None


class PreferenceStore:
    def __init__(self, state: StateStore, clock: Callable[[], float] = time.time):
        self._state = state
        self._clock = clock

    # ------------------------------------------------------------ recommendations

    def remember_recommendations(self, items: Iterable[dict]) -> None:
        items = [{"title": str(i.get("title", ""))[:40], "venue": str(i.get("venue", ""))[:30],
                  "category": str(i.get("category", ""))[:10]} for i in items if i.get("venue")]
        if items:
            self._state.set(LAST_RECS_KEY, {"at": self._clock(), "items": items[:5]})

    def recent_recommendations(self) -> list[dict]:
        data = self._state.get(LAST_RECS_KEY) or {}
        if self._clock() - float(data.get("at", 0)) > RECENT_RECS_MAX_AGE_S:
            return []
        return list(data.get("items", []))

    # ------------------------------------------------------------ feedback

    def resolve(self, raw: dict | None, text: str = "") -> Feedback | None:
        """Turn the LLM's feedback into a verified Feedback, deciding the target in code.

        The model reliably tells *that* it's feedback and *why* (reason); its target/value fields are
        only hints (live tests: "美术馆那个去过了" came back as category 展览). So:
          - venue  = a recent recommendation named in the parent's words or in the hint,
                     or — for "这个/那个" — the first thing we recommended;
          - category = a known category named in the words / hint;
          - visited / too_far are about places → venue; like / not_interested prefer an explicit
            category ("不喜欢看展览"), else the venue.
        Anything that doesn't resolve to something we actually recommended (or a known category)
        is dropped.
        """
        if not isinstance(raw, dict):
            return None
        reason = str(raw.get("reason", "")).strip()
        if reason not in REASONS:
            return None
        hint = f"{raw.get('target', '')} {raw.get('value', '')}"
        recent = self.recent_recommendations()
        venue = next((r["venue"] for r in recent if mentioned(r["venue"], text) or mentioned(r.get("title", ""), text)), None)
        venue = venue or next((r["venue"] for r in recent if mentioned(r["venue"], hint)
                               or (str(raw.get("value", "")).strip() and same_place(r["venue"], str(raw.get("value", ""))))), None)
        deictic = any(w in text for w in ("这个", "那个", "这次", "上次", "这里", "那里", "这儿", "那儿"))
        if venue is None and deictic and recent:
            venue = recent[0]["venue"]
        category = next((c for c in KNOWN_CATEGORIES if c in text), None) \
            or next((c for c in KNOWN_CATEGORIES if c in hint), None)
        if reason in ("visited", "too_far"):
            return Feedback("venue", venue, reason) if venue else None
        if category and (category in text or venue is None):
            return Feedback("category", category, reason)
        return Feedback("venue", venue, reason) if venue else None

    def fallback_from_text(self, text: str) -> Feedback | None:
        """No LLM: "这个太远了" → the first venue we recommended most recently."""
        reason = keyword_feedback(text)
        recent = self.recent_recommendations()
        if not reason or not recent:
            return None
        named = next((r for r in recent if mentioned(r["venue"], text)), None)
        return Feedback("venue", (named or recent[0])["venue"], reason)

    def record(self, fb: Feedback) -> None:
        prefs = self._load()
        now = self._clock()
        if fb.reason == "like":
            if fb.target == "category":
                prefs["liked_categories"][fb.value] = prefs["liked_categories"].get(fb.value, 0) + 1
            else:
                prefs["venues"].pop(fb.value, None)  # liked again → no longer avoided
        else:
            until = now + DISLIKE_DAYS[fb.reason] * 86400
            if fb.target == "venue":
                prefs["venues"][fb.value] = {"reason": fb.reason, "until": until}
            else:
                cur = prefs["categories"].get(fb.value, {"count": 0})
                prefs["categories"][fb.value] = {"reason": fb.reason, "until": until, "count": cur.get("count", 0) + 1}
            if fb.reason == "too_far":
                prefs["too_far_count"] = prefs.get("too_far_count", 0) + 1
        self._state.set(PREFS_KEY, prefs)

    # ------------------------------------------------------------ scoring

    def _load(self) -> dict:
        prefs = self._state.get(PREFS_KEY) or {}
        prefs.setdefault("venues", {})
        prefs.setdefault("categories", {})
        prefs.setdefault("liked_categories", {})
        prefs.setdefault("too_far_count", 0)
        return prefs

    def venue_blocked(self, *names: str) -> bool:
        now = self._clock()
        return any(same_place(v, n) for v, info in self._load()["venues"].items()
                   if info.get("until", 0) > now for n in names if n)

    def category_penalty(self, category: str) -> int:
        """0 = fine, 1 = push down, 2 = said twice → effectively excluded. Negative = liked."""
        prefs = self._load()
        info = prefs["categories"].get(category)
        if info and info.get("until", 0) > self._clock():
            return min(2, int(info.get("count", 1)))
        return -1 if prefs["liked_categories"].get(category) else 0

    def max_route_minutes(self) -> int | None:
        return NEAR_MAX_MINUTES if self._load().get("too_far_count", 0) >= NEAR_ONLY_AFTER else None

    def summary(self) -> str:
        """Short text for the chat context (so the assistant can acknowledge known tastes)."""
        prefs, now = self._load(), self._clock()
        parts = [f"不去{v}" for v, i in prefs["venues"].items() if i.get("until", 0) > now][:3]
        parts += [f"少推荐{c}" for c, i in prefs["categories"].items() if i.get("until", 0) > now][:2]
        return "、".join(parts)
