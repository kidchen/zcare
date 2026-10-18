"""Render a feature Message for PushPlus: a short notification title + a self-contained HTML page.

Design (seniors, modern, light AND dark mode):
  * large type (body 20px, titles 22–26px), generous spacing, rounded cards;
  * numbered items ("**1. …**" + "> …" lines + "[label](url)") become one card each — cover image,
    bold title, muted summary, a soft "label ›" line — and the WHOLE card is the link (big tap target);
  * loose "> …" info lines (weather, traffic) are grouped into one soft panel;
  * a link outside any item is the single main action: a rounded WeChat-green button;
  * no page background or text colour is forced (follows WeChat's theme); tints are translucent.
Styles are INLINE (the PushPlus page may drop <style> blocks). Images must be ``https`` (the page is
https; http images are blocked as mixed content) and load with no Referer (mmbiz.qpic.cn requires it).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from features.detail import _FONT_RAW, _inline
from messages import Message

HEADLINE_CHARS = 20  # ~12 show in the notification; a few more are visible on the page title
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_SPACES = re.compile(r"\s+")
_ITEM = re.compile(r"^\*\*(\d+)\.\s*(.+?)\*\*\s*(.*)$")
_HEADING = re.compile(r"^\*\*(.+?)\*\*$")
_LINK_LINE = re.compile(r"^\[([^\]]+)\]\((https?://[^)\s]+)\)$")
_ARROW = re.compile(r"\s*[›>]+\s*$")

ACCENT = "#07C160"  # WeChat green: familiar to Mom and Dad, reads on light and dark
PANEL = "background:rgba(127,127,127,0.10);border-radius:16px;padding:14px 16px;margin:14px 0"
CARD = ("display:block;text-decoration:none;color:inherit;background:rgba(127,127,127,0.08);"
        "border:1px solid rgba(127,127,127,0.18);border-radius:18px;overflow:hidden;margin:16px 0")
S = {
    "page": "font-size:20px;line-height:1.7;padding:4px 2px 8px;overflow-wrap:anywhere;word-break:break-word",
    "h1": "font-size:26px;line-height:1.35;font-weight:700;margin:6px 0 14px",
    "h2": "font-size:21px;font-weight:700;margin:24px 0 6px",
    "row": "margin:4px 0",
    "p": "margin:12px 0",
    "img": "display:block;width:100%;height:190px;object-fit:cover",
    "body": "padding:14px 16px 16px",
    "title": "font-size:22px;line-height:1.45;font-weight:700;margin:0 0 6px",
    "meta": "font-size:18px;opacity:0.7;margin:2px 0",
    "text": "font-size:19px;line-height:1.65;opacity:0.85;margin:4px 0",
    "more": f"font-size:18px;font-weight:600;color:{ACCENT};margin:10px 0 0",
    "btn": (f"display:block;margin:20px 0 8px;padding:15px 16px;border-radius:999px;background:{ACCENT};"
            "color:#FFFFFF;font-size:21px;font-weight:600;text-align:center;text-decoration:none"),
    "list": "padding-left:1.2em;margin:8px 0",
    "li": "margin:8px 0",
    "foot": "margin-top:28px;font-size:15px;opacity:0.55;text-align:center",
}


def push_title(message: Message) -> str:
    """Notification title: the key fact first, no emoji (WeChat drops them anyway)."""
    raw = message.headline or message.title
    return _SPACES.sub(" ", _EMOJI.sub("", raw)).strip()[:HEADLINE_CHARS] or "家里的小助手"


def _text(raw: str) -> str:
    """Escape + the inline subset (**bold**, [text](url)); WeCom <font> tags are dropped first."""
    return _inline(html.escape(_FONT_RAW.sub("", raw).strip(), quote=True))


def _img(url: str, style: str) -> str:
    if not url.startswith("https://"):
        return ""
    return f'<img src="{html.escape(url, quote=True)}" referrerpolicy="no-referrer" alt="" style="{style}">'


@dataclass
class _Item:
    number: int
    title: str
    meta: str = ""
    lines: list[str] = field(default_factory=list)
    link: tuple[str, str] | None = None  # (label, url)


def _card(item: _Item, image: str) -> str:
    parts = [_img(image, S["img"]), f'<div style="{S["body"]}">',
             f'<div style="{S["title"]}">{item.number}. {_text(item.title)}</div>']
    if item.meta:
        parts.append(f'<div style="{S["meta"]}">{_text(item.meta)}</div>')
    parts += [f'<div style="{S["text"]}">{_text(line)}</div>' for line in item.lines]
    if item.link:
        parts.append(f'<div style="{S["more"]}">{html.escape(_ARROW.sub("", item.link[0]))} ›</div>')
    parts.append("</div>")
    inner = "".join(parts)
    if item.link:  # the whole card is the tap target
        return f'<a href="{html.escape(item.link[1], quote=True)}" rel="noreferrer" style="{CARD}">{inner}</a>'
    return f'<div style="{CARD}">{inner}</div>'


def render_push_html(message: Message) -> str:
    out: list[str] = []
    panel: list[str] = []
    bullets: list[str] = []
    item: _Item | None = None
    first = True

    def flush() -> None:
        nonlocal item
        if panel:
            out.append(f'<div style="{PANEL}">' + "".join(f'<div style="{S["row"]}">{r}</div>' for r in panel) + "</div>")
            panel.clear()
        if bullets:
            out.append(f'<ul style="{S["list"]}">' + "".join(f'<li style="{S["li"]}">{b}</li>' for b in bullets) + "</ul>")
            bullets.clear()
        if item is not None:
            i = item.number - 1
            out.append(_card(item, message.item_images[i] if i < len(message.item_images) else ""))
            item = None

    for raw in message.markdown.splitlines():
        line = raw.strip()
        if not line:
            if item is None:
                flush()
            continue
        if m := _ITEM.match(line):
            flush()
            item = _Item(int(m.group(1)), m.group(2), m.group(3))
            continue
        if item is not None:
            if line.startswith(">"):
                item.lines.append(line.lstrip("> ").strip())
                continue
            if (m := _LINK_LINE.match(line)) and item.link is None:
                item.link = (m.group(1), m.group(2))
                continue
            flush()
        if line.startswith(">"):
            panel.append(_text(line.lstrip("> ")))
        elif line.startswith("- "):
            bullets.append(_text(line[2:]))
        elif m := _LINK_LINE.match(line):
            flush()
            out.append(f'<a href="{html.escape(m.group(2), quote=True)}" rel="noreferrer" style="{S["btn"]}">'
                       f'{html.escape(m.group(1))}</a>')
        elif m := _HEADING.match(line):
            flush()
            tag, style = ("h1", S["h1"]) if first else ("h2", S["h2"])
            out.append(f'<{tag} style="{style}">{_text(m.group(1))}</{tag}>')
            if first and (cover := _img(message.cover, S["img"].replace("height:190px", "height:200px")
                                        + ";border-radius:16px;margin:4px 0 12px")):
                out.append(cover)
        else:
            flush()
            out.append(f'<p style="{S["p"]}">{_text(line)}</p>')
        first = False
    flush()
    return (f'<div style="{S["page"]}">' + "".join(out)
            + f'<div style="{S["foot"]}">家里的小助手 · 信息仅供参考，出门前以实际情况为准</div></div>')
