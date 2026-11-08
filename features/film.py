"""「给我推荐两部周润发的电影」— films / series for a specific actor, director or title, straight from Douban.

GLM only extracts WHAT was asked (``tv_query`` = "周润发", ``tv_kind`` = movie / tv); code checks it is in the
parent's words (or their previous question, for "电影名字给我"). The titles come from Douban's mobile search
(m.douban.com/rexxar/api/v2/search/movie), which answers a person's name with their films — so nothing is
invented: a result is kept only if the asked name is in its title or in its credits line (director / leads),
and its Douban rating is decent. Where to watch is a suggestion (search initials in 奇异果TV), never a promise.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

from clients.http import build_session
from features.tv_recommend import MOBILE_PAGE_URL, MOBILE_UA, search_initials
from messages import Message

log = logging.getLogger(__name__)

SEARCH_URL = "https://m.douban.com/rexxar/api/v2/search/movie"
SHOWN = 4
MIN_RATING = 6.5
MAX_QUERY_CHARS = 20
KIND_CN = {"movie": "电影", "tv": "电视剧"}


@dataclass(frozen=True)
class Title:
    id: str
    name: str
    year: str
    rating: float
    kind: str  # 电影 | 电视剧
    credits: str  # "中国香港 / 剧情 动作 犯罪 / 吴宇森 / 周润发 狄龙"

    @property
    def genres(self) -> str:
        parts = [p.strip() for p in self.credits.split("/")]
        return parts[1].replace(" ", "、") if len(parts) > 1 else ""

    @property
    def people(self) -> str:
        parts = [p.strip() for p in self.credits.split("/")]
        return " / ".join(parts[2:])


def verified_query(raw: object, text: str, previous: str = "") -> str:
    """The asked actor / director / title, only if it's in their words (this message or the previous one)."""
    q = re.sub(r"(的电影|的电视剧|的剧|的片子|演的|主演|电影|电视剧)$", "", str(raw or "").strip().strip("《》「」\"'"))
    if not q or len(q) > MAX_QUERY_CHARS:
        return ""
    return q if q in text or (previous and q in previous) else ""


def parse_results(data: dict) -> list[Title]:
    out = []
    for it in (data or {}).get("items") or []:
        t = (it or {}).get("target") or {}
        if not t.get("id") or not t.get("title"):
            continue
        try:
            rating = float((t.get("rating") or {}).get("value") or 0)
        except (TypeError, ValueError):
            rating = 0.0
        out.append(Title(str(t["id"]), str(t["title"]), str(t.get("year") or ""), rating,
                         str(it.get("type_name") or ""), str(t.get("card_subtitle") or "")))
    return out


def search(query: str, kind: str = "", session: requests.Session | None = None, count: int = 20) -> list[Title]:
    """Douban titles that really match ``query`` (in the title or the credits), best rated first."""
    session = session or build_session(MOBILE_UA)
    resp = session.get(SEARCH_URL, params={"q": query, "count": count}, timeout=15,
                       headers={"User-Agent": MOBILE_UA, "Referer": "https://m.douban.com/movie/"})
    resp.raise_for_status()
    found = [t for t in parse_results(resp.json())
             if (query in t.name or query in t.people) and t.rating >= MIN_RATING]
    if kind in KIND_CN:
        wanted = [t for t in found if t.kind == KIND_CN[kind]]
        found = wanted or found  # asked for films but Douban only has series: still show something real
    return sorted(found, key=lambda t: -t.rating)[:SHOWN]


def build_film_message(query: str, kind: str, name: str, app: str = "奇异果TV",
                       session: requests.Session | None = None, greeting: str = "你好") -> Message:
    what = KIND_CN.get(kind, "影视")
    try:
        titles = search(query, kind, session=session)
    except (requests.RequestException, ValueError) as exc:
        log.warning("douban search for %s failed: %s", query, exc)
        titles = []
    if not titles:
        md = f"**{name}{greeting} 🎬**\n\n在豆瓣上没找到「{query}」相关的{what}，能换个说法吗？比如演员的全名或者片名～"
        return Message(title=f"{query}的{what}", markdown=md, headline=f"没找到{query}的{what}")
    about_title = any(t.name == query for t in titles)  # 「《父母爱情》好看吗」 vs 「周润发的电影」
    heading = f"《{query}》" + ("" if len(titles) == 1 else f"和相关的{what}") if about_title else f"{query}的{what}"
    lines = [f"**{name}{greeting} 🎬 {heading}**", ""]
    for i, t in enumerate(titles, start=1):
        meta = " · ".join(x for x in (f"⭐ {t.rating:.1f}", t.year, t.genres) if x)
        initials = search_initials(t.name)
        lines.append(f"**{i}. 《{t.name}》** {meta}")
        if t.people:
            lines.append(f"> {t.people}")
        lines += [f"> 📺 在电视上打开「{app}」，搜「{initials or t.name}」看看有没有",
                  f"[豆瓣详情]({MOBILE_PAGE_URL.format(id=t.id)})", ""]
    lines.append("评分来自豆瓣；有没有版权以电视上能搜到为准～")
    md = "\n".join(lines)
    headline = (f"《{query}》豆瓣 {titles[0].rating:.1f} 分" if about_title and titles[0].name == query
                else f"{query}的{what}：《{titles[0].name}》")
    return Message(title=f"{heading} · 《{titles[0].name}》等{len(titles)}部", markdown=md.strip(), headline=headline)
