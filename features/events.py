"""Real, upcoming Hohhot cultural events + real-time local notices. No web search.

Sources:
  * WeChat Official Account articles via self-hosted we-mp-rss (clients/wemp_rss.py) — the local
    news / venue / traffic accounts you subscribe to. GLM (free model) only *extracts*.
  * Official weather warnings from the National Meteorological Center (clients/nmc_client.py) —
    used directly as weather notices, no LLM.

Incremental extraction (each article goes to GLM once):
  1. New, relevant articles (keyword pre-filter, ≤ retention days old) are sent in small batches with
     a STATIC prompt that doesn't depend on today's date (dates are resolved against each article's
     publish date). Raw results + the article text are stored in a separate cache file.
  2. Every time events/notices are needed, the stored raw items are re-checked in code against today:
       - events: date (and venue, price) literally in the article; date within the horizon;
       - notices: verbatim quote, numbers with units, date backed by the text, source ≤ 3 days old;
       - (later, in outing.py) AMap confirms the venue exists in Hohhot.
  3. A warm-up job runs right after the we-mp-rss fetches, so messages rarely wait on GLM.
Measured before this design: one 40-article prompt ≈ 7.4k prompt tokens and ~30 s, resent on every refresh.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from clients.llm_client import LLMClient, LLMError
from clients.nmc_client import WeatherWarning
from clients.wemp_rss import Article
from storage import StateStore

log = logging.getLogger(__name__)

# Pre-filters so only relevant articles reach the LLM. Bare "活动" is deliberately absent: it matched
# government-meeting posts ("开展…活动"); only cultural phrasings count.
EVENT_KEYWORDS = re.compile(r"演出|展览|展出|展演|音乐会|二人台|戏曲|京剧|晋剧|评剧|话剧|舞剧|开票|放映|非遗|民俗|"
                            r"集市|庙会|文化馆|博物院|博物馆|美术馆|大剧院|剧场|艺术馆|"
                            r"活动预告|文化活动|公益活动|惠民活动|群众文化")
NOTICE_KEYWORDS = re.compile(r"封闭|管制|限行|禁行|改道|施工|停水|停电|停气|供暖|供热|燃气|电子警察")
RELEVANT = re.compile(f"{EVENT_KEYWORDS.pattern}|{NOTICE_KEYWORDS.pattern}")
EVENTS_MAX_AGE_DAYS = 21
ARTICLES_CACHE_S = 30 * 60  # one feed fetch per 30 min at most
WARNING_MAX_AGE_HOURS = 48
WARNINGS_CACHE_S = 30 * 60


class ArticleSource(Protocol):
    def articles(self) -> list[Article]: ...


class WarningSource(Protocol):
    def warnings(self) -> list[WeatherWarning]: ...


CATEGORIES = ("演出", "展览", "电影", "讲座", "民俗", "其他")
REALTIME_CACHE_KEY = "realtime_cache"  # last computed notices (read by the chat without any network call)
NOTICE_AUDIENCE = {"天气": "both", "民生": "both", "交通": "father"}  # 交通 matters to the driver
NOTICE_ORDER = ("天气", "民生", "交通")
MAX_SOURCE_AGE_DAYS = 3  # older posts are not "real-time"
NOTICE_HORIZON_DAYS = 3
FRESH_DAYS = 1  # events from posts ≤ 1 day old are marked 🆕
RELATIVE_TODAY = ("今天", "今日", "今晨", "今早", "今晚", "今夜", "当天")
RELATIVE_TOMORROW = ("明天", "明日", "明早", "明晚")
WEEKDAYS = "一二三四五六日"
ARTICLE_TEXT_CHARS = 1200  # per article (each is sent once, so we can afford more than the old 600)
BATCH_SIZE = 6
MAX_BATCHES_PER_REFRESH = 6  # bounds a cold start; the rest is picked up by the next warm-up
EXTRACTION_TIMEOUT_S = 120
EXTRACTION_ATTEMPTS = 2
EXTRACTION_MAX_TOKENS = 3000
_PAREN = re.compile(r"[（(].*?[)）]")

# STATIC: no "today" in it — results are a pure function of the articles, so they can be cached forever.
ARTICLE_EXTRACTION_PROMPT = """<task>
用户消息里是呼和浩特本地公众号文章（编号 [1]…[n]，带来源和发布日期）。逐篇找出：
1. events：文章里写明日期、在呼和浩特举办、适合 60 岁左右长辈参加的具体活动：戏曲、二人台、民歌或民族音乐会、民族舞蹈、话剧、展览和博物馆特展、文化馆公益活动、公益电影放映、临时集市、公园和广场的民俗活动。
2. notices：对爸爸妈妈日常生活有用的提醒：交通（道路封闭、管制、限行、新启用的电子警察）、民生（供暖、停水、停电、燃气）。天气不用提取（系统直接用气象台预警）。
</task>

