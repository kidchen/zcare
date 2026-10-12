"""Feature 1 — Dad's morning driving-traffic alert."""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from clients.amap_client import AmapClient, AmapError, DailyForecast, DrivingRoute, LiveWeather
from config import Settings
from features.daytype import DayInfo
from features.events import EventFinder, notice_lines
from messages import Article, Message, add_notices, amap_nav_url

log = logging.getLogger(__name__)

MIN_REPORTED_JAM_M = 200
NO_WIND_DIRECTION = {"", "无风向", "旋转不定"}


def _traffic_summary(route: DrivingRoute) -> tuple[str, str]:
    """Return (label, WeCom font color) for overall traffic."""
    if route.worst_severity >= 3 or route.congested_distance_m >= 2000:
        return "比较堵", "warning"
    if route.worst_severity >= 1:
        return "局部缓行", "comment"
    return "整体畅通", "info"


WEATHER_UNAVAILABLE = "暂时没查到，出门前看看天气预报"


def format_weather(live: LiveWeather | None, today: DailyForecast | None) -> str:
    if live is None:
        return (f"今天 {today.night_temp_c}~{today.day_temp_c}°C，{today.day_weather}" if today
                else WEATHER_UNAVAILABLE)
    if live.wind_direction in NO_WIND_DIRECTION:
        wind = f"风力 {live.wind_power} 级"
    else:
        wind = f"{live.wind_direction}风 {live.wind_power} 级"
    line = f"{live.weather} {live.temperature_c}°C，{wind}"
    if today:
        line += f"，今天 {today.night_temp_c}~{today.day_temp_c}°C"
    return line


def driving_tip(live: LiveWeather | None, today: DailyForecast | None) -> str:
    if live is None:
        return "路上慢点开，注意安全～"
    sky = live.weather + (today.day_weather if today else "")
    if "雪" in sky or "冰" in sky:
        return "路面可能有积雪结冰，请减速慢行、保持车距。"
    if "雨" in sky:
        return "雨天路滑，慢点开，留意行人。"
    if any(k in sky for k in ("雾", "霾", "沙", "尘")):
        return "能见度可能较低，记得开车灯、减速慢行。"
    if live.temperature_c <= -10:
        return "天很冷，出门前热热车，穿暖和些。"
    return "路上慢点开，注意安全～"


def format_commute_message(
    *,
    name: str,
    route: DrivingRoute,
    live: LiveWeather,
    today: DailyForecast | None,
    typical_minutes: int = 0,
    nav_url: str | None = None,
    greeting: str = "早上好",
) -> Message:
    minutes = max(1, round(route.duration_s / 60))
    label, color = _traffic_summary(route)

    lines = [
        f"**{name}{greeting} ☀️**",
        f'> 今日路况：<font color="{color}">{label}</font>',
        f"> 预计车程：**{minutes} 分钟**（{route.distance_m / 1000:.1f} 公里）",
    ]
    if typical_minutes:
        delta = minutes - typical_minutes
        if delta >= 3:
            lines.append(f"> 比平时慢约 {delta} 分钟，可以早点出门")
        elif delta <= -3:
            lines.append(f"> 比平时快约 {-delta} 分钟")
    if route.main_roads:
        lines.append(f"> 主要道路：{' → '.join(route.main_roads)}")
    for jam in route.congested[:3]:
        if jam.distance_m >= MIN_REPORTED_JAM_M:
            lines.append(f"> ⚠️ {jam.road}：{jam.status}约 {jam.distance_m / 1000:.1f} 公里")
    lines.append(f"> 天气：{format_weather(live, today)}")
    lines += ["", driving_tip(live, today)]
    if nav_url:
        lines.append(f"[📍 打开高德地图导航]({nav_url})")

    title = f"{name}{greeting} · 路况{label} · 约{minutes}分钟"
    # Card body: a short summary; the full text is on the detail page the card opens.
    summary = [f"{label}，约 {minutes} 分钟（{route.distance_m / 1000:.1f} 公里）"]
    jams = [j for j in route.congested if j.distance_m >= MIN_REPORTED_JAM_M]
    if jams:
        summary.append(f"⚠️ {jams[0].road}{jams[0].status}" + (f" 等 {len(jams)} 处" if len(jams) > 1 else ""))
    summary.append(f"天气：{live.weather} {live.temperature_c}°C" + (f"，{today.night_temp_c}~{today.day_temp_c}°C" if today else ""))
    summary.append("点开看完整路况和提醒 ›")
    articles = (Article(title, "\n".join(summary), nav_url),) if nav_url else ()
    return Message(title=title, markdown="\n".join(lines), url=nav_url, articles=articles)


