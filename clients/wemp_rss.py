"""WeChat Official Account articles via self-hosted we-mp-rss (github.com/rachelos/we-mp-rss, MIT).

we-mp-rss logs into YOUR Official Account backend (mp.weixin.qq.com, QR scan) and lists the
articles of accounts you subscribe to; it talks only to Tencent servers (no third-party relay).

JSON feed (apis/rss.py → core/rss.py generate_json), no auth required:
    GET {base}/feed/all.json?limit=100          every subscribed account, newest first
    GET {base}/feed/{mp_id}.json?limit=100      one account
    → {"name", "items": [{"id", "title", "description", "link", "updated": "2026-10-11T08:30:00+08:00",
                           "content": "<html…>", "channel_name": "新闻天天看", "feed": {...}}]}
``limit`` is capped by the server's RSS_PAGE_SIZE (we set it to 100 in docker-compose).
Full text requires GATHER.CONTENT=True on the server (also set in docker-compose).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from clients.http import build_session

log = logging.getLogger(__name__)
CHINA_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class Article:
    title: str
    content: str  # plain text (HTML stripped); falls back to the description
    published: date | None  # Beijing date
    url: str
    account: str = ""
    image: str = ""  # cover image (mmbiz.qpic.cn); only the RSS feed carries it


def _parse_iso(raw: str) -> date | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CHINA_TZ)  # we-mp-rss emits Beijing time
    return dt.astimezone(CHINA_TZ).date()


_SHORT_LINK = re.compile(r"^(https?://mp\.weixin\.qq\.com/s/)([^?#]+)(.*)$")


def fix_link(url: str) -> str:
    """we-mp-rss's WeRead mode (before its 2026-09-23 fix) wrote '_' as '~' in short-link tokens
    (/s/ySsAk3lUFT0Wh~9IZCtr-g); WeChat answers those with 「参数错误」. Stored links keep the bug."""
    m = _SHORT_LINK.match(url.strip())
    if not m:
        return url.strip()
    return m.group(1) + re.sub(r"~|%7[Ee]", "_", m.group(2)) + m.group(3)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    return " ".join(BeautifulSoup(html, "html.parser").get_text(" ").split())


def parse_feed(data: dict) -> list[Article]:
    articles = []
    for item in data.get("items") or []:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        title = str(item["title"]).strip()
        content = html_to_text(str(item.get("content") or ""))
        description = html_to_text(str(item.get("description") or ""))
        feed = item.get("feed") if isinstance(item.get("feed"), dict) else {}
        articles.append(Article(
            title=title,
            content=content or (description if description != title else ""),
            published=_parse_iso(str(item.get("updated") or "")),
            url=fix_link(str(item.get("link") or "")),
            account=str(item.get("channel_name") or feed.get("name") or ""),
        ))
    return articles


class WeMpRssClient:
    def __init__(self, base_url: str, feed_ids: Iterable[str] = (), session: requests.Session | None = None,
                 timeout: float = 30.0, limit: int = 100):
        self._base = base_url.rstrip("/")
        self._feeds = tuple(f for f in feed_ids if f) or ("all",)
        self._session = session or build_session(retries=1)
        self._timeout = timeout
        self._limit = limit

    def articles(self) -> list[Article]:
        """Recent articles from the configured feeds (all subscribed accounts by default)."""
        found: dict[str, Article] = {}
        errors: list[Exception] = []
        for feed in self._feeds:
            try:
                resp = self._session.get(f"{self._base}/feed/{feed}.json", params={"limit": self._limit},
                                         timeout=self._timeout)
                resp.raise_for_status()
                for a in parse_feed(resp.json()):
                    found.setdefault(a.url or a.title, a)
            except (requests.RequestException, ValueError) as exc:
                log.warning("we-mp-rss feed %s failed: %s", feed, exc)
                errors.append(exc)
        if not found and errors:
            raise errors[-1]
        covers = self._covers()
        return [replace(a, image=covers.get(a.url, "")) for a in found.values()]

    def _covers(self) -> dict[str, str]:
        """link → cover image from the RSS feeds' <enclosure> (the JSON feed omits it). Best effort."""
        covers: dict[str, str] = {}
        for feed in self._feeds:
            try:
                resp = self._session.get(f"{self._base}/feed/{feed}.rss", params={"limit": self._limit},
                                         timeout=self._timeout)
                resp.raise_for_status()
                covers.update(parse_rss_covers(resp.content))
            except (requests.RequestException, ElementTree.ParseError) as exc:
                log.info("we-mp-rss covers for %s unavailable: %s", feed, exc)
        return covers


def parse_rss_covers(xml: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in ElementTree.fromstring(xml).iter("item"):
        link, enc = fix_link(item.findtext("link") or ""), item.find("enclosure")
        url = (enc.get("url") or "").strip() if enc is not None else ""
        if link and url.startswith("http") and (enc.get("type") or "image/").startswith("image/"):
            out[link] = url
    return out
