"""Parks and squares near Mom's home — a "🌳 家附近走走" section on the outing message.

* AMap nearby search (公园 / 植物园 / 城市广场) around MOTHER_HOME, cached for PARKS_CACHE_DAYS: parks don't
  move, and the AMap key has a low per-second quota.
* Only within an easy walk (PARK_MAX_M straight-line), no tiny residential gardens, nothing Mom said
  no to (preference feedback: 去过了 / 太远 / 不感兴趣).
* Rotates day by day among the nearest few, so it isn't the same park every morning.
* Walking time is an estimate from the straight-line distance (route ≈ 1.3× straight, ~60 m/min for an
  easy pace) — labelled "约", no extra API call.
* Shown only on days that are fine for being outside (the caller passes the weather advice).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import date

import requests

from clients.amap_client import AmapClient, AmapError, Poi
from config import Location
from features.preferences import PreferenceStore
from messages import Message, amap_nav_url
from storage import StateStore

log = logging.getLogger(__name__)

PARK_TYPES = "110101|110103|110105"  # 公园 | 植物园 | 城市广场 (广场 = 广场舞, 晨练)
PARK_MAX_M = 2000
PARKS_SHOWN = 2
ROTATE_AMONG = 5
PARKS_CACHE_DAYS = 30
CACHE_KEY = "nearby_parks_v2"  # v2: fixed kinds + merged park sections (v1 cache is simply ignored)
PRIVATE_HINTS = ("小区", "内部", "停车", "单位", "家属院")  # residents-only courtyards, compounds, car parks
PART_HINTS = ("沙坑", "厕所", "卫生间", "入口", "出口", "门岗", "售票", "游乐场", "儿童乐园")  # bits of a park
_SECTION = re.compile(r"[-—（(]?\s*(?:[A-Za-z0-9一二三四东西南北]+区|[东西南北正]门|[东西南北]园)\s*[）)]?$")
WALK_M_PER_MIN = 60
ROUTE_FACTOR = 1.3


@dataclass(frozen=True)
class Park:
    name: str
    location: str
    distance_m: int
    photo: str = ""
    kind: str = "公园"

    @property
    def walk_minutes(self) -> int:
        return max(1, round(self.distance_m * ROUTE_FACTOR / WALK_M_PER_MIN))


def _kind(poi_type: str) -> str:
    """AMap type "风景名胜;公园广场;城市广场" → its last part ("…;公园广场;公园" is a park, not a square)."""
    last = poi_type.split(";")[-1] if poi_type else ""
    return "广场" if "广场" in last else ("植物园" if "植物园" in last else "公园")


def _base_name(name: str) -> str:
    """"丝绸之路公园B区" / "青城公园(东门)" → "丝绸之路公园" / "青城公园"."""
    return _SECTION.sub("", name).strip() or name


def _usable(p: Poi) -> bool:
    return (0 < p.distance_m <= PARK_MAX_M and not any(h in p.name for h in PRIVATE_HINTS)
            and not any(h in p.name for h in PART_HINTS))


def nearby_parks(amap: AmapClient, home: Location, state: StateStore, clock=time.time) -> list[Park]:
    """Usable parks near ``home``, nearest first ([] if AMap is unreachable and nothing is cached)."""
    cached = state.get(CACHE_KEY) or {}
    if cached.get("home") == home.coords and clock() - float(cached.get("at", 0)) < PARKS_CACHE_DAYS * 86400:
        return [Park(**p) for p in cached.get("parks", [])]
    try:
        pois = amap.search_around(home.coords, PARK_TYPES, radius_m=PARK_MAX_M)
    except (AmapError, requests.RequestException) as exc:
        log.warning("nearby parks unavailable: %s", exc)
        return [Park(**p) for p in cached.get("parks", [])]  # stale is better than nothing
    parks: list[Park] = []
    seen: set[str] = set()
    for p in pois:  # nearest first; one entry per park (its nearest section, renamed to the park)
        base = _base_name(p.name)
        if _usable(p) and base not in seen:
            seen.add(base)
            parks.append(Park(base, p.location, p.distance_m, p.photo, _kind(p.type)))
    state.set(CACHE_KEY, {"home": home.coords, "at": clock(), "parks": [p.__dict__ for p in parks]})
    return parks


def pick_parks(parks: list[Park], today: date, prefs: PreferenceStore | None = None,
               exclude: tuple[str, ...] = ()) -> list[Park]:
    """PARKS_SHOWN parks, rotating daily among the nearest ROTATE_AMONG that Mom hasn't ruled out."""
    ok = [p for p in parks if p.name not in exclude and not (prefs is not None and prefs.venue_blocked(p.name))]
    pool = ok[:ROTATE_AMONG]
    if len(pool) <= PARKS_SHOWN:
        return pool
    start = today.toordinal() % len(pool)
    return [pool[(start + i) % len(pool)] for i in range(PARKS_SHOWN)]


def add_parks(message: Message, parks: list[Park], home: Location,
              tip: str = "去公园走一圈，活动活动筋骨，累了就找个长椅歇一歇～") -> Message:
    """Append the "🌳 家附近走走" section; numbered items continue after the message's own items so the
    push page shows each park as a card (photo + walking time, tap → AMap walking navigation)."""
    if not parks:
        return message
    first = _next_item_number(message.markdown)
    lines = ["", "**🌳 家附近走走**"]
    images = list(message.item_images) + [""] * (first - 1 - len(message.item_images))
    for i, p in enumerate(parks, start=first):
        lines += [f"**{i}. {p.name}**", f"> 🚶 走路约 {p.walk_minutes} 分钟（约 {_round_m(p.distance_m)} 米）· {p.kind}",
                  f"[走路去{p.name}]({amap_nav_url(home.coords, home.name, p.location, p.name, 'walk')})", ""]
        images.append(p.photo)
    lines.append(tip)
    return replace(message, markdown=message.markdown.rstrip() + "\n" + "\n".join(lines),
                   item_images=tuple(images))


def _next_item_number(markdown: str) -> int:
    nums = [int(n) for n in re.findall(r"^\*\*(\d+)\.", markdown, flags=re.M)]
    return max(nums, default=0) + 1


def _round_m(m: int) -> int:
    return max(100, int(round(m * ROUTE_FACTOR / 100.0)) * 100)
