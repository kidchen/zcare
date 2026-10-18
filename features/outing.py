"""Feature 2 — Mom's low-stress outing suggestion with gentle transit directions.

Sources, in priority order:
  1. Real upcoming Hohhot cultural events (features/events.py: web search → GLM extraction →
     verified against the source text and AMap). Shown as up to 3 events with bus routes.
  2. Scraped event list page (optional, ``HtmlListEventSource``, configured via OUTING_EVENT_*).
  3. ``CURATED_PLACES`` — museums, cultural venues and public parks, every name verified on AMap.

Route rule: at most ``OUTING_MAX_TRANSFERS`` transfers and ``OUTING_MAX_WALK_METERS``
of walking. Among valid plans prefer fewer transfers, then less walking, then faster.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import requests
from bs4 import BeautifulSoup

from clients.amap_client import (
    AmapClient,
    AmapError,
    DailyForecast,
    LiveWeather,
    Poi,
    TransitLeg,
    TransitPlan,
    poi_is_indoor,
)
from clients.http import DESKTOP_UA, build_session
from clients.llm_client import LLMError
from config import Settings
from features.commute import format_weather
from features.daytype import DayInfo, day_info, off_days
from features.events import EventFinder, LocalEvent, notice_lines
from features.preferences import PreferenceStore
from messages import Article, Message, add_notices, amap_nav_url
from storage import StateStore

log = logging.getLogger(__name__)

STATE_KEY = "outing_recent_places"
RECENT_KEEP = 4  # don't repeat a place within ~4 days
MAX_CANDIDATES_TRIED = 4  # each try costs 1 POI + 2 transit calls of AMap quota


@dataclass(frozen=True)
class Place:
    name: str
    category: str
    indoor: bool
    description: str
    closed_weekdays: tuple[int, ...] = ()  # 0 = Monday
    search_keyword: str | None = None  # AMap POI keyword if it differs from name
    source: str = "curated"


# Museums, cultural venues and public parks (no commercial attractions). Every name/keyword
# was checked to resolve to the right AMap POI in Hohhot. Opening days change — verify
# closed_weekdays before relying on them.
CURATED_PLACES: tuple[Place, ...] = (
    Place("内蒙古博物院", "博物馆", True, "草原文化和恐龙化石展厅，室内有座椅，适合慢慢逛。",
          closed_weekdays=(0,)),  # usually closed Mondays; reservation usually required
    Place("将军衙署博物院", "博物馆", True, "清代绥远将军衙署旧址，院落规整，讲的是老呼市的历史。"),
    Place("内蒙古美术馆", "美术馆", True, "安静的室内展馆，常有书画和摄影展。"),
    Place("呼和浩特雕塑艺术馆", "艺术馆", True, "周末常有免费的公益电影放映，一般要提前预约。"),
    Place("内蒙古图书馆", "图书馆", True, "可以坐下来看看报纸杂志，冬夏都舒适。"),
    Place("大召广场", "寺庙广场", False, "大召寺前的广场，老城味道浓，常有晨练和民俗活动。"),
    Place("乌兰夫公园", "公园", False, "市区里的公园，适合散散步、晒晒太阳。"),
    Place("青城公园", "公园", False, "市中心老公园，湖边有长椅，树荫多。"),
    Place("满都海公园", "公园", False, "园内道路平缓，适合散步、晒太阳。"),
    Place("公主府公园", "公园", False, "清代公主府旧址旁的园林，人少清静。"),
)
MAX_EVENTS_SHOWN = 3
MAX_EVENTS_TRIED = 6


class EventSource(Protocol):
    def fetch(self) -> list[Place]: ...


class HtmlListEventSource:
    """Generic scraper for a venue's "exhibitions/events" list page.

    Each element matching ``item_selector`` is treated as one event title at ``venue``.
    Pick the selector in browser devtools, e.g. ``ul.news-list li a``.
    """

    def __init__(
        self,
        url: str,
        item_selector: str,
        venue: str,
        indoor: bool = True,
        session: requests.Session | None = None,
        max_items: int = 3,
    ):
        self._url = url
        self._selector = item_selector
        self._venue = venue
        self._indoor = indoor
        self._session = session or build_session(DESKTOP_UA)
        self._max_items = max_items

    def fetch(self) -> list[Place]:
        resp = self._session.get(self._url, timeout=10)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding  # many local sites are GBK
        return parse_event_list(resp.text, self._selector, self._venue, self._indoor)[: self._max_items]


def parse_event_list(html: str, selector: str, venue: str, indoor: bool = True) -> list[Place]:
    soup = BeautifulSoup(html, "html.parser")
    places = []
    for el in soup.select(selector):
        title = " ".join(el.get_text(" ", strip=True).split())
        if title:
            places.append(Place(venue, "展览活动", indoor, f"近期活动：{title[:60]}", source="event"))
    return places


# ------------------------------------------------------------------ weather


STAY_HOME_KEYWORDS = ("暴雨", "暴雪", "大暴", "沙尘暴", "冰雹", "雷阵雨")
INDOOR_KEYWORDS = ("雨", "雪", "雾", "霾", "沙", "尘")


def _wind_level(power: str) -> int:
    digits = re.findall(r"\d+", power)
    return int(digits[-1]) if digits else 0


def weather_advice(live: LiveWeather | None, today: DailyForecast | None) -> str:
    """'home' | 'indoor' | 'any'. Unknown weather (AMap down) → 'any'; official warnings still show."""
    if live is None:
        return "indoor" if today and any(k in today.day_weather for k in INDOOR_KEYWORDS) else "any"
    sky = live.weather + (today.day_weather if today else "")
    wind = max(_wind_level(live.wind_power), _wind_level(today.day_wind_power) if today else 0)
    if any(k in sky for k in STAY_HOME_KEYWORDS) or wind >= 7:
        return "home"
    if any(k in sky for k in INDOOR_KEYWORDS) or wind >= 5:
        return "indoor"
    if live.temperature_c <= 0 or live.temperature_c >= 32:
        return "indoor"
    return "any"


# ------------------------------------------------------------------ routing


def pick_gentlest_plan(
    plans: Iterable[TransitPlan], max_transfers: int, max_walk_m: int
) -> TransitPlan | None:
    ok = [p for p in plans if p.rides >= 1 and p.transfers <= max_transfers and p.walking_m <= max_walk_m]
    return min(ok, key=lambda p: (p.transfers, p.walking_m // 200, p.duration_s), default=None)


def order_candidates(
    places: Iterable[Place], today: date, advice: str, recent: list[str],
    prefs: PreferenceStore | None = None,
) -> list[Place]:
    pool = [p for p in places if today.weekday() not in p.closed_weekdays]
    if prefs is not None:  # learned from chat: "这个去过了", "我们不喜欢看展览"
        pool = [p for p in pool if not prefs.venue_blocked(p.name, p.search_keyword or "")
                and prefs.category_penalty(p.category) < 2] or pool
    if advice == "indoor":
        pool = [p for p in pool if p.indoor] or pool
    fresh = [p for p in pool if p.name not in recent] or pool
    if not fresh:
        return []
    events = [p for p in fresh if p.source == "event"]
    rest = [p for p in fresh if p.source != "event"]
    if rest:
        offset = today.toordinal() % len(rest)  # deterministic daily rotation
        rest = rest[offset:] + rest[:offset]
        if prefs is not None:  # stable sort: disliked categories after the rest, liked ones first
            rest.sort(key=lambda p: prefs.category_penalty(p.category))
    return events + rest


def _clean_line_name(name: str) -> str:
    # "12路(火车站--东客站)" → "12路"
    return re.split(r"[（(]", name, maxsplit=1)[0].strip() or name


def _round_m(meters: int) -> int:
    return max(50, int(round(meters / 50.0)) * 50)


def describe_leg(leg: TransitLeg, is_last: bool) -> str:
    if leg.mode == "walk":
        return f"步行约 {_round_m(leg.distance_m)} 米" + ("到达" if is_last else "")
    line = _clean_line_name(leg.line_name)
    stops = f"（{leg.stop_count} 站）" if leg.stop_count else ""
    return f"乘坐 **{line}**：「{leg.departure_stop}」上车 →「{leg.arrival_stop}」下车{stops}"


# ------------------------------------------------------------------ message


def gentle_tips(advice: str, live: LiveWeather | None, plan: TransitPlan | None) -> list[str]:
    tips = ["9:30 以后再出门，错开早高峰，车上更容易有座位"]
    if plan is not None and plan.transfers == 0:
        tips.append("回程在对面站台坐同一路车就能回家")
    if live is None:
        tips.append("出门前看看天气，多带一件外套")
    elif live.temperature_c <= 5:
        tips.append("天凉，戴好帽子围巾，注意保暖")
    elif live.temperature_c >= 28:
        tips.append("天热，避开中午，带上水和遮阳帽")
    tips.append("走累了就找地方坐一坐，不赶时间，慢慢逛")
    tips.append("带好手机、水杯和常用药")
    return tips


def format_outing_message(
    *,
    name: str,
    place: Place,
    poi: Poi,
    plan: TransitPlan,
    live: LiveWeather,
    today: DailyForecast | None,
    advice: str,
    nav_url: str | None = None,
    greeting: str = "早上好",
) -> Message:
    minutes = max(1, round(plan.duration_s / 60))
    where = "室内" if place.indoor else "户外"
    lines = [
        f"**{name}{greeting} 🌷 今天出去走走？**",
        f"> 📍 推荐：**{poi.name or place.name}**（{place.category} · {where}）",
        f"> {place.description}",
        f"> 🌤 天气：{format_weather(live, today)}",
        "",
        f"**🚌 怎么去**（约 {minutes} 分钟 · 步行约 {_round_m(plan.walking_m)} 米"
        + (f" · {plan.cost_yuan:g} 元" if plan.cost_yuan else "")
        + "）",
    ]
    for i, leg in enumerate(plan.legs, start=1):
        lines.append(f"{i}. {describe_leg(leg, is_last=i == len(plan.legs))}")
    lines += ["", "**💡 小贴士**"]
    lines += [f"- {t}" for t in gentle_tips(advice, live, plan)]
    if nav_url:
        lines.append(f"[📍 打开高德地图导航]({nav_url})")
    place_name = poi.name or place.name
    articles: tuple[Article, ...] = ()
    if nav_url:
        desc = "\n".join([
            f"{place.category} · {where}",
            f"🚌 {route_summary(plan)}",
            "点开看完整路线和出行贴士 ›",
        ])
        articles = (Article(f"{name}{greeting}，今天去{place_name}走走？", desc, nav_url, poi.photo),)
    return Message(title=f"今日出行建议 · {place_name}", markdown="\n".join(lines), url=nav_url,
                   articles=articles, headline=f"去{place_name} 公交{minutes}分钟",
                   cover=poi.photo if poi.photo.startswith("https://") else "")


def _sky_short(live: LiveWeather | None, today: DailyForecast | None) -> str:
    """"小雪-3°C " — weather and temperature for a push headline ("" if unknown)."""
    if live is not None:
        return f"{live.weather}{live.temperature_c}°C "
    if today is not None:
        return f"{today.day_weather}{today.night_temp_c}~{today.day_temp_c}°C "
    return ""


def format_stay_home_message(
    name: str, live: LiveWeather | None, today: DailyForecast | None, greeting: str = "早上好"
) -> Message:
    md = "\n".join([
        f"**{name}{greeting} 🏠**",
        f"> 🌧 天气：{format_weather(live, today)}",
        "",
        "今天天气不太适合出门，在家歇一歇吧。",
        "可以泡杯茶、看看剧，或者在屋里活动活动筋骨～",
    ])
    return Message(title="今天适合在家休息", markdown=md, headline=f"{_sky_short(live, today)}今天在家歇歇")


def format_no_route_message(
    name: str, live: LiveWeather | None, today: DailyForecast | None, greeting: str = "早上好"
) -> Message:
    md = "\n".join([
        f"**{name}{greeting} 🌷**",
        f"> 🌤 天气：{format_weather(live, today)}",
        "",
        "今天没找到特别省力的公交路线，就在家附近散散步、晒晒太阳吧。",
    ])
    return Message(title="今日出行建议", markdown=md, headline=f"{_sky_short(live, today)}附近散散步")


def amap_search_url(keyword: str, city: str) -> str:
    """AMap URI search link — works without coordinates (used when AMap's API is unreachable)."""
    from urllib.parse import quote
    return f"https://uri.amap.com/search?keyword={quote(keyword)}&city={quote(city)}&src=zcare&callnative=1"


def format_place_without_route(
    name: str, place: Place, city: str, live: LiveWeather | None, today: DailyForecast | None,
    greeting: str = "早上好",
) -> Message:
    """Degraded mode (map service unreachable): still a warm, useful suggestion — no route."""
    where = "室内" if place.indoor else "户外"
    nav = amap_search_url(place.search_keyword or place.name, city)
    md = "\n".join([
        f"**{name}{greeting} 🌷 今天出去走走？**",
        f"> 📍 推荐：**{place.name}**（{place.category} · {where}）",
        f"> {place.description}",
        f"> 🌤 天气：{format_weather(live, today)}",
        "",
        "今天的公交路线暂时没查到，可以点下面的按钮在高德地图里看看怎么走，或者问问家里人。",
        "",
        "**💡 小贴士**",
        *[f"- {t}" for t in gentle_tips("any", live, None)],
        f"[📍 打开高德地图导航]({nav})",
    ])
    article = Article(f"{name}{greeting}，今天去{place.name}走走？",
                      f"{place.category} · {where}\n{place.description}\n点开看出行贴士 ›", nav)
    return Message(title=f"今日出行建议 · {place.name}", markdown=md, url=nav, articles=(article,),
                   headline=f"去{place.name}走走？")


# ------------------------------------------------------------------ events


@dataclass(frozen=True)
class EventPick:
    event: LocalEvent
    poi: Poi
    plan: TransitPlan | None  # None = no gentle bus route


NO_GENTLE_ROUTE = "公交要多次换乘或走路较多，建议打车，或让家人开车送"


def route_summary(plan: TransitPlan) -> str:
    rides = " → ".join(_clean_line_name(leg.line_name) for leg in plan.legs if leg.mode != "walk")
    transfer = "不用换乘" if plan.transfers == 0 else f"换乘 {plan.transfers} 次"
    return (f"坐 {rides}，{transfer}，约 {max(1, round(plan.duration_s / 60))} 分钟，"
            f"步行约 {_round_m(plan.walking_m)} 米")


WEEKEND_CATEGORIES = ("演出", "民俗", "展览", "电影")  # leisure categories favoured on Fridays / days off


def rank_events(events: list[LocalEvent], today: date, info: DayInfo | None = None,
                prefs: PreferenceStore | None = None) -> list[LocalEvent]:
    """Order events before resolving them on AMap (only the first few are tried).

    - Disliked venues / categories (from chat feedback) are dropped or pushed down.
    - Weekend mode (Fridays + days off): events that happen on an upcoming day off, and leisure
      categories, come first.
    """
    if prefs is not None:
        events = [e for e in events if not prefs.venue_blocked(e.venue) and prefs.category_penalty(e.category) < 2]
    days_off = off_days(today, 7) if info is not None and info.weekend_mode else set()

    def key(e: LocalEvent):
        penalty = prefs.category_penalty(e.category) if prefs is not None else 0
        if days_off:
            on_off_day = e.start in days_off or (e.end is not None and any(e.start <= d <= e.end for d in days_off))
            return (penalty, not on_off_day, e.category not in WEEKEND_CATEGORIES, max(e.start, today), -e.senior_score)
        return (penalty, max(e.start, today), -e.senior_score)

    return sorted(events, key=key)


def pick_events(
    amap: AmapClient, settings: Settings, events: list[LocalEvent], advice: str,
    prefs: PreferenceStore | None = None, max_minutes: int | None = None,
) -> list[EventPick]:
    """Resolve each event's venue on AMap (must be a real Hohhot place) and find a gentle route."""
    picks: list[EventPick] = []
    venues: dict[str, tuple[Poi | None, TransitPlan | None]] = {}
    home = settings.mother_home
    for event in events[:MAX_EVENTS_TRIED]:
        if len(picks) >= MAX_EVENTS_SHOWN:
            break
        if event.venue not in venues:
            try:
                poi = amap.search_poi(event.venue, settings.city_name)
                plan = None
                if poi is not None:
                    plans: list[TransitPlan] = []
                    for strategy in (2, 3):
                        plans += amap.get_transit_routes(home.coords, poi.location, settings.city_name, strategy)
                    plan = pick_gentlest_plan(plans, settings.outing_max_transfers, settings.outing_max_walk_m)
            except (AmapError, requests.RequestException) as exc:
                log.warning("AMap lookup failed for venue %s: %s", event.venue, exc)
                poi, plan = None, None
            venues[event.venue] = (poi, plan)
        poi, plan = venues[event.venue]
        if poi is None:
            log.info("event dropped (venue not found on AMap): %s @ %s", event.title, event.venue)
            continue
        if advice == "indoor" and not poi_is_indoor(poi.type):
            continue
        if prefs is not None and prefs.venue_blocked(poi.name):
            continue
        if max_minutes is not None and (plan is None or plan.duration_s > max_minutes * 60):
            continue  # they told us places like this are too far
        picks.append(EventPick(event, poi, plan))
    return picks


def events_heading(info: DayInfo | None) -> str:
    if info is None:
        return "呼和浩特近期活动"
    if info.holiday:
        return f"{info.holiday}快乐！假期去哪儿逛逛"
    if not info.workday:
        return "周末去哪儿逛逛"
    if info.day.weekday() == 4:
        return "这个周末去哪儿玩"
    return "呼和浩特近期活动"


def format_events_message(
    *, name: str, greeting: str, picks: list[EventPick], live: LiveWeather | None,
    today: DailyForecast | None, home_coords: str, home_name: str, heading: str = "呼和浩特近期活动",
) -> Message:
    lines = [f"**{name}{greeting} 🌷 {heading}**", f"> 🌤 今天天气：{format_weather(live, today)}", ""]
    articles: list[Article] = []
    for i, p in enumerate(picks, start=1):
        ev = p.event
        price = f" · 💰 {ev.price}" if ev.price else ""
        route = route_summary(p.plan) if p.plan else NO_GENTLE_ROUTE
        new = "🆕 " if ev.fresh else ""  # newly announced in today's local news
        nav = amap_nav_url(home_coords, home_name, p.poi.location, p.poi.name, "bus")
        lines += [f"**{i}. {new}{ev.title}**", f"> 🗓 {ev.when()} · 📍 {p.poi.name}{price}", f"> 🚌 {route}",
                  f"[📍 导航去{p.poi.name}]({nav})", ""]
        articles.append(Article(
            title=f"{new}{ev.when()} {ev.title}",
            description="\n".join([f"📍 {p.poi.name}{price}", f"🚌 {route}", "点开看详情、路线和贴士 ›"]),
            url=nav,
            picurl=p.poi.photo,
        ))
    tips = ["活动以主办方公告为准，去之前在公众号或小程序确认是否需要预约、购票"]
    if any(p.event.is_evening() for p in picks):
        tips.append("晚上的演出散场晚，回家建议打车，或让家人来接")
    tips.append("带好手机、水杯和常用药，累了就找地方坐一坐")
    lines += ["**💡 小贴士**", *[f"- {t}" for t in tips]]
    photo = next((p.poi.photo for p in picks if p.poi.photo.startswith("https://")), "")
    return Message(title=f"近期活动 · {picks[0].event.title}", markdown="\n".join(lines),
                   url=articles[0].url, articles=tuple(articles),
                   headline=f"{len(picks)}个活动：{picks[0].event.title}", cover=photo)


# --------------------------------------------------------------- orchestration


def _gather_places(event_sources: Iterable[EventSource]) -> list[Place]:
    places: list[Place] = []
    for source in event_sources:
        try:
            places.extend(source.fetch())
        except (requests.RequestException, ValueError) as exc:
            log.warning("event source failed, falling back to curated list: %s", exc)
    return places + list(CURATED_PLACES)


def default_event_sources(settings: Settings) -> list[EventSource]:
    if settings.outing_event_url and settings.outing_event_item_selector and settings.outing_event_venue:
        return [HtmlListEventSource(
            settings.outing_event_url, settings.outing_event_item_selector, settings.outing_event_venue
        )]
    return []


def build_outing_message(
    amap: AmapClient,
    settings: Settings,
    state: StateStore,
    today: date,
    event_sources: Iterable[EventSource] | None = None,
    record: bool = True,
    name: str | None = None,
    greeting: str = "早上好",
    events: EventFinder | None = None,
    prefs: PreferenceStore | None = None,
    remember: bool = False,
) -> Message:
    """Never raises for feed / map / network problems: degrades to curated places + official warnings."""
    message = _build_outing_message(amap, settings, state, today, event_sources, record, name, greeting, events,
                                    prefs, remember)
    # Real-time reminders (cold snap, heating, water cuts...) on every outing message, incl. stay-home.
    return add_notices(message, notice_lines(events, today, "mother"))


def _build_outing_message(
    amap: AmapClient,
    settings: Settings,
    state: StateStore,
    today: date,
    event_sources: Iterable[EventSource] | None,
    record: bool,
    name: str | None,
    greeting: str,
    events: EventFinder | None,
    prefs: PreferenceStore | None = None,
    remember: bool = False,
) -> Message:
    name = settings.mother.display_name if name is None else name
    info = day_info(today)
    try:
        live = amap.get_live_weather(settings.city_adcode)
    except (AmapError, requests.RequestException) as exc:
        log.warning("AMap weather unavailable, continuing without it: %s", exc)
        live = None
    forecast = amap.get_today_forecast(settings.city_adcode)
    advice = weather_advice(live, forecast)
    if advice == "home":
        return format_stay_home_message(name, live, forecast, greeting)
    max_minutes = prefs.max_route_minutes() if prefs is not None else None

    def remember_items(items: list[dict]) -> None:
        if remember and prefs is not None:
            prefs.remember_recommendations(items)

    if events is not None:
        try:
            found = events.find(today)
        except (LLMError, requests.RequestException, ValueError, OSError) as exc:
            log.warning("event lookup failed; falling back to places: %s", exc)
            found = []
        ranked = rank_events(found, today, info, prefs)
        picks = pick_events(amap, settings, ranked, advice, prefs=prefs, max_minutes=max_minutes)
        if picks:
            remember_items([{"title": p.event.title, "venue": p.poi.name, "category": p.event.category} for p in picks])
            return format_events_message(
                name=name, greeting=greeting, picks=picks, live=live, today=forecast,
                home_coords=settings.mother_home.coords, home_name=settings.mother_home.name,
                heading=events_heading(info),
            )

    sources = default_event_sources(settings) if event_sources is None else event_sources
    candidates = order_candidates(_gather_places(sources), today, advice, state.recent(STATE_KEY), prefs)
    home = settings.mother_home
    map_failures = 0

    for place in candidates[:MAX_CANDIDATES_TRIED]:
        try:
            poi = amap.search_poi(place.search_keyword or place.name, settings.city_name)
            if poi is None:
                log.info("POI not found: %s", place.name)
                continue
            plans: list[TransitPlan] = []
            for strategy in (2, 3):  # fewest transfers, least walking
                plans += amap.get_transit_routes(home.coords, poi.location, settings.city_name, strategy)
        except (AmapError, requests.RequestException) as exc:
            log.warning("AMap lookup failed for %s: %s", place.name, exc)
            map_failures += 1
            continue
        plan = pick_gentlest_plan(plans, settings.outing_max_transfers, settings.outing_max_walk_m)
        if plan is not None and max_minutes is not None and plan.duration_s > max_minutes * 60:
            plan = None
        if plan is None:
            log.info("no gentle route to %s", place.name)
            continue
        if record:
            state.remember(STATE_KEY, [place.name], keep=RECENT_KEEP)
        remember_items([{"title": place.name, "venue": poi.name or place.name, "category": place.category}])
        return format_outing_message(
            name=name, place=place, poi=poi, plan=plan, live=live, today=forecast, advice=advice,
            nav_url=amap_nav_url(home.coords, home.name, poi.location, poi.name or place.name, "bus"),
            greeting=greeting,
        )
    if candidates and map_failures:
        # The map service itself is failing (not just "no gentle route"): still suggest a classic place.
        place = candidates[0]
        remember_items([{"title": place.name, "venue": place.name, "category": place.category}])
        return format_place_without_route(name, place, settings.city_name, live, forecast, greeting)
    return format_no_route_message(name, live, forecast, greeting)
