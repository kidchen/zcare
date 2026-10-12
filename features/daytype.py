"""Workday / weekend / public-holiday classification for mainland China (incl. 调休 make-up days).

Uses the `chinesecalendar` package (official State Council schedule). Years it doesn't know yet
(the next year's schedule is usually published in November) fall back to a plain Mon–Fri rule —
update the package when the new calendar is out.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

log = logging.getLogger(__name__)

HOLIDAY_CN = {
    "New Year's Day": "元旦",
    "Spring Festival": "春节",
    "Tomb-sweeping Day": "清明节",
    "Labour Day": "劳动节",
    "Dragon Boat Festival": "端午节",
    "Mid-autumn Festival": "中秋节",
    "National Day": "国庆节",
    "Anti-Fascist 70th Day": "抗战胜利纪念日",
}
WEEKDAYS = "一二三四五六日"
_warned_years: set[int] = set()


@dataclass(frozen=True)
class DayInfo:
    day: date
    workday: bool
    holiday: str = ""  # Chinese holiday name on public-holiday days off, else ""

    @property
    def kind(self) -> str:
        if self.workday:
            return "workday"
        return "holiday" if self.holiday else "weekend"

    @property
    def label(self) -> str:
        """Short Chinese label for prompts/headers: 工作日 / 周末 / 国庆节假期 / 调休上班."""
        if self.workday:
            return "调休上班" if self.day.weekday() >= 5 else "工作日"
        return f"{self.holiday}假期" if self.holiday else "周末"

    @property
    def weekend_mode(self) -> bool:
        """Friday previews + every day off: bias outings toward leisure / weekend events."""
        return not self.workday or self.day.weekday() == 4


def day_info(d: date) -> DayInfo:
    try:
        import chinese_calendar as cc

        workday = cc.is_workday(d)
        _, name = cc.get_holiday_detail(d)
        holiday = HOLIDAY_CN.get(str(name), str(name or "")) if (name and not workday) else ""
        return DayInfo(d, workday, holiday)
    except (NotImplementedError, ImportError) as exc:
        if d.year not in _warned_years:
            _warned_years.add(d.year)
            log.warning("no official holiday calendar for %s (%s); using Mon–Fri rule — update chinesecalendar",
                        d.year, exc)
        return DayInfo(d, d.weekday() < 5)


def off_days(start: date, days: int = 7) -> set[date]:
    """Days off (weekends + public holidays, minus 调休 workdays) in [start, start+days]."""
    return {start + timedelta(i) for i in range(days + 1) if not day_info(start + timedelta(i)).workday}
