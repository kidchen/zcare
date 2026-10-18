"""Render a feature Message for PushPlus: a short notification title + a self-contained HTML page.

The page is the same content as the H5 detail page (large text, cream background, big buttons), but
with INLINE styles — the PushPlus page may drop <style> blocks — and only ``https`` images (the page is
https, so http images from our own server are blocked as mixed content). Covers load with no Referer,
which WeChat's image CDN (mmbiz.qpic.cn) requires off-site.
"""
from __future__ import annotations

import html
import re

from features.detail import markdown_to_html
from messages import Message

HEADLINE_CHARS = 20  # ~12 show in the notification; a few more are visible on the page title
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_SPACES = re.compile(r"\s+")

STYLES = {
    "h1": "font-size:26px;line-height:1.4;margin:4px 0 16px;color:#1F1F1F",
    "h2": "font-size:23px;line-height:1.45;margin:26px 0 8px;color:#1F1F1F",
    "p": "margin:10px 0",
    "note": "background:#FFFFFF;border-left:6px solid #E0A458;border-radius:10px;padding:12px 16px;margin:10px 0",
    "list": "padding-left:1.3em;margin:10px 0",
    "li": "margin:10px 0",
    "a": "color:#0B57D0",
    "btn": ("display:block;margin:16px 0;padding:16px;border-radius:14px;background:#0B57D0;color:#FFFFFF;"
            "font-size:22px;font-weight:600;text-align:center;text-decoration:none"),
}
PAGE = ('<div style="background:#FAF7F0;color:#2F2F2F;font-size:21px;line-height:1.75;padding:18px 16px;'
        'border-radius:12px;overflow-wrap:anywhere;word-break:break-word">{body}'
        '<p style="margin-top:26px;color:#6B6B6B;font-size:16px">家里的小助手 · 信息仅供参考，出门前以实际情况为准</p></div>')


def push_title(message: Message) -> str:
    """Notification title: the key fact first, no emoji (WeChat drops them anyway)."""
    raw = message.headline or message.title
    return _SPACES.sub(" ", _EMOJI.sub("", raw)).strip()[:HEADLINE_CHARS] or "家里的小助手"


def _inline_styles(fragment: str) -> str:
    fragment = fragment.replace('<p class="note">', f'<p style="{STYLES["note"]}">')
    fragment = fragment.replace('<a class="btn" ', f'<a style="{STYLES["btn"]}" ')
    fragment = re.sub(r"<h1>", f'<h1 style="{STYLES["h1"]}">', fragment)
    fragment = re.sub(r'<h2( id="[^"]*")?>', lambda m: f'<h2{m.group(1) or ""} style="{STYLES["h2"]}">', fragment)
    fragment = re.sub(r"<(ul|ol)>", lambda m: f'<{m.group(1)} style="{STYLES["list"]}">', fragment)
    fragment = fragment.replace("<li>", f'<li style="{STYLES["li"]}">').replace("<p>", f'<p style="{STYLES["p"]}">')
    return fragment.replace('<a href="', f'<a style="{STYLES["a"]}" href="')


def render_push_html(message: Message) -> str:
    body = _inline_styles(markdown_to_html(message.markdown))
    if message.cover.startswith("https://"):
        img = (f'<img src="{html.escape(message.cover, quote=True)}" referrerpolicy="no-referrer" alt="" '
               'style="display:block;width:100%;border-radius:10px;margin:12px 0">')
        cut = body.find("</h1>")
        body = body[: cut + 5] + img + body[cut + 5:] if cut >= 0 else img + body
    return PAGE.format(body=body)
