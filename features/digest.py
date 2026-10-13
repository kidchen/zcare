"""Evening local-news digest for Mom and Dad: up to 5 items from the subscribed WeChat accounts.

Choices (agreed with the family): daily evening, everyone, ≤ 5 items, no topic/source filtering,
each card opens the ORIGINAL WeChat article (native in WeChat, no republishing).

Pipeline (two cheap GLM calls on the free model):
  1. Candidates: articles published today/yesterday that haven't been in a digest yet; the same story
     from several accounts is grouped (title similarity) — that's de-duplication, not filtering.
  2. Rank: one call over the candidates' titles + opening lines → score for "worth telling the parents".
  3. Summarize: one call over the top few articles' text → a plain title and 1–2 sentence summary.
  4. Check: every number in the summary/title must appear in the article; otherwise (or if GLM is down)
     the item falls back to the article's own title and first sentence. The digest always goes out
     when there are articles.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Iterable

from clients.llm_client import LLMClient, LLMError
from clients.wemp_rss import Article
from features.events import _numbers_backed
from messages import Article as Card
from messages import Message
from storage import StateStore

log = logging.getLogger(__name__)

STATE_KEY = "digest_sent"  # article keys already sent in a digest (no repeats)
TITLES_KEY = "digest_sent_titles"  # their titles: the same story from another account isn't repeated either
KEEP_SENT = 300
MAX_CANDIDATES = 40
SUMMARIZE_TOP = 7  # summarize a couple more than we show, in case some fail the checks
TEXT_CHARS = 1200
SAME_STORY = 0.6  # title similarity ratio
_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)

RANK_PROMPT = """<task>
用户消息里是呼和浩特本地公众号最近的文章列表（编号、来源、标题、开头几句）。挑出最值得讲给 60 岁左右的爸爸妈妈听的本地新闻，按重要性、和他们日常生活的相关度、有意思的程度打分。
</task>

<rules>
同一件事只选一篇。最多选 8 篇。score 为 1 到 5 的整数（5 最值得讲）。
</rules>

<output>
只输出一个 JSON 对象：{"picks": [{"id": 编号, "score": 1到5}]}
</output>"""

SUMMARY_PROMPT = """<task>
为用户消息里的每篇本地新闻，写一条讲给 60 岁左右长辈听的简短新闻。
</task>

<rules>
title：口语化的标题，不超过 22 个字，平实，不夸张、不吓人。
summary：一两句话，不超过 70 个字，说清楚是什么事、和咱们家的日常有什么关系。
数字、日期、地点、名称必须是原文里写的，不能编造或推算；不加个人评论。
</rules>