<exclude>
在该篇发布日期之前就已结束的活动；只有开放时间或闭馆公告、没有具体活动的内容；亲子或儿童专场；电竞、酒吧、Livehouse、流行偶像演唱会、相亲、商业促销；不在呼和浩特的活动；人事任免、会议、经济数据、排名、招聘、学校通知。
</exclude>

<rules>
严格只用文章里明确写出的信息，不猜测、不编造。
日期一律按该篇的发布日期换算：写"今天/今日"就是发布日期，写"明天"就是发布日期加一天；原文只说"本周日"而没有具体日期的不要。票价照抄原文。
notice 的 quote：从原文逐字摘抄一句（不超过 40 字），必须和原文一字不差。
notice 的 title：给长辈看的一句口语化提醒，不超过 25 字，不写「今天/明天」，不照抄文章标题，数字必须原文就有；写清哪条路、哪个区域、什么时间。原文没写清具体内容的不要。
</rules>

<output>
只输出一个 JSON 对象：
{"events": [{"title": "活动名称", "venue": "场馆名称（不含地址）", "date": "YYYY-MM-DD 开始日期", "end_date": "YYYY-MM-DD 展览结束日期，没有就填空字符串", "time": "19:00 或空字符串", "price": "原文票价，没写就填空字符串", "category": "演出|展览|电影|讲座|民俗|其他", "senior_score": 1到3的整数（3最适合长辈）, "source": 文章编号}],
 "notices": [{"category": "交通|民生", "title": "一句话提醒", "date": "YYYY-MM-DD 开始或生效日期", "end_date": "只有持续一段时间的封闭、施工、管制、停水停电才填结束日期，否则空字符串", "quote": "原文逐字摘抄", "source": 文章编号}]}
