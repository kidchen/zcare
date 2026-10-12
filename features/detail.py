"""Senior-friendly H5 detail pages for news cards.

A WeCom news card can only show ~512 bytes and a tap goes straight to its URL. Instead of linking
the card to AMap (which hides the rest of the message), we store the full message and link the
card to /detail/{id}: large text, high contrast, and a big「📍 打开高德地图导航」button at the bottom.

Privacy: pages contain the home location (inside the AMap link) and port 8000 is public, so ids are
random 128-bit tokens (no listing, no sequential ids), pages expire after DETAIL_TTL_DAYS, and the
page sends no Referer and asks not to be indexed.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import secrets
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from messages import Message

log = logging.getLogger(__name__)

DETAIL_TTL_DAYS = 14
MAX_PAGES = 500
_ID = re.compile(r"^[A-Za-z0-9_-]{20,40}$")
NAV_HOSTS = ("https://uri.amap.com/",)  # card links that get a detail page instead


# ------------------------------------------------------------------ rendering

_FONT_RAW = re.compile(r"</?font\b[^>]*>", re.I)  # WeCom markdown colour tags — removed before escaping
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_LINE_LINK = re.compile(r"^\[([^\]]+)\]\((https?://[^)\s]+)\)$")
_NUMBERED = re.compile(r"^(\d+)\.\s+(.*)$")
_ITEM_HEADING = re.compile(r"^\*\*(\d+)\.\s*(.+?)\*\*")

PAGE_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #FAF7F0; color: #2F2F2F;
       font: 21px/1.75 -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
       -webkit-text-size-adjust: 100%; }
html, body { overflow-x: hidden; }
main { max-width: 680px; margin: 0 auto; padding: 22px 20px 120px; overflow-wrap: anywhere; word-break: break-word; }
h1 { font-size: 27px; line-height: 1.4; margin: 6px 0 18px; color: #1F1F1F; }
h2 { font-size: 23px; line-height: 1.45; margin: 28px 0 10px; color: #1F1F1F; }
p { margin: 10px 0; }
.note { background: #FFFFFF; border-left: 6px solid #E0A458; border-radius: 10px; padding: 12px 16px; }
ol, ul { padding-left: 1.3em; margin: 10px 0; }
li { margin: 10px 0; }
strong { color: #1F1F1F; }
a { color: #0B57D0; }
.btn { display: block; margin: 18px 0; padding: 18px 16px; border-radius: 16px; background: #0B57D0;
       color: #FFFFFF; font-size: 23px; font-weight: 600; text-align: center; text-decoration: none; }
.btn:active { background: #0842A0; }
footer { margin-top: 30px; color: #6B6B6B; font-size: 17px; }
"""


def _inline(escaped: str) -> str:
    text = _BOLD.sub(r"<strong>\1</strong>", escaped)
    return _LINK.sub(r'<a href="\2" rel="noreferrer">\1</a>', text)


def markdown_to_html(markdown: str) -> str:
    """Render the small markdown subset our messages use. Everything is escaped first."""
    out: list[str] = []
    list_tag: str | None = None
    first_heading = True

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in markdown.splitlines():
        line = html.escape(_FONT_RAW.sub("", raw).strip(), quote=True)
        if not line:
            close_list()
            continue
        if m := _LINE_LINK.match(line):
            close_list()
            out.append(f'<a class="btn" href="{m.group(2)}" rel="noreferrer">{m.group(1)}</a>')
            continue
        if line.startswith("- "):
            if list_tag != "ul":
                close_list(); out.append("<ul>"); list_tag = "ul"
            out.append(f"<li>{_inline(line[2:])}</li>")
            continue
        if m := _NUMBERED.match(line):
            if list_tag != "ol":
                close_list(); out.append("<ol>"); list_tag = "ol"
            out.append(f"<li>{_inline(m.group(2))}</li>")
            continue
        close_list()
        if line.startswith("&gt; ") or line == "&gt;":
            out.append(f'<p class="note">{_inline(line[5:])}</p>')
        elif m := _ITEM_HEADING.match(line):
            out.append(f'<h2 id="item-{m.group(1)}">{_inline(line)}</h2>')
        elif line.startswith("**") and line.endswith("**") and line.count("**") == 2:
            tag = "h1" if first_heading else "h2"
            first_heading = False
            out.append(f"<{tag}>{_inline(line)}</{tag}>")
        else:
            out.append(f"<p>{_inline(line)}</p>")
    close_list()
    return "\n".join(out)


def render_page(title: str, markdown: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(title)}</title>
<style>{PAGE_CSS}</style>
</head><body><main>
{markdown_to_html(markdown)}
<footer>家里的小助手 · 信息仅供参考，出门前以实际情况为准</footer>
</main></body></html>"""


# ------------------------------------------------------------------ storage


class DetailStore:
    def __init__(self, directory: Path, public_base_url: str, clock: Callable[[], float] = time.time):
        self.directory = Path(directory)
        self._base = public_base_url.rstrip("/")
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return bool(self._base)

    def _path(self, report_id: str) -> Path | None:
        return self.directory / f"{report_id}.json" if _ID.match(report_id) else None

    def publish(self, message: Message) -> Message:
        """Store ``message`` and point its AMap-linked cards at the detail page.

        Messages without AMap-linked cards (e.g. drama picks → Douban) are returned unchanged."""
        if not self.enabled or not message.articles:
            return message
        if not any(a.url.startswith(NAV_HOSTS) for a in message.articles):
            return message
        report_id = secrets.token_urlsafe(16)  # 22 chars, 128 bits
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(report_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"title": message.title, "markdown": message.markdown,
                                   "created": self._clock()}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        self._prune()
        page = f"{self._base}/detail/{report_id}"
        many = len(message.articles) > 1
        articles = tuple(
            replace(a, url=f"{page}#item-{i}" if many else page) if a.url.startswith(NAV_HOSTS) else a
            for i, a in enumerate(message.articles, start=1)
        )
        return replace(message, articles=articles, url=page)

    def publish_page(self, title: str, markdown: str) -> str | None:
        """Store an arbitrary page (e.g. the daily admin report) → its URL, or None if disabled."""
        if not self.enabled:
            return None
        report_id = secrets.token_urlsafe(16)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(report_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"title": title, "markdown": markdown, "created": self._clock()},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        self._prune()
        return f"{self._base}/detail/{report_id}"

    def load(self, report_id: str) -> dict | None:
        path = self._path(report_id)
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if self._clock() - float(data.get("created", 0)) > DETAIL_TTL_DAYS * 86400:
            return None
        return data

    def _prune(self) -> None:
        cutoff = self._clock() - DETAIL_TTL_DAYS * 86400
        files = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for i, f in enumerate(files):
            if f.stat().st_mtime < cutoff or i < len(files) - MAX_PAGES:
                f.unlink(missing_ok=True)