<output>
只输出一个 JSON 对象：{"items": [{"id": 编号, "title": "标题", "summary": "摘要"}]}
</output>"""


@dataclass(frozen=True)
class DigestItem:
    title: str
    summary: str
    url: str
    account: str
    key: str
    source_title: str = ""  # the article's own title (for cross-account duplicate detection)


def article_key(a: Article) -> str:
    return a.url or f"{a.title}|{a.published}"


def _norm_title(t: str) -> str:
    return _PUNCT.sub("", t)


_DIGITS = re.compile(r"\d+")


def same_story(a: str, b: str) -> bool:
    """Similar titles about the same thing. Different numbers mean different stories
    ("第1条…社区1号" vs "第2条…社区2号"); matching numbers support it ("10月15日起全面供暖")."""
    x, y = _norm_title(a), _norm_title(b)
    if not x or not y:
        return False
    nx, ny = set(_DIGITS.findall(a)), set(_DIGITS.findall(b))
    if nx and ny and not (nx <= ny or ny <= nx):
        return False
    return x in y or y in x or SequenceMatcher(None, x, y).ratio() >= SAME_STORY


def lead_sentence(text: str, limit: int = 70) -> str:
    """First sentence of the article — the extractive fallback summary."""
    text = " ".join(text.split())
    m = re.search(r"^(.{8,}?[。！？!?])", text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[: limit - 1] + "…"


def candidates(articles: Iterable[Article], today: date, sent: set[str],
               sent_titles: Iterable[str] = ()) -> list[Article]:
    """Today's and yesterday's articles not yet sent, one per story (the longest text wins).
    Stories already told (same story from another account) are skipped too."""
    sent_titles = list(sent_titles)
    pool = [a for a in articles if a.published and today - timedelta(days=1) <= a.published <= today
            and article_key(a) not in sent and a.title.strip()
            and not any(same_story(a.title, t) for t in sent_titles)]
    pool.sort(key=lambda a: (a.published, len(a.content)), reverse=True)
    stories: list[Article] = []
    for a in pool:
        if not any(same_story(a.title, s.title) for s in stories):
            stories.append(a)
    return stories[:MAX_CANDIDATES]


class DigestBuilder:
    def __init__(self, llm: LLMClient | None, state: StateStore, max_items: int = 5):
        self._llm = llm
        self._state = state
        self._max = max_items

    # ------------------------------------------------------------ GLM steps

    def _rank(self, pool: list[Article]) -> list[Article]:
        if self._llm is None or len(pool) <= 1:
            return pool
        docs = "\n".join(f"[{i}] 来源：{a.account}｜标题：{a.title}｜开头：{lead_sentence(a.content, 60)}"
                         for i, a in enumerate(pool, start=1))
        try:
            data = self._llm.chat_json([{"role": "system", "content": RANK_PROMPT},
                                        {"role": "user", "content": f"文章：\n{docs}"}],
                                       max_tokens=600, temperature=0.2, timeout=60, max_attempts=2, purpose="digest")
        except LLMError as exc:
            log.warning("digest ranking failed, using newest first: %s", exc)
            return pool
        scored: dict[int, tuple[int, int]] = {}  # idx → (score, position in GLM's list)
        for pos, p in enumerate(data.get("picks") or []):
            try:
                idx, score = int(p.get("id")) - 1, int(p.get("score", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            if 0 <= idx < len(pool) and idx not in scored:
                scored[idx] = (score, pos)
        if not scored:
            return pool
        ranked = sorted(scored, key=lambda i: (-scored[i][0], scored[i][1]))  # ties: GLM's own order
        return [pool[i] for i in ranked] + [a for i, a in enumerate(pool) if i not in scored]

    def _summarize(self, top: list[Article]) -> dict[int, tuple[str, str]]:
        if self._llm is None or not top:
            return {}
        docs = "\n\n".join(f"[{i}] 来源：{a.account}\n标题：{a.title}\n内容：{' '.join(a.content.split())[:TEXT_CHARS]}"
                           for i, a in enumerate(top, start=1))
        try:
            data = self._llm.chat_json([{"role": "system", "content": SUMMARY_PROMPT},
                                        {"role": "user", "content": f"文章：\n{docs}"}],
                                       max_tokens=1500, temperature=0.3, timeout=90, max_attempts=2, purpose="digest")
        except LLMError as exc:
            log.warning("digest summaries failed, using article leads: %s", exc)
            return {}
        out: dict[int, tuple[str, str]] = {}
        for it in data.get("items") or []:
            try:
                idx = int(it.get("id")) - 1
            except (TypeError, ValueError, AttributeError):
                continue
            title, summary = str(it.get("title", "")).strip(), str(it.get("summary", "")).strip()
            if 0 <= idx < len(top) and title and summary:
                out[idx] = (title[:30], summary[:90])
        return out

    # ------------------------------------------------------------ public

    def build(self, articles: list[Article], today: date) -> list[DigestItem]:
        sent = set(self._state.recent(STATE_KEY))
        pool = candidates(articles, today, sent, self._state.recent(TITLES_KEY))
        if not pool:
            return []
        top = self._rank(pool)[:SUMMARIZE_TOP]
        summaries = self._summarize(top)
        items: list[DigestItem] = []
        for i, a in enumerate(top):
            text = f"{a.title}\n{a.content}"
            title, summary = summaries.get(i, ("", ""))
            if not (title and summary and _numbers_backed(title, text) and _numbers_backed(summary, text)):
                if title or summary:
                    log.info("digest item falls back to the article's own words: %s", a.title)
                title, summary = a.title.strip(), lead_sentence(a.content) or a.title.strip()
            items.append(DigestItem(title, summary, a.url, a.account, article_key(a), a.title))
            if len(items) >= self._max:
                break
        return items

    def remember(self, items: list[DigestItem]) -> None:
        self._state.remember(STATE_KEY, [i.key for i in items], keep=KEEP_SENT)
        self._state.remember(TITLES_KEY, [i.source_title for i in items], keep=KEEP_SENT)


def format_digest_message(items: list[DigestItem], name: str = "爸爸妈妈", greeting: str = "晚上好") -> Message:
    lines = [f"**{name}{greeting} 📰 今天呼和浩特的新鲜事**", ""]
    cards: list[Card] = []
    for n, it in enumerate(items, start=1):
        src = f"（来源：{it.account}）" if it.account else ""
        lines += [f"**{n}. {it.title}**", f"> {it.summary}{src}"]
        if it.url:
            lines.append(f"[阅读原文]({it.url})")
        lines.append("")
        if it.url.startswith("http"):
            cards.append(Card(title=f"{n}. {it.title}", description=f"{it.summary}\n来源：{it.account}" if it.account
                              else it.summary, url=it.url))
    lines.append("点开就能看原文，看累了就歇歇眼睛～")
    return Message(title=f"今天呼和浩特的新鲜事 · {items[0].title}", markdown="\n".join(lines).strip(),
                   url=items[0].url or None, articles=tuple(cards))
