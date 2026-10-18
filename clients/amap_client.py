"""AutoNavi / 高德 Web Service (REST) API wrapper.

Docs: https://lbs.amap.com/api/webservice/summary
All coordinates are GCJ-02 "lng,lat" strings (longitude first).

AMap v3 quirk: empty string fields are often returned as ``[]`` instead of "".
The ``_s``/``_i`` helpers normalise that so parsing never crashes on it.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from clients.http import build_session

log = logging.getLogger(__name__)

BASE_URL = "https://restapi.amap.com"

# Traffic-message-channel status → severity (higher is worse).
TMC_SEVERITY = {"未知": 0, "畅通": 0, "缓行": 1, "拥堵": 2, "严重拥堵": 3}


class AmapError(RuntimeError):
    def __init__(self, info: str, infocode: str = ""):
        super().__init__(f"AMap error {infocode}: {info}")
        self.info = info
        self.infocode = infocode


def _s(value: Any) -> str:
    if value is None or isinstance(value, (list, dict)):
        return ""
    return str(value)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(_s(value)))
    except ValueError:
        return default


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(_s(value))
    except ValueError:
        return default


# ---------------------------------------------------------------- data models


@dataclass(frozen=True)
class CongestedRoad:
    road: str
    status: str  # worst status seen on this road: 缓行 / 拥堵 / 严重拥堵
    distance_m: int  # total slow/jammed distance on this road


@dataclass(frozen=True)
class DrivingRoute:
    distance_m: int
    duration_s: int  # traffic-aware ETA
    traffic_lights: int
    main_roads: tuple[str, ...]  # longest roads, in driving order
    congested: tuple[CongestedRoad, ...]  # worst first

    @property
    def worst_severity(self) -> int:
        return max((TMC_SEVERITY.get(c.status, 0) for c in self.congested), default=0)

    @property
    def congested_distance_m(self) -> int:
        return sum(c.distance_m for c in self.congested)


@dataclass(frozen=True)
class TransitLeg:
    mode: str  # walk | bus | subway | railway
    distance_m: int
    duration_s: int
    line_name: str = ""
    departure_stop: str = ""
    arrival_stop: str = ""
    stop_count: int = 0  # number of stops ridden


@dataclass(frozen=True)
class TransitPlan:
    duration_s: int
    walking_m: int
    cost_yuan: float
    legs: tuple[TransitLeg, ...]

    @property
    def rides(self) -> int:
        return sum(1 for leg in self.legs if leg.mode != "walk")

    @property
    def transfers(self) -> int:
        return max(0, self.rides - 1)


@dataclass(frozen=True)
class LiveWeather:
    weather: str  # 晴 / 多云 / 小雨 / 浮尘 ...
    temperature_c: int
    wind_direction: str
    wind_power: str  # e.g. "≤3", "4"
    humidity: int
    report_time: str


@dataclass(frozen=True)
class DailyForecast:
    date: str
    day_weather: str
    night_weather: str
    day_temp_c: int
    night_temp_c: int
    day_wind_power: str


@dataclass(frozen=True)
class Poi:
    name: str
    address: str
    location: str  # "lng,lat"
    type: str
    photo: str = ""  # public AMap photo URL, if any
    distance_m: int = 0  # straight-line distance from the search centre (nearby search only)


# --------------------------------------------------------------------- parsers

_REGION_PREFIX = re.compile(r"^(内蒙古自治区|内蒙古|呼和浩特市|呼和浩特|呼市)")
_NAME_NOISE = re.compile(r"[\s（）()·\-—_]+")
INDOOR_TYPE_HINTS = ("科教文化", "影剧院", "运动场馆", "图书馆", "博物馆", "美术馆", "展览馆", "文化宫")


def _core_name(name: str) -> str:
    return _REGION_PREFIX.sub("", _NAME_NOISE.sub("", name))


def _is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def poi_name_matches(query: str, poi_name: str) -> bool:
    """Guard against AMap's fuzzy top hit being a different place.

    AMap returns *something* for almost any keyword: "呼和浩特市文化馆" → 将军衙署博物院,
    "呼和浩特博物馆" → 内蒙古博物院. We accept a hit only if, after dropping region prefixes,
    one name's characters appear in order within the other (大召广场 ⊂ 大召无量寺大召广场).
    """
    fq, fp = _NAME_NOISE.sub("", query), _NAME_NOISE.sub("", poi_name)
    if len(fq) >= 2 and len(fp) >= 2 and (_is_subsequence(fq, fp) or _is_subsequence(fp, fq)):
        return True
    # Prefix-stripped comparison only for distinctive names: "博物院" alone would match the
    # wrong museum (内蒙古博物院 vs 呼和浩特博物院).
    q, p = _core_name(query), _core_name(poi_name)
    if len(q) < 4 or len(p) < 4:
        return False
    return _is_subsequence(q, p) or _is_subsequence(p, q)


def _photo(p: dict[str, Any]) -> str:
    """First AMap photo URL, upgraded to https (push pages are https; http images get blocked)."""
    photos = p.get("photos") if isinstance(p.get("photos"), list) else []
    photo = next((_s(ph.get("url")) for ph in photos if isinstance(ph, dict) and _s(ph.get("url"))), "")
    return "https://" + photo[len("http://"):] if photo.startswith("http://") else photo


def poi_is_indoor(poi_type: str) -> bool:
    return any(h in poi_type for h in INDOOR_TYPE_HINTS)



def parse_driving_path(path: dict[str, Any]) -> DrivingRoute:
    road_distance: dict[str, int] = {}
    jams: dict[str, dict[str, Any]] = {}
    for step in path.get("steps") or []:
        road = _s(step.get("road"))
        if road:
            road_distance[road] = road_distance.get(road, 0) + _i(step.get("distance"))
        for tmc in step.get("tmcs") or []:
            status = _s(tmc.get("status"))
            if TMC_SEVERITY.get(status, 0) == 0:
                continue
            entry = jams.setdefault(road or "无名道路", {"status": status, "distance": 0})
            entry["distance"] += _i(tmc.get("distance"))
            if TMC_SEVERITY[status] > TMC_SEVERITY[entry["status"]]:
                entry["status"] = status

    # Top-3 roads by length, kept in the order they're driven (dict preserves order).
    longest = set(sorted(road_distance, key=road_distance.get, reverse=True)[:3])
    main_roads = tuple(r for r in road_distance if r in longest)

    congested = tuple(
        sorted(
            (CongestedRoad(road, j["status"], j["distance"]) for road, j in jams.items()),
            key=lambda c: (-TMC_SEVERITY[c.status], -c.distance_m),
        )
    )
    return DrivingRoute(
        distance_m=_i(path.get("distance")),
        duration_s=_i(path.get("duration")),
        traffic_lights=_i(path.get("traffic_lights")),
        main_roads=main_roads,
        congested=congested,
    )


def parse_transit(transit: dict[str, Any]) -> TransitPlan:
    legs: list[TransitLeg] = []
    for seg in transit.get("segments") or []:
        walking = seg.get("walking")
        if isinstance(walking, dict) and _i(walking.get("distance")) > 0:
            legs.append(TransitLeg("walk", _i(walking.get("distance")), _i(walking.get("duration"))))

        bus = seg.get("bus")
        buslines = (bus.get("buslines") if isinstance(bus, dict) else None) or []
        if buslines:
            line = buslines[0]  # AMap lists equivalent alternatives; first is recommended
            mode = "subway" if "地铁" in _s(line.get("type")) else "bus"
            legs.append(
                TransitLeg(
                    mode=mode,
                    distance_m=_i(line.get("distance")),
                    duration_s=_i(line.get("duration")),
                    line_name=_s(line.get("name")),
                    departure_stop=_s((line.get("departure_stop") or {}).get("name")),
                    arrival_stop=_s((line.get("arrival_stop") or {}).get("name")),
                    stop_count=_i(line.get("via_num")) + 1,
                )
            )

        railway = seg.get("railway")
        if isinstance(railway, dict) and _s(railway.get("name")):
            legs.append(
                TransitLeg(
                    mode="railway",
                    distance_m=_i(railway.get("distance")),
                    duration_s=_i(railway.get("time")),
                    line_name=_s(railway.get("name")),
                    departure_stop=_s((railway.get("departure_stop") or {}).get("name")),
                    arrival_stop=_s((railway.get("arrival_stop") or {}).get("name")),
                )
            )
    return TransitPlan(
        duration_s=_i(transit.get("duration")),
        walking_m=_i(transit.get("walking_distance")),
        cost_yuan=_f(transit.get("cost")),
        legs=tuple(legs),
    )


# ---------------------------------------------------------------------- client


MIN_INTERVAL_S = 0.34  # personal Web服务 keys allow only a few requests/second (else infocode 10021)
QPS_LIMIT_CODES = ("10021", "10014", "10015")  # per-key / per-service / per-IP QPS exceeded
QPS_RETRY_WAIT_S = 1.2


class AmapClient:
    _lock = threading.Lock()  # shared: scheduled jobs and chat requests use the same key
    _last_call = 0.0

    def __init__(self, api_key: str, session: requests.Session | None = None, timeout: float = 8.0,
                 min_interval_s: float | None = None, sleep=time.sleep, clock=time.monotonic):
        if not api_key:
            raise ValueError("AMap API key is required")
        self._key = api_key
        self._session = session or build_session()
        self._timeout = timeout
        self._min_interval = min_interval_s  # None → MIN_INTERVAL_S (read at call time; tests set it to 0)
        self._sleep, self._clock = sleep, clock

    def _pace(self) -> None:
        with AmapClient._lock:
            gap = MIN_INTERVAL_S if self._min_interval is None else self._min_interval
            wait = AmapClient._last_call + gap - self._clock()
            if wait > 0:
                self._sleep(wait)
            AmapClient._last_call = self._clock()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {**params, "key": self._key, "output": "JSON"}
        for attempt in (1, 2):
            self._pace()
            resp = self._session.get(f"{BASE_URL}{path}", params=query, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            if str(data.get("status")) == "1":
                return data
            code = _s(data.get("infocode"))
            if code in QPS_LIMIT_CODES and attempt == 1:
                log.info("AMap QPS limit (%s) on %s; retrying once", code, path)
                self._sleep(QPS_RETRY_WAIT_S)
                continue
            raise AmapError(_s(data.get("info")) or "unknown", code)
        raise AssertionError("unreachable")

    # -- driving -------------------------------------------------------------

    def get_driving_route(self, origin: str, destination: str, strategy: int = 10) -> DrivingRoute:
        """Traffic-aware driving route. strategy=10: avoid jams, AMap's recommended default."""
        data = self._get(
            "/v3/direction/driving",
            {"origin": origin, "destination": destination, "strategy": strategy, "extensions": "all"},
        )
        paths = (data.get("route") or {}).get("paths") or []
        if not paths:
            raise AmapError("no driving path returned")
        return parse_driving_path(paths[0])

    # -- transit -------------------------------------------------------------

    def get_transit_routes(
        self, origin: str, destination: str, city: str, strategy: int = 2
    ) -> list[TransitPlan]:
        """Bus/subway plans. strategy: 0 fastest, 1 cheapest, 2 fewest transfers,
        3 least walking, 5 no subway."""
        data = self._get(
            "/v3/direction/transit/integrated",
            {
                "origin": origin,
                "destination": destination,
                "city": city,
                "cityd": city,
                "strategy": strategy,
                "nightflag": 0,
            },
        )
        transits = (data.get("route") or {}).get("transits") or []
        return [parse_transit(t) for t in transits]

    # -- weather -------------------------------------------------------------

    def get_live_weather(self, adcode: str) -> LiveWeather:
        data = self._get("/v3/weather/weatherInfo", {"city": adcode, "extensions": "base"})
        lives = data.get("lives") or []
        if not lives:
            raise AmapError(f"no live weather for {adcode}")
        live = lives[0]
        return LiveWeather(
            weather=_s(live.get("weather")),
            temperature_c=_i(live.get("temperature")),
            wind_direction=_s(live.get("winddirection")),
            wind_power=_s(live.get("windpower")),
            humidity=_i(live.get("humidity")),
            report_time=_s(live.get("reporttime")),
        )

    def get_forecast(self, adcode: str) -> list[DailyForecast]:
        data = self._get("/v3/weather/weatherInfo", {"city": adcode, "extensions": "all"})
        forecasts = data.get("forecasts") or []
        casts = forecasts[0].get("casts") if forecasts else []
        return [
            DailyForecast(
                date=_s(c.get("date")),
                day_weather=_s(c.get("dayweather")),
                night_weather=_s(c.get("nightweather")),
                day_temp_c=_i(c.get("daytemp")),
                night_temp_c=_i(c.get("nighttemp")),
                day_wind_power=_s(c.get("daypower")),
            )
            for c in casts or []
        ]

    def get_today_forecast(self, adcode: str) -> DailyForecast | None:
        """Best-effort: forecast failure shouldn't kill the whole message."""
        try:
            casts = self.get_forecast(adcode)
        except (AmapError, requests.RequestException) as exc:
            log.warning("forecast unavailable: %s", exc)
            return None
        return casts[0] if casts else None

    # -- places --------------------------------------------------------------

    def search_around(self, location: str, types: str, radius_m: int = 2500, limit: int = 20) -> list[Poi]:
        """POIs of AMap ``types`` (e.g. 110101 公园 | 110105 城市广场) within ``radius_m``, nearest first."""
        data = self._get(
            "/v3/place/around",
            {"location": location, "types": types, "radius": radius_m, "sortrule": "distance",
             "offset": min(limit, 25), "page": 1, "extensions": "all"},
        )
        out = []
        for p in data.get("pois") or []:
            if not isinstance(p, dict) or not _s(p.get("location")) or not _s(p.get("name")):
                continue
            out.append(Poi(_s(p.get("name")), _s(p.get("address")), _s(p.get("location")), _s(p.get("type")),
                           _photo(p), _i(p.get("distance"))))
        return out

    def search_poi(self, keywords: str, city: str, strict: bool = True) -> Poi | None:
        """Keyword POI search restricted to ``city``. With ``strict`` (default) the top hit is
        returned only if its name actually matches ``keywords`` (see poi_name_matches)."""
        data = self._get(
            "/v3/place/text",
            {"keywords": keywords, "city": city, "citylimit": "true", "offset": 1, "page": 1,
             "extensions": "all"},  # "all" includes photos
        )
        pois = data.get("pois") or []
        if not pois:
            return None
        p = pois[0]
        location = _s(p.get("location"))
        if not location:
            return None
        if strict and not poi_name_matches(keywords, _s(p.get("name"))):
            log.info("AMap top hit %r does not match %r; ignoring", _s(p.get("name")), keywords)
            return None
        return Poi(_s(p.get("name")), _s(p.get("address")), location, _s(p.get("type")), _photo(p))
