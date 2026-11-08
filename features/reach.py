"""How to get from home to a place — easy bus routes first, but hard-to-reach places stay as options.

A place with no gentle bus route (≤ OUTING_MAX_TRANSFERS, ≤ OUTING_MAX_WALK_M) used to be dropped. Now it's
kept with the honest picture: the best public-transport route anyway ("公交不太方便：换乘 2 次，约 65 分钟")
and the driving time ("开车约 25 分钟，周末可以让家人开车带去，或打车"), so Mom and Dad can decide —
a weekend drive, a taxi, or bus + bike.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

import requests

from clients.amap_client import AmapClient, AmapError, Poi, TransitPlan
from config import Location, Settings
from messages import amap_nav_url

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reach:
    gentle: TransitPlan | None = None  # easy bus/subway route
    best: TransitPlan | None = None  # fastest public-transport route when there's no easy one
    drive_minutes: int | None = None  # only looked up when there's no easy route

    @property
    def easy(self) -> bool:
        return self.gentle is not None

    @property
    def minutes(self) -> int | None:
        plan = self.gentle or self.best
        return max(1, round(plan.duration_s / 60)) if plan else self.drive_minutes


def pick_gentlest_plan(plans: Iterable[TransitPlan], max_transfers: int, max_walk_m: int) -> TransitPlan | None:
    ok = [p for p in plans if p.rides >= 1 and p.transfers <= max_transfers and p.walking_m <= max_walk_m]
    return min(ok, key=lambda p: (p.transfers, p.walking_m // 200, p.duration_s), default=None)


def _round_m(m: int) -> int:
    return max(50, int(round(m / 50.0)) * 50)


def get_reach(amap: AmapClient, settings: Settings, home: Location, poi: Poi,
              plans: list[TransitPlan] | None = None) -> Reach:
    """Transit routes (fewest transfers + least walking); driving time only if no easy route.
    Raises AmapError / RequestException for the transit lookup (the caller decides); driving is optional."""
    if plans is None:
        plans = []
        for strategy in (2, 3):  # fewest transfers, least walking
            plans += amap.get_transit_routes(home.coords, poi.location, settings.city_name, strategy)
    gentle = pick_gentlest_plan(plans, settings.outing_max_transfers, settings.outing_max_walk_m)
    if gentle is not None:
        return Reach(gentle=gentle)
    best = min((p for p in plans if p.rides >= 1), key=lambda p: p.duration_s, default=None)
    try:
        drive = amap.get_driving_route(home.coords, poi.location)
        drive_minutes = max(1, round(drive.duration_s / 60))
    except (AmapError, requests.RequestException) as exc:
        log.info("no driving time for %s: %s", poi.name, exc)
        drive_minutes = None
    return Reach(best=best, drive_minutes=drive_minutes)


def clean_line_name(name: str) -> str:
    """"12路(火车站--东客站)" → "12路"."""
    return re.split(r"[（(]", name, maxsplit=1)[0].strip() or name


def hard_route_line(reach: Reach) -> str:
    """"公交不太方便：坐 K6路 → 38路（换乘 1 次），走路约 3050 米，约 99 分钟" — the actual lines, so Mom
    and Dad can judge the trip themselves (or no usable route at all)."""
    b = reach.best
    if b is None:
        return "公交不太方便：附近没有合适的公交"
    rides = " → ".join(clean_line_name(leg.line_name) for leg in b.legs if leg.mode != "walk" and leg.line_name)
    parts = [f"坐 {rides}" + (f"（换乘 {b.transfers} 次）" if b.transfers else "") if rides
             else (f"要换乘 {b.transfers} 次" if b.transfers else "不用换乘")]
    if b.walking_m > 0:
        parts.append(f"走路约 {_round_m(b.walking_m)} 米")
    return f"公交不太方便：{'，'.join(parts)}，约 {max(1, round(b.duration_s / 60))} 分钟"


def drive_line(reach: Reach) -> str:
    if reach.drive_minutes:
        return f"开车约 {reach.drive_minutes} 分钟，周末可以让家人开车带去，或打车"
    return "可以打车，或周末让家人开车带去"


def hard_reach_lines(reach: Reach, home: Location, poi: Poi) -> list[str]:
    """Markdown for a place without an easy bus route: the honest route note + bus and car buttons."""
    return [
        f"> 🚌 {hard_route_line(reach)}",
        f"> 🚗 {drive_line(reach)}",
        f"[📍 公交怎么走]({amap_nav_url(home.coords, home.name, poi.location, poi.name, 'bus')})",
        f"[🚗 开车 / 打车导航]({amap_nav_url(home.coords, home.name, poi.location, poi.name, 'car')})",
    ]
