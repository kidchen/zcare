"""Official weather warnings from the National Meteorological Center (中央气象台, nmc.cn).

List:   GET http://www.nmc.cn/rest/findAlarm?pageNo=1&pageSize=100&province=内蒙古自治区
        → data.page.list[] = {alertid, issuetime "2026/10/12 07:09", title, url, pic}
Detail: GET http://www.nmc.cn{url} → HTML whose text contains
        "…气象台2026年10月12日07时08分发布大风蓝色预警信号：24小时内…将受大风影响，平均风力可达6级以上…"

Authoritative and structured: no LLM extraction, nothing to "verify".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

import requests

from clients.http import DESKTOP_UA, build_session

log = logging.getLogger(__name__)

BASE_URL = "http://www.nmc.cn"
_SIGNAL = re.compile(r"发布(?P<signal>[^，。：:]{2,12}?预警信号)")
_DETAIL = re.compile(r"预警信号[：:](?P<text>[^。]{6,120}。)")
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class WeatherWarning:
    title: str  # e.g. 内蒙古自治区呼和浩特市气象台发布大风蓝色预警信号
    signal: str  # e.g. 大风蓝色预警信号
    issued: datetime
    detail: str  # first sentence of the official text, e.g. 24小时内…平均风力可达6级以上…。
    url: str
    lifted: bool  # 解除 (cancellation) notices


def parse_issuetime(raw: str) -> datetime | None:
    for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def detail_sentence(html: str) -> str:
    text = _WS.sub("", _TAGS.sub(" ", html))
    m = _DETAIL.search(text)
    return m.group("text") if m else ""


class NmcClient:
    def __init__(self, province: str = "内蒙古自治区", city_keyword: str = "呼和浩特",
                 session: requests.Session | None = None, timeout: float = 10.0):
        self._province = province
        self._city = city_keyword
        self._session = session or build_session(DESKTOP_UA)
        self._timeout = timeout

    def warnings(self) -> list[WeatherWarning]:
        """Current warnings whose title names the city (incl. its counties, e.g. 呼和浩特市武川县)."""
        resp = self._session.get(
            f"{BASE_URL}/rest/findAlarm",
            params={"pageNo": 1, "pageSize": 100, "signaltype": "", "signallevel": "", "province": self._province},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        items = ((resp.json().get("data") or {}).get("page") or {}).get("list") or []
        result: list[WeatherWarning] = []
        for item in items:
            title = str(item.get("title", ""))
            issued = parse_issuetime(str(item.get("issuetime", "")))
            signal = _SIGNAL.search(title)
            if self._city not in title or issued is None or not signal:
                continue
            url = BASE_URL + str(item.get("url", ""))
            detail = ""
            try:
                page = self._session.get(url, timeout=self._timeout)
                page.raise_for_status()
                page.encoding = "utf-8"
                detail = detail_sentence(page.text)
            except requests.RequestException as exc:
                log.info("warning detail unavailable (%s): %s", url, exc)
            result.append(WeatherWarning(title, signal.group("signal"), issued, detail, url, "解除" in title))
        return result
