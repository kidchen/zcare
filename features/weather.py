"""On-demand weather ("这周天气咋样", "明天冷不冷"): current conditions + the next few days, official
warnings and a clothing tip. No traffic. AMap's forecast covers today + 3 days, so we say "这几天"
instead of pretending to know the whole week.
"""
from __future__ import annotations

import logging
from datetime import date

import requests

from clients.amap_client import AmapClient, AmapError, DailyForecast, LiveWeather
from config import Settings
from features.commute import NO_WIND_DIRECTION
from features.events import EventFinder
from messages import Message, add_notices

log = logging.getLogger(__name__)
WEEKDAYS = "一二三四五六日"


def _day_label(d: str, today: date) -> str:
    try:
        day = date.fromisoformat(d)
    except ValueError:
        return d
    offset = (day - today).days
    rel = {0: "今天", 1: "明天", 2: "后天"}.get(offset, "")
    return f"{rel}（周{WEEKDAYS[day.weekday()]}）" if rel else f"周{WEEKDAYS[day.weekday()]}（{day.month}月{day.day}日）"


def _sky(day: DailyForecast) -> str:
    return day.day_weather if day.day_weather == day.night_weather else f"{day.day_weather}转{day.night_weather}"


def clothing_tips(live: LiveWeather | None, days: list[DailyForecast]) -> list[str]:
    tips: list[str] = []
    if days:
        low = min(d.night_temp_c for d in days)
        swing = max(d.day_temp_c - d.night_temp_c for d in days)
        sky = "".join(d.day_weather + d.night_weather for d in days)
        if low <= 0:
            tips.append(f"早晚最低到 {low}°C，出门穿厚外套，戴好帽子手套")
        elif low <= 8:
            tips.append(f"早晚只有 {low}°C 左右，记得加件外套")
        if swing >= 12:
            tips.append(f"白天和晚上差 {swing}°C 左右，穿能脱能穿的衣服，别着凉")
        if "雨" in sky:
            tips.append("这几天有雨，出门带把伞，路滑慢慢走")
        if "雪" in sky or "冰" in sky:
            tips.append("可能下雪结冰，尽量少出门，出门穿防滑鞋")
        if any(k in sky for k in ("沙", "尘", "霾")):
            tips.append("空气不太好，出门戴口罩，回家洗洗脸")
    if not tips:
        tips.append("天气还不错，适合出门走走，注意多喝水")
    return tips


def format_weather_message(name: str, greeting: str, live: LiveWeather | None, days: list[DailyForecast],
                           today: date) -> Message:
    lines = [f"**{name}{greeting} 🌤 这几天的天气**"]
    if live is not None:
        wind = (f"风力 {live.wind_power} 级" if live.wind_direction in NO_WIND_DIRECTION
                else f"{live.wind_direction}风 {live.wind_power} 级")
        lines.append(f"> 现在：{live.weather} {live.temperature_c}°C，{wind}，湿度 {live.humidity}%")
    if days:
        lines += ["", "**未来几天**"]
        lines += [f"- {_day_label(d.date, today)}：{_sky(d)}，{d.night_temp_c}~{d.day_temp_c}°C，风力 {d.day_wind_power} 级"
                  for d in days]
    if live is None and not days:
        lines += ["", "天气暂时没查到，可以看看手机上的天气预报，出门多带件外套。"]
    lines += ["", "**💡 穿衣出行**", *[f"- {t}" for t in clothing_tips(live, days)]]
    title = f"{name}{greeting} · 这几天的天气"
    return Message(title=title, markdown="\n".join(lines))


def build_weather_message(amap: AmapClient, settings: Settings, today: date, name: str = "您",
                          greeting: str = "你好", events: EventFinder | None = None,
                          audience: str = "mother") -> Message:
    """Never raises for map/network problems."""
    try:
        live = amap.get_live_weather(settings.city_adcode)
    except (AmapError, requests.RequestException) as exc:
        log.warning("AMap weather unavailable: %s", exc)
        live = None
    try:
        days = amap.get_forecast(settings.city_adcode)
    except (AmapError, requests.RequestException) as exc:
        log.warning("AMap forecast unavailable: %s", exc)
        days = []
    message = format_weather_message(name, greeting, live, days, today)
    warnings: list[str] = []
    if events is not None:
        try:  # official NMC warnings only — weather questions shouldn't bring traffic or heating notices
            warnings = [n.line(today) for n in events.notices(today, audience) if n.category == "天气"][:3]
        except Exception as exc:  # noqa: BLE001
            log.warning("weather warnings unavailable: %s", exc)
    return add_notices(message, warnings, heading="⚠️ 官方天气预警")