def format_commute_unavailable(
    *, name: str, greeting: str, live: LiveWeather | None, today: DailyForecast | None, nav_url: str | None,
) -> Message:
    """Degraded mode: the map service didn't answer — still a warm greeting with weather and a button."""
    lines = [
        f"**{name}{greeting} ☀️**",
        "> 今天的实时路况暂时没查到，出门前可以点下面的按钮在高德里看一眼。",
        f"> 天气：{format_weather(live, today)}",
        "",
        driving_tip(live, today),
    ]
    if nav_url:
        lines.append(f"[📍 打开高德地图导航]({nav_url})")
    title = f"{name}{greeting} · 今日路况"
    article = (Article(title, f"实时路况暂时没查到\n天气：{format_weather(live, today)}\n点开看详情 ›", nav_url),
               ) if nav_url else ()
    return Message(title=title, markdown="\n".join(lines), url=nav_url, articles=article)


def _weather(amap: AmapClient, settings: Settings) -> tuple[LiveWeather | None, DailyForecast | None]:
    try:
        live = amap.get_live_weather(settings.city_adcode)
    except (AmapError, requests.RequestException) as exc:
        log.warning("AMap weather unavailable: %s", exc)
        live = None
    return live, amap.get_today_forecast(settings.city_adcode)


def build_commute_message(
    amap: AmapClient, settings: Settings, name: str | None = None, greeting: str = "早上好",
    events: EventFinder | None = None, today_date: date | None = None,
) -> Message:
    """Never raises for map/network problems: degrades to weather + reminders + an AMap button."""
    home, work = settings.father_home, settings.father_work
    name = settings.father.display_name if name is None else name
    nav_url = amap_nav_url(home.coords, home.name, work.coords, work.name, "car")
    today_date = today_date or datetime.now(ZoneInfo(settings.timezone)).date()
    live, today = _weather(amap, settings)
    try:
        route = amap.get_driving_route(home.coords, work.coords)
    except (AmapError, requests.RequestException) as exc:
        log.warning("AMap driving route unavailable: %s", exc)
        message = format_commute_unavailable(name=name, greeting=greeting, live=live, today=today, nav_url=nav_url)
    else:
        message = format_commute_message(
            name=name, greeting=greeting, route=route, live=live, today=today,
            typical_minutes=settings.commute_typical_minutes, nav_url=nav_url,
        )
    # Real-time local reminders for the driver: road closures, cold snaps, heating/water notices.
    return add_notices(message, notice_lines(events, today_date, "father"))


def build_offday_greeting(
    amap: AmapClient, settings: Settings, info: DayInfo, events: EventFinder | None = None,
) -> Message | None:
    """Weekends / public holidays: no commute report. Only a short greeting when there is something
    worth knowing (weather warning, heating / water / power notice) — traffic notices are dropped.
    Returns None (send nothing) on a quiet day off."""
    notices = []
    if events is not None:
        try:
            notices = [n.line(info.day) for n in events.notices(info.day, "father") if n.category != "交通"][:2]
        except Exception as exc:  # noqa: BLE001 — optional
            log.warning("notices unavailable on day off: %s", exc)
    if not notices:
        return None
    live, today = _weather(amap, settings)
    name = settings.father.display_name
    wish = f"{info.holiday}快乐！" if info.holiday else "周末愉快！"
    md = "\n".join([
        f"**{name}早上好 ☀️ {wish}**",
        f"> 天气：{format_weather(live, today)}",
        "",
        "今天不用上班，好好歇歇～",
    ])
    return add_notices(Message(title=f"{name}早上好 · {wish}", markdown=md), notices)
