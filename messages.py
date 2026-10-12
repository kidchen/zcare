"""Channel-neutral message model shared by features and the WeCom client."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_QUOTE = re.compile(r"^>\s?", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def strip_html(text: str) -> str:
    """Remove any HTML/XML tags (e.g. WeCom <font>, stray <div>) — they render literally in WeChat."""
    return _HTML_TAG.sub("", text)


def markdown_to_text(markdown: str) -> str:
    """Strip the WeCom markdown subset down to plain text (for msgtype=text)."""
    text = strip_html(markdown)
    text = _BOLD.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _LINK.sub(r"\1：\2", text)
    return text.strip()


@dataclass(frozen=True)
class Article:
    """One item of a WeCom ``news`` card. Plain text only — no HTML, no markdown."""

    title: str
    description: str
    url: str  # where a tap goes (http/https)
    picurl: str = ""  # public JPG/PNG; optional


@dataclass(frozen=True)
class Message:
    title: str
    markdown: str
    url: str | None = None  # used by textcard / "open in AMap" links
    articles: tuple[Article, ...] = ()  # one per item (e.g. per drama) for news cards

    @property
    def text(self) -> str:
        return markdown_to_text(self.markdown)


def add_notices(message: Message, notes: list[str], heading: str = "📢 今日提醒") -> Message:
    """Insert short real-time reminders: a markdown section (before the trailing link, if any)
    and the first one at the top of the first card. No-op when ``notes`` is empty."""
    from dataclasses import replace

    if not notes:
        return message
    lines = message.markdown.split("\n")
    at = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("[")), len(lines))
    section = ["", f"**{heading}**", *[f"- {n}" for n in notes]]
    markdown = "\n".join(lines[:at] + section + ([""] if at < len(lines) else []) + lines[at:]).strip()
    articles = message.articles
    if articles:
        first = replace(articles[0], description=f"📢 {notes[0]}\n{articles[0].description}")
        articles = (first, *articles[1:])
    return replace(message, markdown=markdown, articles=articles)


def amap_nav_url(origin: str, origin_name: str, dest: str, dest_name: str, mode: str) -> str:
    """AMap URI API deep link (opens the AMap app on phones). mode: car | bus | walk."""
    return (
        "https://uri.amap.com/navigation"
        f"?from={origin},{quote(origin_name)}&to={dest},{quote(dest_name)}"
        f"&mode={mode}&coordinate=gaode&callnative=1&src=zcare"
    )
