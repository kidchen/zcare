"""Feature 3 — drama picks for Mom & Dad, watched on 奇异果TV (iQiyi TV app, Dad has VIP).

Each recommendation mixes two pools (default 2 + 1):
  * New hits — Douban's 热门 国产剧 list (currently airing / just aired), aired this year or last.
  * Classics — Douban 选影视 genre lists (历史 / 家庭 / 战争 / 悬疑 / 犯罪 / 戏曲, mainland China,
    sorted by score), a random 3 genres per run.
Quality gate: rating ≥ TV_MIN_RATING (7.5), or — for new shows — rating ≥ 7.0 with ≥ 20 000
votes (strong current popularity). Unrated shows are skipped. Only clearly unsuitable genres are
excluded (animation, horror, reality/talk shows, kids'); modern dramas, suspense and light sci-fi
are welcome. Picks are spread across genres; dramas on iQiyi rank higher.
Details (genres / synopsis / where to watch) come from the mobile JSON API
(m.douban.com/rexxar), because desktop detail pages now redirect scripts to a
sec.douban.com anti-bot check. The HTML page is kept as a fallback.
Requests are throttled; set DOUBAN_COOKIE (``bid=...``) if you start getting 403/418.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import random
import re
import time
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

import requests
from bs4 import BeautifulSoup
from pypinyin import Style, lazy_pinyin

from clients.http import DESKTOP_UA, build_session
from config import Settings
from messages import Article, Message
from storage import StateStore

log = logging.getLogger(__name__)

SEARCH_URL = "https://movie.douban.com/j/search_subjects"
GENRE_SEARCH_URL = "https://movie.douban.com/j/new_search_subjects"
DETAIL_URL = "https://movie.douban.com/subject/{id}/"
MOBILE_PAGE_URL = "https://m.douban.com/movie/subject/{id}/"  # what a tap opens on the phone
MOBILE_DETAIL_URL = "https://m.douban.com/rexxar/api/v2/tv/{id}"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
STATE_KEY = "tv_recent_ids"
RECENT_KEEP = 60  # every drama shown (scheduled or on request) is remembered; ~20 recommendations
POOL_SIZE = 30  # sample from the top-N best-rated not-yet-shown candidates → variety, still high quality
MAX_DETAIL_FETCHES = 10
PAGE_STARTS = (0, 20)  # each run reads a random page per genre for variety
CLASSIC_GENRES_PER_RUN = 3  # genre lists fetched per run (randomly chosen) — keeps Douban requests low
POPULAR_MIN_RATING = 7.0  # new shows may pass below TV_MIN_RATING if they are clearly popular...
POPULAR_MIN_VOTES = 20_000  # ...with at least this many Douban ratings
NEW_MAX_AGE_YEARS = 1  # aired this year or last year = "new"
SUMMARY_CHARS = 70


@dataclass(frozen=True)
class Drama:
    subject_id: str
    title: str
    rating: float
    url: str
    year: str = ""
    genres: tuple[str, ...] = ()
    summary: str = ""
    platforms: tuple[str, ...] = ()
    poster: str = ""  # Douban image URL (hotlink-protected: needs a douban Referer)
    votes: int = 0  # number of Douban ratings (popularity)


def _to_float(raw: str | None) -> float:
    try:
        return float(raw or 0)
    except ValueError:
        return 0.0


def parse_search_subjects(data: dict) -> list[Drama]:
    return [
        Drama(str(s["id"]), s.get("title", ""), _to_float(s.get("rate")), s.get("url", ""),
              poster=s.get("cover", "") or "")
        for s in data.get("subjects", [])
        if s.get("id")
    ]


def parse_detail_html(html: str, base: Drama) -> Drama:
    soup = BeautifulSoup(html, "html.parser")
    genres = tuple(el.get_text(strip=True) for el in soup.select('span[property="v:genre"]'))
    summary_el = soup.select_one("span.all.hidden") or soup.select_one('span[property="v:summary"]')
    summary = " ".join(summary_el.get_text().split()) if summary_el else ""
    rating_el = soup.select_one('strong[property="v:average"]')
    rating = _to_float(rating_el.get_text(strip=True)) if rating_el else 0.0
    year_el = soup.select_one("span.year")
    year = year_el.get_text(strip=True).strip("()（）") if year_el else ""
    votes_el = soup.select_one('span[property="v:votes"]')
    votes = int(_to_float(votes_el.get_text(strip=True))) if votes_el else 0
    poster_el = soup.select_one('img[rel="v:image"]')
    poster = (poster_el.get("src") or "") if poster_el else ""
    platforms = tuple(dict.fromkeys(
        (a.get("data-cn") or a.get_text(strip=True)).strip()
        for a in soup.select("a.playBtn")
        if (a.get("data-cn") or a.get_text(strip=True)).strip()
    ))
    return replace(
        base,
        genres=genres or base.genres,
        summary=summary or base.summary,
        rating=rating or base.rating,
        year=year or base.year,
        platforms=platforms or base.platforms,
        poster=poster or base.poster,
        votes=votes or base.votes,
    )


def parse_mobile_detail(data: dict, base: Drama) -> Drama:
    """Parse m.douban.com/rexxar/api/v2/tv/{id} JSON."""
    rating = (data.get("rating") or {}).get("value")
    votes = (data.get("rating") or {}).get("count") or 0
    platforms = []
    for vendor in data.get("vendors") or []:
        title = (vendor.get("title") or "").strip()
        if not title or vendor.get("accessible") is False:
            continue
        pay = (vendor.get("payment_desc") or "").strip()
        platforms.append(f"{title}（{pay}）" if pay else title)
    return replace(
        base,
        title=data.get("title") or base.title,
        genres=tuple(data.get("genres") or ()) or base.genres,
        summary=" ".join((data.get("intro") or "").split()) or base.summary,
        rating=_to_float(str(rating)) if rating else base.rating,
        year=str(data.get("year") or base.year),
        platforms=tuple(dict.fromkeys(platforms)) or base.platforms,
        poster=((data.get("pic") or {}).get("large") or data.get("cover_url") or base.poster),
        votes=int(votes) or base.votes,
    )


class DoubanTvSource:
    def __init__(
        self,
        session: requests.Session | None = None,
        cookie: str = "",
        delay_s: float = 1.5,
        timeout: float = 10.0,
    ):
        self._session = session or build_session(DESKTOP_UA, retries=2)
        self._session.headers.setdefault("Referer", "https://movie.douban.com/tv/")
        if cookie:
            self._session.headers["Cookie"] = cookie
        self._delay_s = delay_s
        self._timeout = timeout

    def list_by_genre(self, genre: str, min_rating: float = 7.5, start: int = 0) -> list[Drama]:
        """Douban 选影视: 电视剧 + genre, mainland China, sorted by score."""
        resp = self._session.get(
            GENRE_SEARCH_URL,
            params={"sort": "S", "range": f"{min_rating:g},10", "tags": f"电视剧,{genre}",
                    "start": start, "countries": "中国大陆"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return [
            Drama(str(s["id"]), s.get("title", ""), _to_float(s.get("rate")), s.get("url", ""),
                  genres=(genre,), poster=s.get("cover", "") or "")
            for s in resp.json().get("data", []) if s.get("id")
        ]

    def list_candidates(self, tag: str, sort: str = "recommend", limit: int = 100) -> list[Drama]:
        """sort: recommend (热度) | time (最新) | rank (评分)."""
        resp = self._session.get(
            SEARCH_URL,
            params={"type": "tv", "tag": tag, "sort": sort, "page_limit": limit, "page_start": 0},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return parse_search_subjects(resp.json())

    def enrich(self, drama: Drama) -> Drama:
        time.sleep(self._delay_s)  # be polite; Douban bans bursts
        try:
            resp = self._session.get(
                MOBILE_DETAIL_URL.format(id=drama.subject_id),
                headers={"User-Agent": MOBILE_UA, "Referer": "https://m.douban.com/tv/"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return parse_mobile_detail(resp.json(), drama)
        except (requests.RequestException, ValueError) as exc:
            log.info("mobile detail failed for %s (%s); trying HTML page", drama.title, exc)
        resp = self._session.get(DETAIL_URL.format(id=drama.subject_id), timeout=self._timeout)
        resp.raise_for_status()
        if "sec.douban.com" in resp.url:
            raise requests.HTTPError(f"Douban anti-bot redirect for {drama.title}")
        return parse_detail_html(resp.text, drama)


def is_new(drama: Drama, this_year: int) -> bool:
    try:
        return int(drama.year[:4]) >= this_year - NEW_MAX_AGE_YEARS
    except ValueError:
        return False


def passes_quality(drama: Drama, min_rating: float, this_year: int) -> bool:
    """Rating ≥ min_rating, or a new show that is clearly popular and still well rated."""
    if drama.rating >= min_rating:
        return True
    return (is_new(drama, this_year) and drama.rating >= POPULAR_MIN_RATING
            and drama.votes >= POPULAR_MIN_VOTES)


def suitable(drama: Drama, excluded_genres: Iterable[str]) -> bool:
    """Only clearly unsuitable content is excluded (default: animation, horror, reality, kids')."""
    return not set(drama.genres) & set(excluded_genres)


def primary_genre(drama: Drama) -> str:
    return next((g for g in drama.genres if g != "剧情"), drama.genres[0] if drama.genres else "")


def score(drama: Drama, preferred_genres: tuple[str, ...], preferred_platform: str = "") -> float:
    """Rating first; popularity, liked genres and availability on Dad's iQiyi VIP as tie-breakers."""
    matches = len(set(drama.genres) & set(preferred_genres))
    popularity = 0.25 * math.log10(drama.votes) if drama.votes > 1 else 0.0  # 10k → 1.0, 100k → 1.25
    return (drama.rating + popularity + 0.3 * min(matches, 2)
            + (1.0 if on_platform(drama, preferred_platform) else 0.0))


def search_initials(title: str) -> str:
    """Pinyin initials for TV-remote search (大明王朝1566 → DMWC; handles 长安/重庆 polyphones)."""
    return "".join(s[0] for s in lazy_pinyin(title, style=Style.FIRST_LETTER) if s and s[0].isalpha()).upper()


def watch_note(drama: Drama, platform: str, app: str) -> str:
    """Where/how Dad watches it, nudging toward the TV app he has VIP for."""
    if on_platform(drama, platform):
        entry = next(p for p in drama.platforms if platform in p)
        pay = entry[entry.index("（") + 1:-1] if "（" in entry and entry.endswith("）") else ""
        initials = search_initials(drama.title)
        how = f"搜「{initials}」" if initials else f"搜「{drama.title}」"
        return f"在电视上打开「{app}」，{how}就能找到{f'（{pay}）' if pay else ''}，大屏看更舒服"
    if drama.platforms:
        return f"{app}上暂时没有，可以在{'、'.join(drama.platforms[:2])}看"
    return f"可以在「{app}」里搜搜看"


def on_platform(drama: Drama, platform: str) -> bool:
    return bool(platform) and any(platform in p for p in drama.platforms)


def gather_trending(source: DoubanTvSource, tag: str) -> list[Drama]:
    """Douban 热门 list for ``tag`` (currently airing). Unrated shows have no quality signal yet."""
    return [d for d in source.list_candidates(tag, sort="recommend", limit=50) if d.rating >= POPULAR_MIN_RATING]


def gather_classics(
    source: DoubanTvSource, genres: Iterable[str], min_rating: float, rng: random.Random
) -> list[Drama]:
    """A random few genre lists, de-duplicated."""
    genres = list(genres)
    found: dict[str, Drama] = {}
    for genre in rng.sample(genres, min(CLASSIC_GENRES_PER_RUN, len(genres))):
        try:
            for drama in source.list_by_genre(genre, min_rating, start=rng.choice(PAGE_STARTS)):
                found.setdefault(drama.subject_id, drama)
        except (requests.RequestException, ValueError) as exc:
            log.warning("Douban %s list failed: %s", genre, exc)
    return list(found.values())


def _select(
    source: DoubanTvSource,
    candidates: list[Drama],
    n: int,
    recent: list[str],
    keep: Callable[[Drama], bool],
    rank: Callable[[Drama], float],
    taken_genres: set[str],
    rng: random.Random,
    allow_reuse: bool = False,
    never: frozenset[str] = frozenset(),
) -> list[Drama]:
    """Choose ``n`` not-recently-shown dramas from ``candidates``, spread across genres.

    ``allow_reuse``: if nothing fresh is left, let the oldest-shown half of ``recent`` come back
    (only used when the recommendation would otherwise be empty). ``never``: ids that must not be
    picked in any case (e.g. already chosen in this recommendation).
    """
    if n <= 0 or not candidates:
        return []
    candidates = [d for d in candidates if d.subject_id not in never]
    fresh = [d for d in candidates if d.subject_id not in set(recent)]
    if not fresh and allow_reuse:
        still_recent = set(recent[len(recent) // 2:])
        fresh = [d for d in candidates if d.subject_id not in still_recent] or candidates
        log.info("drama pool exhausted; re-allowing %d older picks", len(fresh))
    if not fresh:
        return []
    fresh.sort(key=lambda d: d.rating, reverse=True)
    sampled = rng.sample(fresh[:POOL_SIZE], min(MAX_DETAIL_FETCHES, len(fresh[:POOL_SIZE])))
    good: list[Drama] = []
    for drama in sampled:
        if len(good) >= n + 2:  # enough choice for ranking; saves Douban requests
            break
        try:
            drama = source.enrich(drama)
        except requests.RequestException as exc:
            log.warning("detail fetch failed for %s: %s", drama.title, exc)
        if keep(drama):
            good.append(drama)
        else:
            log.info("skipping %s (%.1f, %d votes, %s, %s)", drama.title, drama.rating, drama.votes,
                     drama.year or "?", "/".join(drama.genres))
    good.sort(key=rank, reverse=True)
    chosen: list[Drama] = []
    for d in good:  # best first, but skip a genre already in this recommendation
        if len(chosen) < n and primary_genre(d) not in taken_genres:
            chosen.append(d)
            taken_genres.add(primary_genre(d))
    for d in good:  # not enough variety available: fill with the best remaining
        if len(chosen) < n and d not in chosen:
            chosen.append(d)
    return chosen


def recommend(
    source: DoubanTvSource,
    classic_genres: Iterable[str],
    preferred_genres: tuple[str, ...],
    min_rating: float,
    picks: int,
    exclude_ids: Iterable[str],
    excluded_genres: Iterable[str] = (),
    preferred_platform: str = "",
    new_picks: int = 2,
    trending_tag: str = "国产剧",
    this_year: int | None = None,
    rng: random.Random | None = None,
) -> list[Drama]:
    """``new_picks`` trending new hits + the rest high-scoring classics (shortfalls are filled
    from the other pool). ``exclude_ids`` is ordered oldest → newest."""
    rng = rng or random.Random()
    this_year = this_year or date.today().year
    recent = list(exclude_ids)
    excluded = tuple(excluded_genres)
    new_target = max(0, min(new_picks, picks))
    rank = lambda d: score(d, preferred_genres, preferred_platform)  # noqa: E731
    taken: set[str] = set()

    try:
        trending = gather_trending(source, trending_tag)
    except (requests.RequestException, ValueError) as exc:
        log.warning("Douban trending list failed: %s", exc)
        trending = []
    new_hits = _select(
        source, trending, new_target, recent,
        keep=lambda d: suitable(d, excluded) and is_new(d, this_year) and passes_quality(d, min_rating, this_year),
        rank=rank, taken_genres=taken, rng=rng,
    )
    shown = recent + [d.subject_id for d in new_hits]
    classics_pool = [d for d in gather_classics(source, classic_genres, min_rating, rng) if d.rating >= min_rating]
    if not trending and not classics_pool:
        raise requests.HTTPError("no Douban candidates (all lists failed or empty)")
    classics = _select(
        source, classics_pool, picks - len(new_hits), shown,
        keep=lambda d: suitable(d, excluded) and passes_quality(d, min_rating, this_year),
        rank=rank, taken_genres=taken, rng=rng,
        allow_reuse=not new_hits, never=frozenset(d.subject_id for d in new_hits),
    )
    return new_hits + classics


def _short(text: str, limit: int = SUMMARY_CHARS) -> str:
    return text if len(text) <= limit else text[:limit].rstrip("，。、 ") + "…"


_SUBJECT_ID = re.compile(r"^(?:\d{1,12}|wx[0-9a-f]{16})$")  # Douban subject id | WeChat cover
POSTER_MAX_BYTES = 2 * 1024 * 1024


class PosterCache:
    """Serve Douban posters from our own server so WeCom ``picurl`` is a reliable public URL.

    Douban images return 418 without a douban Referer, and phones don't send one, so linking
    them directly gives broken pictures. We download once (with the Referer) into the data
    volume and expose them at ``{public_base_url}/posters/{id}.jpg``.
    """

    def __init__(self, directory: Path, public_base_url: str, session: requests.Session | None = None,
                 max_files: int = 200, timeout: float = 10.0):
        self.directory = Path(directory)
        self._base = public_base_url.rstrip("/")
        self._session = session or build_session(DESKTOP_UA, retries=1)
        self._max_files = max_files
        self._timeout = timeout

    def path_for(self, subject_id: str) -> Path | None:
        if not _SUBJECT_ID.match(subject_id):
            return None
        return self.directory / f"{subject_id}.jpg"

    def picurl(self, drama: Drama) -> str:
        """Public URL for the drama's poster, or "" if unavailable (card shows without a picture)."""
        return self.cache(drama.subject_id, drama.poster, {"Referer": "https://movie.douban.com/"}, drama.title)

    def article_cover(self, image_url: str) -> str:
        """Public URL for a WeChat article cover. mmbiz.qpic.cn shows a "not allowed to be quoted"
        placeholder when loaded with a foreign Referer, so we fetch it without one and serve our copy."""
        if not image_url.startswith(("https://mmbiz.qpic.cn/", "http://mmbiz.qpic.cn/")):
            return ""
        key = "wx" + hashlib.sha1(image_url.encode()).hexdigest()[:16]
        return self.cache(key, image_url, {}, image_url)

    def cache(self, key: str, source: str, headers: dict[str, str], label: str) -> str:
        path = self.path_for(key)
        if not (self._base and source and path):
            return ""
        if not path.exists():
            try:
                resp = self._session.get(source, timeout=self._timeout, headers=headers)
                resp.raise_for_status()
                ctype = resp.headers.get("Content-Type", "")
                if not ctype.startswith("image/") or not 0 < len(resp.content) <= POSTER_MAX_BYTES:
                    raise ValueError(f"not a usable image ({ctype}, {len(resp.content)} bytes)")
                self.directory.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(resp.content)
                os.replace(tmp, path)
                self._prune()
            except (requests.RequestException, ValueError, OSError) as exc:
                log.warning("image unavailable for %s: %s", label, exc)
                return ""
        return f"{self._base}/posters/{key}.jpg"

    def _prune(self) -> None:
        files = sorted(self.directory.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - self._max_files)]:
            old.unlink(missing_ok=True)


def drama_article(drama: Drama, index: int, picurl: str = "", note: str = "", new: bool = False) -> Article:
    meta = []
    if drama.genres:
        meta.append(" / ".join(drama.genres[:3]))
    if drama.year:
        meta.append(drama.year)
    lines = [" · ".join(meta)] if meta else []
    if note:
        lines.append(f"📺 {note}")
    elif drama.platforms:
        lines.append(f"在哪看：{'、'.join(drama.platforms[:2])}")
    if drama.summary:
        lines.append(_short(drama.summary, 80))
    return Article(
        title=f"{index}.{'🔥新剧 ' if new else ''}《{drama.title}》 ⭐{drama.rating:.1f}",
        description="\n".join(lines),
        url=MOBILE_PAGE_URL.format(id=drama.subject_id),
        picurl=picurl,
    )


def format_tv_message(
    dramas: list[Drama],
    name: str,
    picurl_for: Callable[[Drama], str] = lambda d: "",
    platform: str = "爱奇艺",
    app: str = "奇异果TV",
    this_year: int | None = None,
) -> Message:
    this_year = this_year or date.today().year
    if not dramas:
        md = f"**周末追剧推荐 📺**\n\n{name}，这周没找到新的高分剧，可以重温一下喜欢的老剧～"
        return Message(title="周末追剧推荐", markdown=md)
    lines = [f"**{name}，给您挑了几部好剧 📺**", ""]
    for i, d in enumerate(dramas, start=1):
        meta = [f"⭐ {d.rating:.1f}"]
        if d.genres:
            meta.append(" / ".join(d.genres[:3]))
        if d.year:
            meta.append(d.year)
        tag = "🔥新剧 " if is_new(d, this_year) else ""
        lines.append(f"**{i}. {tag}《{d.title}》** {' ｜ '.join(meta)}")
        if d.summary:
            lines.append(f"> {_short(d.summary)}")
        lines.append(f"> 📺 {watch_note(d, platform, app)}")
        lines.append("")
    lines.append(f"有正在热播的新剧，也有口碑好的老剧。{app}用遥控器搜剧名的首字母最方便。看剧别太晚，注意休息眼睛～")
    return Message(
        title=f"{name}，追剧推荐 · 《{dramas[0].title}》等{len(dramas)}部",
        markdown="\n".join(lines).strip(),
        url=MOBILE_PAGE_URL.format(id=dramas[0].subject_id),
        articles=tuple(drama_article(d, i, picurl_for(d), watch_note(d, platform, app), is_new(d, this_year))
                       for i, d in enumerate(dramas, start=1)),
    )


def build_tv_message(
    settings: Settings,
    state: StateStore,
    source: DoubanTvSource | None = None,
    record: bool = True,
    name: str | None = None,
    posters: PosterCache | None = None,
) -> Message:
    source = source or DoubanTvSource(cookie=settings.douban_cookie)
    dramas = recommend(
        source,
        classic_genres=settings.tv_classic_genres,
        preferred_genres=settings.tv_preferred_genres,
        new_picks=settings.tv_new_picks,
        trending_tag=settings.tv_trending_tag,
        excluded_genres=settings.tv_excluded_genres,
        preferred_platform=settings.tv_preferred_platform,
        min_rating=settings.tv_min_rating,
        picks=settings.tv_picks,
        exclude_ids=state.recent(STATE_KEY),
    )
    if record and dramas:
        state.remember(STATE_KEY, [d.subject_id for d in dramas], keep=RECENT_KEEP)
    names = name if name is not None else "、".join(
        settings.member(r).display_name for r in settings.tv_recipients
    )
    return format_tv_message(dramas, names, posters.picurl if posters else (lambda d: ""),
                             platform=settings.tv_preferred_platform, app=settings.tv_app_name)