没有就输出空数组。
</output>"""


def build_articles_user_content(docs: str) -> str:
    return f"[Context: Location=呼和浩特]\n文章：\n{docs}"


def article_key(a: Article) -> str:
    return hashlib.sha1((a.url or f"{a.title}|{a.published}").encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class LocalEvent:
    title: str
    venue: str
    start: date
    end: date | None  # exhibitions
    time: str
    price: str
    category: str
    senior_score: int
    fresh: bool = False  # newly announced (from the real-time layer)

    def when(self) -> str:
        """Human-friendly date, e.g. 10月17日（周六）19:00 or 展期至11月30日."""
        if self.end and self.end > self.start:
            return f"展期至{self.end.month}月{self.end.day}日"
        s = f"{self.start.month}月{self.start.day}日（周{WEEKDAYS[self.start.weekday()]}）"
        return f"{s} {self.time}".strip()

    def is_evening(self) -> bool:
        m = re.match(r"(\d{1,2}):", self.time)
        return bool(m and int(m.group(1)) >= 18)


_DATE = re.compile(r"(?:(\d{4})\s*[年.\-/])?\s*(\d{1,2})\s*[月.\-/]\s*(\d{1,2})")


def _parse_date(raw: Any, today: date | None = None) -> date | None:
    """Accept 2026-10-17, 2026年10月17日, 2026.10.17, or 10月17日 (year inferred: this year,
    or next year if that date is more than ~2 months in the past)."""
    m = _DATE.search(str(raw or ""))
    if not m:
        return None
    year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
    try:
        if year:
            return date(int(year), month, day)
        if today is None:
            return None
        guess = date(today.year, month, day)
        return date(today.year + 1, month, day) if (today - guess).days > 60 else guess
    except ValueError:
        return None


def _verified_price(price: str, text: str) -> str:
    """Keep the price only if it says 免费 and the source does, or all its numbers are in the source."""
    price = price.strip()
    if not price:
        return ""
    if "免费" in price:
        return "免费" if "免费" in text else ""
    numbers = re.findall(r"\d+", price)
    return price if numbers and all(re.search(rf"(?<!\d){n}(?!\d)", text) for n in numbers) else ""


def _date_in_text(d: date, text: str) -> bool:
    """True if month/day of ``d`` is literally written in ``text`` (10月17日, 10.17, 10-17, 10/17...)."""
    m, day = d.month, d.day
    pattern = rf"(?<!\d)0?{m}\s*[月.\-/]\s*0?{day}(?!\d)"
    return re.search(pattern, text) is not None


def _date_evidence(d: date, text: str, published: date | None, today: date, day_only: bool = False) -> bool:
    """The source supports date ``d``: written explicitly, or a post published TODAY that says
    今天 (→ today) / 明天 (→ tomorrow). ``day_only`` also accepts "12日" (same month, notices only)."""
    if _date_in_text(d, text):
        return True
    if day_only and re.search(rf"(?<![\d月]){d.day}日", text) and d.month == today.month:
        return True
    if published == today:
        if d == today and any(w in text for w in RELATIVE_TODAY):
            return True
        if d == today + timedelta(days=1) and any(w in text for w in RELATIVE_TOMORROW):
            return True
    return False


def _source_is_recent(published: date | None, today: date) -> bool:
    return _source_is_recent_days(published, today, MAX_SOURCE_AGE_DAYS)


def _source_is_recent_days(published: date | None, today: date, days: int) -> bool:
    return published is not None and 0 <= (today - published).days <= days


_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s，。、！？；：,.!?;:“”\"'‘’（）()【】\[\]《》<>…—\-~·]+")
_DURATION = re.compile(r"为期\s*(\d{1,3})\s*天|持续\s*(\d{1,3})\s*天")
MAX_ONGOING_START_AGE_DAYS = 14
_ONGOING_KINDS = re.compile(r"封闭|施工|管制|限行|改道|禁行|停水|停电|停气")
_LEADING_RELATIVE = re.compile(r"^(今天|今日|明天|明日|后天)[，,：:\s]*")


_UNITS = {"°C": "°C|℃|度", "℃": "°C|℃|度", "度": "°C|℃|度", "级": "级", "毫米": "毫米|mm", "mm": "毫米|mm"}
_NUMBER = re.compile(r"(-?\d+)\s*(°C|℃|度|级|毫米|mm)?")


def _numbers_backed(text_to_check: str, source: str) -> bool:
    """Every number in a generated title must appear in the source — with the same unit when it has
    one (the "3" of "西南风3级" does not back "最低3°C")."""
    for number, unit in _NUMBER.findall(text_to_check):
        digits = number.lstrip("-")
        if unit:
            pattern = rf"(?<!\d){digits}\s*(?:{_UNITS[unit]})"
        else:
            pattern = rf"(?<!\d){digits}(?!\d)"
        if not re.search(pattern, source):
            return False
    return True


def _norm(text: str) -> str:
    return _PUNCT.sub("", text)


def quote_in_source(quote: str, text: str) -> bool:
    """Verbatim check that ignores punctuation/spacing differences ("出门，记得" vs "出门 记得") and
    accepts a quote stitched from several sentences of the same post if every clause is verbatim."""
    src = _norm(text)
    if _norm(quote) and _norm(quote) in src:
        return True
    clauses = [_norm(c) for c in re.split(r"[，。！？；,.!?;]", quote)]
    clauses = [c for c in clauses if c]
    return bool(clauses) and all(len(c) >= 4 and c in src for c in clauses)


def clean_venue(venue: str) -> str:
    return _PAREN.sub("", venue).strip(" ：:·-")


def validate_event(raw: dict[str, Any], sources: list[str], today: date, horizon_days: int,
                   published: list[date | None] | None = None) -> LocalEvent | None:
    """Return a LocalEvent only if every claim is backed by the cited search result.

    With ``published`` (real-time layer) the source must be ≤ 3 days old, and "今天/明天" in a
    post published today also counts as date evidence."""
    try:
        idx = int(raw.get("source")) - 1
    except (TypeError, ValueError):
        return None
    if not 0 <= idx < len(sources):
        return None
    text = sources[idx]
    pub = published[idx] if published else None
    if published is not None and not _source_is_recent(pub, today):
        return None
    title = str(raw.get("title", "")).strip()
    venue = clean_venue(str(raw.get("venue", "")))
    start, end = _parse_date(raw.get("date"), today), _parse_date(raw.get("end_date"), today)
    if not (title and venue and start):
        return None
    if venue not in text:
        log.info("event dropped (venue not in source): %s @ %s", title, venue)
        return None
    if end and end >= start:  # exhibition: must still be running and its end date must be in the text
        if not (start <= today + timedelta(days=horizon_days) and end >= today and _date_in_text(end, text)):
            return None
    else:
        end = None
        evidence = _date_evidence(start, text, pub, today) if published is not None else _date_in_text(start, text)
        if not (today <= start <= today + timedelta(days=horizon_days) and evidence):
            log.info("event dropped (date %s not verifiable / out of range): %s", start, title)
            return None
    category = str(raw.get("category", "其他"))
    try:
        score = max(1, min(3, int(raw.get("senior_score", 2))))
    except (TypeError, ValueError):
        score = 2
    return LocalEvent(
        title=title[:40], venue=venue[:30], start=start, end=end,
        time=str(raw.get("time", "")).strip()[:5], price=_verified_price(str(raw.get("price", "")), text)[:20],
        category=category if category in CATEGORIES else "其他", senior_score=score,
        fresh=published is not None,
    )


@dataclass(frozen=True)
class LocalNotice:
    category: str  # 天气 | 交通 | 民生
    title: str  # one-line reminder written for the parents
    date: date
    quote: str  # verbatim evidence from the source
    end: date | None = None  # ongoing notices (e.g. a 31-day road closure)

    @property
    def audience(self) -> str:
        return NOTICE_AUDIENCE.get(self.category, "both")

    def line(self, today: date) -> str:
        if self.date < today and self.end:
            return f"{self.title}（持续到{self.end.month}月{self.end.day}日）"
        when = "今天" if self.date == today else ("明天" if self.date == today + timedelta(days=1)
                                                 else f"{self.date.month}月{self.date.day}日")
        if when in self.title or f"{self.date.month}月{self.date.day}日" in self.title:
            return self.title  # the title already says when
        return f"{when}：{self.title}"


def validate_notice(raw: dict[str, Any], sources: list[str], published: list[date | None],
                    today: date) -> LocalNotice | None:
    """Keep a notice only if it is recent, its quote is verbatim in the source, and its date is
    today/upcoming and supported by the source text."""
    try:
        idx = int(raw.get("source")) - 1
    except (TypeError, ValueError):
        return None
    if not 0 <= idx < len(sources) or not _source_is_recent(published[idx], today):
        return None
    text, pub = sources[idx], published[idx]
    category = str(raw.get("category", "")).strip()
    # The model writes "明天…" relative to the post date; we add the correct day ourselves.
    title = _LEADING_RELATIVE.sub("", str(raw.get("title", "")).strip())
    quote = _SPACE.sub("", str(raw.get("quote", "")))
    if category not in NOTICE_AUDIENCE or not 6 <= len(title) <= 30 or not 4 <= len(_norm(quote)) <= 60:
        return None  # titles under 6 chars ("降温提醒", "道路封闭") are too vague to help
    if category == "天气" and not re.search(r"\d|°|雨|雪|风|霜|雾|沙|降温|升温|寒潮|高温", title):
        return None  # a weather reminder must say what: degrees, rain, snow, wind...
    if not quote_in_source(quote, text):
        log.info("notice dropped (quote not verbatim): %s", title)
        return None
    if re.search(r"[【】]|原文", title):
        return None  # template placeholder copied instead of filled in
    if not _numbers_backed(title, text):
        log.info("notice dropped (number in title not in source): %s", title)
        return None
    d = _parse_date(raw.get("date"), today)
    end = _parse_date(raw.get("end_date"), today)
    if d is None or d > today + timedelta(days=NOTICE_HORIZON_DAYS):
        return None
    if not _date_evidence(d, text, pub, today, day_only=True):
        log.info("notice dropped (date %s not supported by source): %s", d, title)
        return None
    if d < today:  # started already: keep only an ongoing disruption that is verifiably still in effect
        if not _ONGOING_KINDS.search(title + quote):
            return None
        if end is None or end < today or (today - d).days > MAX_ONGOING_START_AGE_DAYS:
            return None
        duration = _DURATION.search(text)
        by_duration = duration and abs((d + timedelta(days=int(duration.group(1) or duration.group(2))) - end).days) <= 1
        if not (_date_in_text(end, text) or by_duration):
            log.info("notice dropped (end %s not supported by source): %s", end, title)
            return None
    elif end is not None and end < d:
        end = None
    return LocalNotice(category, title, d, quote, end)


def _notice_to_json(n: LocalNotice) -> dict[str, Any]:
    return {"category": n.category, "title": n.title, "date": n.date.isoformat(), "quote": n.quote,
            "end": n.end.isoformat() if n.end else ""}


def _notice_from_json(d: dict[str, Any]) -> LocalNotice:
    return LocalNotice(d["category"], d["title"], date.fromisoformat(d["date"]), d.get("quote", ""),
                       date.fromisoformat(d["end"]) if d.get("end") else None)


def _event_to_json(e: LocalEvent) -> dict[str, Any]:
    d = asdict(e)
    d["start"] = e.start.isoformat()
    d["end"] = e.end.isoformat() if e.end else ""
    return d


def _event_from_json(d: dict[str, Any]) -> LocalEvent:
    return LocalEvent(
        title=d["title"], venue=d["venue"], start=date.fromisoformat(d["start"]),
        end=date.fromisoformat(d["end"]) if d.get("end") else None, time=d.get("time", ""),
        price=d.get("price", ""), category=d.get("category", "其他"), senior_score=int(d.get("senior_score", 2)),
        fresh=bool(d.get("fresh", False)),
    )


def notice_lines(finder: "EventFinder | None", today: date, audience: str, limit: int = 2) -> list[str]:
    """Best-effort reminder lines for a message; never raises."""
    if finder is None:
        return []
    try:
        return [n.line(today) for n in finder.notices(today, audience)[:limit]]
    except Exception as exc:  # noqa: BLE001 — reminders are optional; the message must still go out
        log.warning("real-time notices unavailable: %s", exc)
        return []


def warning_to_notice(w: WeatherWarning, today: date) -> LocalNotice:
    """Official NMC warning → weather notice, verbatim (no LLM)."""
    detail = w.detail if len(w.detail) <= 70 else w.detail[:69] + "…"
    signal = w.signal.replace("信号", "")
    title = f"{signal}（{w.issued.month}月{w.issued.day}日{w.issued.hour}时发布）" + (f"：{detail}" if detail else "")
    return LocalNotice("天气", title, today, w.detail or w.title)


class EventFinder:
    """Events + notices for the outing / commute / chat. Thread-safe; never raises on source failures."""

    def __init__(
        self,
        llm: LLMClient | None,
        state: StateStore,
        articles: ArticleSource | None = None,
        warnings: WarningSource | None = None,
        horizon_days: int = 7,
        clock=time.time,
        events_max_age_days: int = EVENTS_MAX_AGE_DAYS,
        extract_store: StateStore | None = None,
    ):
        self._llm = llm
        self._state = state
        self._articles_src = articles
        self._warnings_src = warnings
        self._horizon = horizon_days
        self._clock = clock
        # never look further back than the article source keeps (WEMP_RETENTION_DAYS)
        self._events_max_age = max(MAX_SOURCE_AGE_DAYS, events_max_age_days)
        self._extracts = extract_store or StateStore(Path(str(state._path)).with_name("article_extracts.json"))
        self._articles_cache: tuple[float, list[Article]] | None = None
        self._warnings_cache: tuple[float, list[WeatherWarning]] | None = None
        self._lock = threading.Lock()  # one extraction at a time (scheduler warm-up vs. a parent's request)
        self.on_stats = None  # optional callback(**stats) for the activity log / daily report
        self._feed_error = False

    # ---------------------------------------------------------------- public

    def refresh(self, today: date, report: bool = False) -> int:
        """Send new, relevant articles to GLM (in batches) and store the raw results.
        Returns the number of articles processed. Safe to call often: known articles are skipped.
        ``report``: always emit stats (the scheduled warm-up); otherwise only when there was work."""
        if self._llm is None:
            return 0
        with self._lock:
            known = self._extracts.get("items") or {}
            fresh = [a for a in self._candidate_articles(today) if article_key(a) not in known]
            done = failed = 0
            for start in range(0, min(len(fresh), BATCH_SIZE * MAX_BATCHES_PER_REFRESH), BATCH_SIZE):
                batch = fresh[start:start + BATCH_SIZE]
                results = self._extract_batch(batch)
                if results is None:
                    failed += 1
                    continue  # failed batch: not cached, retried on the next refresh
                for a, (events, notices) in zip(batch, results):
                    known[article_key(a)] = {
                        "at": self._clock(), "published": a.published.isoformat() if a.published else "",
                        "title": a.title, "text": f"{a.title}\n{a.content[:ARTICLE_TEXT_CHARS]}",
                        "events": events, "notices": notices,
                    }
                done += len(batch)
            self._prune(known, today)
            self._extracts.set("items", known)
            if fresh:
                log.info("article extraction: %d new relevant articles, %d processed, %d cached",
                         len(fresh), done, len(known))
            if self.on_stats is not None and (report or fresh):
                try:
                    self.on_stats(feed_articles=len(self._articles_cache[1]) if self._articles_cache else 0,
                                  feed_error=self._feed_error, new_relevant=len(fresh), processed=done,
                                  failed_batches=failed, cached=len(known),
                                  events=len(self._validated_events(today)), notices=len(self._validated_notices(today)))
                except Exception:  # noqa: BLE001
                    log.debug("on_stats hook failed", exc_info=True)
            return done

    def find(self, today: date) -> list[LocalEvent]:
        """Upcoming verified events, today's first, then soonest; best-for-seniors first within a day."""
        self.refresh(today)
        events = self._validated_events(today)
        return sorted(events, key=lambda e: (max(e.start, today), -e.senior_score))

    def notices(self, today: date, audience: str) -> list[LocalNotice]:
        """Verified real-time reminders for ``audience`` ("father" / "mother"), soonest first."""
        self.refresh(today)
        notices = self._weather_notices(today) + self._validated_notices(today)
        self._state.set(REALTIME_CACHE_KEY, {"date": today.isoformat(), "at": self._clock(),
                                             "notices": [_notice_to_json(n) for n in notices]})
        return self._for(notices, audience)

    def cached_notices(self, today: date, audience: str) -> list[LocalNotice]:
        """Same as notices() but never hits the network (for chat context)."""
        cached = self._state.get(REALTIME_CACHE_KEY) or {}
        if cached.get("date") != today.isoformat():
            return []
        return self._for([_notice_from_json(n) for n in cached.get("notices", [])], audience)

    @staticmethod
    def _for(notices: list[LocalNotice], audience: str) -> list[LocalNotice]:
        mine = [n for n in notices if n.audience in ("both", audience)]
        # today/upcoming first, ongoing ones after; weather before utilities before traffic
        return sorted(mine, key=lambda n: (n.end is not None, n.date, NOTICE_ORDER.index(n.category)))

    # ---------------------------------------------------------------- validation (cheap, every call)

    def _cached_items(self) -> list[dict[str, Any]]:
        return list((self._extracts.get("items") or {}).values())

    def _validated_events(self, today: date) -> list[LocalEvent]:
        found: dict[tuple[str, date], LocalEvent] = {}
        for item in self._cached_items():
            published = _parse_date(item.get("published"))
            if published is None or (today - published).days > self._events_max_age:
                continue
            for raw in item.get("events") or []:
                raw = dict(raw, source=1)
                recent = _source_is_recent(published, today)
                # recent posts may say 今天/明天; older ones need an explicit date in the text
                event = validate_event(raw, [item["text"]], today, self._horizon,
                                       [published] if recent else None)
                if event is None:
                    continue
                if (today - published).days > FRESH_DAYS and event.fresh:
                    event = LocalEvent(**{**asdict(event), "fresh": False})
                found.setdefault((event.title, event.start), event)
        return list(found.values())

    def _validated_notices(self, today: date) -> list[LocalNotice]:
        notices: dict[str, LocalNotice] = {}
        for item in self._cached_items():
            published = _parse_date(item.get("published"))
            for raw in item.get("notices") or []:
                if raw.get("category") == "天气":
                    continue  # weather comes only from the official NMC feed
                if n := validate_notice(dict(raw, source=1), [item["text"]], [published], today):
                    notices.setdefault(n.quote, n)
        return list(notices.values())

    # ---------------------------------------------------------------- sources

    def _articles(self) -> list[Article]:
        if self._articles_src is None:
            return []
        if self._articles_cache and self._clock() - self._articles_cache[0] < ARTICLES_CACHE_S:
            return self._articles_cache[1]
        try:
            articles = self._articles_src.articles()
            self._feed_error = False
        except Exception as exc:  # noqa: BLE001 — a feed outage must not break messages
            log.warning("article feed unavailable: %s", exc)
            self._feed_error = True
            # remember the failure for 5 minutes so callers don't each wait on a dead feed
            self._articles_cache = (self._clock() - ARTICLES_CACHE_S + 300, [])
            return []
        self._articles_cache = (self._clock(), articles)
        return articles

    def _candidate_articles(self, today: date) -> list[Article]:
        recent = [a for a in self._articles() if _source_is_recent_days(a.published, today, self._events_max_age)
                  and RELEVANT.search(a.title + a.content)]
        return sorted(recent, key=lambda a: a.published or date.min, reverse=True)

    def _extract_batch(self, batch: list[Article]) -> list[tuple[list, list]] | None:
        docs = "\n\n".join(
            f"[{i}] 标题：{a.title}\n来源：{a.account}\n发布日期：{a.published}\n内容：{a.content[:ARTICLE_TEXT_CHARS]}"
            for i, a in enumerate(batch, start=1)
        )
        messages = [{"role": "system", "content": ARTICLE_EXTRACTION_PROMPT},
                    {"role": "user", "content": build_articles_user_content(docs)}]
        try:
            data = self._llm.chat_json(messages, max_tokens=EXTRACTION_MAX_TOKENS, temperature=0.1,
                                       timeout=EXTRACTION_TIMEOUT_S, max_attempts=EXTRACTION_ATTEMPTS,
                                       purpose="extract")
        except LLMError as exc:
            log.warning("article extraction failed for a batch of %d: %s", len(batch), exc)
            return None
        per_article: list[tuple[list, list]] = [([], []) for _ in batch]
        for kind, slot in (("events", 0), ("notices", 1)):
            for raw in data.get(kind) or []:
                try:
                    idx = int(raw.get("source")) - 1
                except (TypeError, ValueError, AttributeError):
                    continue
                if isinstance(raw, dict) and 0 <= idx < len(batch):
                    per_article[idx][slot].append({k: v for k, v in raw.items() if k != "source"})
        return per_article

    def _prune(self, known: dict[str, dict], today: date) -> None:
        cutoff = today - timedelta(days=self._events_max_age + 1)
        for key in [k for k, v in known.items() if (_parse_date(v.get("published")) or date.min) < cutoff]:
            del known[key]

    def _weather_notices(self, today: date) -> list[LocalNotice]:
        if self._warnings_src is None:
            return []
        if self._warnings_cache and self._clock() - self._warnings_cache[0] < WARNINGS_CACHE_S:
            warnings = self._warnings_cache[1]
        else:
            try:
                warnings = self._warnings_src.warnings()
            except Exception as exc:  # noqa: BLE001
                log.warning("NMC warnings unavailable: %s", exc)
                warnings = []
            self._warnings_cache = (self._clock(), warnings)
        now = datetime.combine(today, datetime.max.time())
        newest: dict[str, WeatherWarning] = {}
        for w in sorted(warnings, key=lambda w: w.issued):
            kind = re.sub(r"[红橙黄蓝]色", "", w.signal)  # 大风预警信号, 寒潮预警信号…
            if (now - w.issued).total_seconds() > WARNING_MAX_AGE_HOURS * 3600:
                continue
            if w.lifted:
                newest.pop(kind, None)  # a later cancellation removes the warning
            else:
                newest[kind] = w
        return [warning_to_notice(w, today) for w in newest.values()]
