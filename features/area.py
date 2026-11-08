"""「大黑河那边有什么逛的」— places to visit around a place Mom or Dad named.

GLM extracts the place ("大黑河") from the message; code verifies it appears literally in what they wrote.
AMap finds the place, then parks / scenic spots / museums / malls around it (nearest first, one entry per
park, nothing residents-only, nothing they ruled out). Each gets the way to get there from home: the easy
bus route if there is one, otherwise the honest note + driving time (features/reach.py). If AMap doesn't know
the place, 小助理 asks which area they mean instead of sending something generic.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace

import requests

from clients.amap_client import AmapClient, AmapError, Poi
from config import Location, Settings
from features.outing import route_summary
from features.parks import PART_HINTS, PRIVATE_HINTS, _base_name
from features.preferences import PreferenceStore
from features.reach import Reach, get_reach, hard_reach_lines
from messages import Message, amap_nav_url

log = logging.getLogger(__name__)

AREA_TYPES = "110000|140100|140200|060101"  # 风景名胜 (parks, scenic) | 博物馆 | 展览馆 | 购物中心
AREA_RADIUS_M = 3000
AREA_SHOWN = 3
MAX_PLACE_CHARS = 20
_AROUND_WORDS = re.compile(r"(那边|这边|附近|周边|周围|一带|那儿|这儿|那里|这里|旁边|边上|那块)+$")
_SUB = re.compile(r"\s*[-—(（].*$")  # "大召无量寺-菩萨殿" / "格桑花海(大黑河郊野公园)" → part of a bigger place


def verified_place(raw: object, text: str) -> str:
    """GLM proposes, code verifies: the place must be written in the user's own message.
    "大黑河那边" → "大黑河" (the "that side / nearby" words aren't part of the name)."""
    place = str(raw or "").strip().strip("「」\"'")
    if not place or len(place) > MAX_PLACE_CHARS or place not in text:
        return ""
    return _AROUND_WORDS.sub("", place).strip()


def _group(name: str) -> str:
    return _base_name(_SUB.sub("", name)) or name


def _same_place(a: str, b: str) -> bool:
    return a in b or b in a


def _kind(poi_type: str) -> str:
    return poi_type.split(";")[-1] if poi_type else "好去处"


def find_spots(amap: AmapClient, settings: Settings, place: str,
               prefs: PreferenceStore | None = None) -> tuple[Poi | None, list[Poi]]:
    """→ (an anchor for the place, up to AREA_SHOWN spots). (None, []) if AMap doesn't know the place.

    Places NAMED after it come first (「大黑河」 is a long river whose map point is meaningless; 大黑河郊野公园
    is what Mom means), then spots around the best match. Sub-attractions ("…公园-梵草映月") are grouped
    under their park; residents-only places and park parts are dropped, and so is anything Mom ruled out."""
    named = amap.search_text(place, settings.city_name, types=AREA_TYPES)
    anchor = named[0] if named else (amap.search_poi(place, settings.city_name)
                                     or amap.search_poi(place, settings.city_name, strict=False))
    if anchor is None:
        return None, []
    nearby = amap.search_around(anchor.location, AREA_TYPES, radius_m=AREA_RADIUS_M) if named or anchor else []
    spots: list[Poi] = []
    groups: list[str] = []
    for p in named + nearby:
        g = _group(p.name)
        if (any(_same_place(g, x) or x in p.name for x in groups) or any(h in p.name for h in PRIVATE_HINTS + PART_HINTS)
                or (prefs is not None and prefs.venue_blocked(p.name, g))):
            continue
        groups.append(g)
        spots.append(p if p.name == g else replace(p, name=g, type="景区"))  # a sub-entry's type isn't the place's
        if len(spots) >= AREA_SHOWN:
            break
    return anchor, spots


def build_area_message(amap: AmapClient, settings: Settings, place: str, home: Location, name: str,
                       prefs: PreferenceStore | None = None, greeting: str = "你好") -> Message:
    try:
        center, spots = find_spots(amap, settings, place, prefs)
    except (AmapError, requests.RequestException) as exc:
        log.warning("area search for %s failed: %s", place, exc)
        center, spots = None, []
    if center is None or not spots:
        what = f"在地图上没找到「{place}」" if center is None else f"「{place}」附近没找到合适的去处"
        md = f"**{name}{greeting} 🌷**\n\n{what}，能再说具体一点吗？比如附近的路名、小区或者公园的名字～"
        return Message(title=f"{place}附近", markdown=md, headline=f"{place}附近没找到")
    lines = [f"**{name}{greeting} 🌷 {place}那边可以逛逛这些**", ""]
    images = []
    for i, p in enumerate(spots, start=1):
        try:
            reach = get_reach(amap, settings, home, p)
        except (AmapError, requests.RequestException) as exc:
            log.info("no route to %s: %s", p.name, exc)
            reach = Reach()
        where = f" · 离{place}约 {_m(p.distance_m)}" if p.distance_m and place not in p.name else ""
        lines += [f"**{i}. {p.name}**", f"> {_kind(p.type)}{where}"]
        if reach.easy:
            lines += [f"> 🚌 {route_summary(reach.gentle)}",
                      f"[📍 公交怎么走]({amap_nav_url(home.coords, home.name, p.location, p.name, 'bus')})"]
        else:
            lines += hard_reach_lines(reach, home, p)
        lines.append("")
        images.append(p.photo)
    lines.append("出门前看看天气，走累了就找地方坐一坐～")
    return Message(title=f"{place}附近 · {spots[0].name}", markdown="\n".join(lines).strip(),
                   headline=f"{place}附近：{spots[0].name}等{len(spots)}处", item_images=tuple(images))


def _m(distance_m: int) -> str:
    return f"{distance_m / 1000:.1f} 公里" if distance_m >= 1000 else f"{max(50, round(distance_m / 50) * 50)} 米"
