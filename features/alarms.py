"""Alarms: catch silent degradation (a feed with no article text, a job that never ran) — adapted from nari.

* Checks are pure functions over the activity log, the article feed and the host's /proc + disk numbers;
  each returns ``Alarm(key, firing, desc, severity)`` for every rule (firing or not), so recovery is seen.
* ``AlarmEngine.apply`` compares with the stored state (``data/state.json`` → "alarms") and notifies only on
  a change: 🚨 fired, ⏫ escalated (warning → critical), ✅ recovered (with how long it lasted).
* ``remind`` sends one daily summary while anything is still firing, so nothing is forgotten.
* Delivery is the caller's ``notify(title, lines)`` (PushPlus to you, WeCom as backup). A dead zcare can't
  alarm: the host watchdog (deploy/watchdog.sh) covers that.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from storage import StateStore

log = logging.getLogger(__name__)

CRITICAL, WARNING = "critical", "warning"
STATE_KEY = "alarms"
MISSED_GRACE_S = 30 * 60
FEED_STALE_DAYS = 2  # ~13 articles a day normally: none yesterday AND none today means fetching stopped
TEXT_MISSING_SHARE = 0.8
TEXT_MIN_ARTICLES = 5
TEXT_MIN_CHARS = 10
LLM_WINDOW_S = 6 * 3600
LLM_MIN_CALLS = 3
BACKUP_MAX_AGE_D = 2
DISK_CRITICAL_PCT = 85
MEM_MIN_AVAILABLE_PCT = 10

JOB_CN = {"commute": "爸爸早间路况", "outing": "妈妈出行推荐", "tv": "周五好剧", "digest": "晚间本地新闻",
          "daily_report": "每日日报", "backup": "每日备份"}


@dataclass(frozen=True)
class Alarm:
    key: str
    firing: bool
    desc: str
    severity: str = CRITICAL


# ------------------------------------------------------------------ checks


def job_alarms(entries: list[dict[str, Any]], jobs: Iterable[str]) -> list[Alarm]:
    """The LAST run of each scheduled job failed or had to send the backup greeting."""
    out = []
    for job in jobs:
        runs = [e for e in entries if e.get("type") == "sent" and e.get("job") == job]
        last = runs[-1] if runs else None
        bad = bool(last) and (not last.get("ok") or bool(last.get("fallback")))
        what = "发送失败" if last and not last.get("ok") else "只发出了备用问候（内容没生成出来）"
        out.append(Alarm(f"job:{job}", bad, f"{JOB_CN.get(job, job)}{what}"))
    return out


def missed_alarms(entries: list[dict[str, Any]], schedules: dict[str, Any], started_at: float,
                  now: float) -> list[Alarm]:
    """A scheduled time passed (+ grace) without the job running. ``schedules``: job → APScheduler trigger."""
    out = []
    for job, trigger in schedules.items():
        last = max((e["ts"] for e in entries if e.get("type") == "sent" and e.get("job") == job), default=0.0)
        baseline = max(last, started_at)
        tz = getattr(trigger, "timezone", None)
        due = trigger.get_next_fire_time(None, datetime.fromtimestamp(baseline, tz))
        missed = due is not None and due.timestamp() < now - MISSED_GRACE_S
        out.append(Alarm(f"missed:{job}", missed,
                         f"{JOB_CN.get(job, job)}应在 {due:%m-%d %H:%M} 发送，但一直没有运行" if missed else f"{job} on schedule"))
    return out


def feed_alarms(entries: list[dict[str, Any]], articles: list[Any] | None, now: float) -> list[Alarm]:
    """we-mp-rss: reachable, fresh, and actually delivering article TEXT (not just headlines)."""
    extracts = [e for e in entries if e.get("type") == "extract"]
    down = articles is None or bool(extracts and extracts[-1].get("feed_error"))
    out = [Alarm("feed:down", down, "公众号文章源（we-mp-rss）连不上，活动、提醒和晚间新闻都会受影响")]
    arts = articles or []
    dates = [a.published for a in arts if getattr(a, "published", None)]
    newest = max(dates) if dates else None
    today = datetime.fromtimestamp(now).date()
    stale = not down and (newest is None or (today - newest).days >= FEED_STALE_DAYS)
    out.append(Alarm("feed:stale", stale, f"公众号{'一直没有文章' if newest is None else f'从 {newest:%m-%d} 起'}没有新文章："
                     "可能是 we-mp-rss 登录过期或者抓取停了（企业微信回复「扫码」重新授权）"))
    recent = sorted(arts, key=lambda a: a.published or datetime.min.date(), reverse=True)[:30]
    empty = sum(1 for a in recent if len(getattr(a, "content", "") or "") < TEXT_MIN_CHARS)
    missing = len(recent) >= TEXT_MIN_ARTICLES and empty / len(recent) >= TEXT_MISSING_SHARE
    out.append(Alarm("feed:no_text", missing,
                     f"公众号文章没有正文（最近 {len(recent)} 篇里 {empty} 篇只有标题）：活动和提醒提取不到，"
                     "晚间新闻只有标题。看看 we-mp-rss 日志里的「获取文章内容失败」"))
    return out


def delivery_alarms(entries: list[dict[str, Any]], now: float) -> list[Alarm]:
    errors = [e for e in entries if e.get("type") == "error" and e["ts"] > now - 86400]
    pushplus = [e for e in errors if "PushPlus" in str(e.get("what", ""))]
    claw = [e for e in errors if str(e.get("what", "")) == "ClawBot" and e["ts"] > now - 6 * 3600]
    return [
        Alarm("delivery:pushplus", bool(pushplus),
              f"PushPlus 推送失败 {len(pushplus)} 次（24 小时内）：爸妈可能没收到消息。最近一次："
              f"{str(pushplus[-1].get('error', ''))[:80] if pushplus else ''}"),
        Alarm("chat:clawbot", bool(claw), f"ClawBot 聊天出问题：{str(claw[-1].get('error', ''))[:80] if claw else ''}",
              WARNING),
    ]


def llm_alarm(entries: list[dict[str, Any]], now: float) -> Alarm:
    calls = [e for e in entries if e.get("type") == "llm" and e["ts"] > now - LLM_WINDOW_S]
    failed = sum(1 for e in calls if not e.get("ok"))
    bad = len(calls) >= LLM_MIN_CALLS and failed * 2 >= len(calls)
    return Alarm("llm:failing", bad, f"GLM 最近 6 小时 {len(calls)} 次调用失败了 {failed} 次（聊天会用离线回复）", WARNING)


def backup_alarm(data_dir: Path, now: float) -> Alarm:
    root = Path(data_dir) / "backups"
    days = sorted(p for p in root.iterdir() if p.is_dir() and p.name.isdigit()) if root.is_dir() else []
    age_d = (now - days[-1].stat().st_mtime) / 86400 if days else None
    bad = age_d is not None and age_d > BACKUP_MAX_AGE_D
    return Alarm("backup:old", bad, f"最新的数据备份已经 {age_d or 0:.0f} 天了（每天 03:45 应该备份一次）", WARNING)


def host_alarms(data_dir: Path, meminfo: Path = Path("/proc/meminfo")) -> list[Alarm]:
    st = os.statvfs(data_dir)
    disk_pct = 100 * (1 - st.f_bavail / st.f_blocks) if st.f_blocks else 0.0
    mem: dict[str, int] = {}
    try:
        for line in meminfo.read_text().splitlines():
            k, _, v = line.partition(":")
            mem[k] = int(v.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    avail_pct = 100 * mem["MemAvailable"] / mem["MemTotal"] if mem.get("MemTotal") and "MemAvailable" in mem else 100.0
    return [Alarm("host:disk", disk_pct >= DISK_CRITICAL_PCT, f"服务器磁盘已用 {disk_pct:.0f}%（≥ {DISK_CRITICAL_PCT}% 告警）"),
            Alarm("host:memory", avail_pct < MEM_MIN_AVAILABLE_PCT,
                  f"服务器可用内存只剩 {avail_pct:.0f}%（< {MEM_MIN_AVAILABLE_PCT}% 告警）", WARNING)]


# ------------------------------------------------------------------ engine


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m} 分钟" if m < 120 else f"{m // 60} 小时" if m < 48 * 60 else f"{m // 1440} 天"


class AlarmEngine:
    def __init__(self, state: StateStore, notify: Callable[[str, list[str]], None], clock=time.time):
        self._state = state
        self._notify = notify
        self._clock = clock

    def _all(self) -> dict[str, dict]:
        return dict(self._state.get(STATE_KEY) or {})

    def firing(self) -> dict[str, dict]:
        return {k: v for k, v in self._all().items() if v.get("firing")}

    def clear(self, keys: Iterable[str]) -> None:
        """Forget alarms whose rule was turned off — silently (no misleading "recovered" message)."""
        states = self._all()
        if any(states.pop(k, None) is not None for k in list(keys)):
            self._state.set(STATE_KEY, states)

    def apply(self, alarms: Iterable[Alarm]) -> list[str]:
        """Store the new states; notify fired / escalated / recovered alarms in one message."""
        now = self._clock()
        states = self._all()
        lines: list[str] = []
        worst = WARNING
        for a in alarms:
            st = states.get(a.key, {"firing": False})
            if a.firing and not st.get("firing"):
                st = {"firing": True, "since": now, "desc": a.desc, "severity": a.severity}
                lines.append(f"{'🚨' if a.severity == CRITICAL else '⚠️'} {a.desc}")
            elif a.firing:
                if a.severity == CRITICAL and st.get("severity") != CRITICAL:
                    lines.append(f"⏫ 升级为严重：{a.desc}")
                st.update(desc=a.desc, severity=a.severity)
            elif st.get("firing"):
                lines.append(f"✅ 已恢复：{st.get('desc', a.key)}（持续了 {_ago(now - st.get('since', now))}）")
                st = {"firing": False, "resolved": now}
            else:
                continue
            if a.firing and a.severity == CRITICAL:
                worst = CRITICAL
            states[a.key] = st
        self._state.set(STATE_KEY, states)
        if lines:
            recovered_only = all(line.startswith("✅") for line in lines)
            title = "zcare 已恢复" if recovered_only else ("zcare 告警" if worst == CRITICAL else "zcare 提醒")
            first = lines[0].lstrip("🚨⚠️⏫✅ ").removeprefix("已恢复：").removeprefix("升级为严重：")
            self._safe_notify(f"{title}：{first}", lines)
        return lines

    def remind(self) -> list[str]:
        """Daily: everything still firing, oldest first."""
        firing = sorted(self.firing().values(), key=lambda s: s.get("since", 0))
        if not firing:
            return []
        now = self._clock()
        lines = [f"{'🚨' if s.get('severity') == CRITICAL else '⚠️'} {s.get('desc')}（已经 {_ago(now - s.get('since', now))}）"
                 for s in firing]
        self._safe_notify(f"zcare 仍有 {len(firing)} 个问题没解决", lines)
        return lines

    def _safe_notify(self, title: str, lines: list[str]) -> None:
        try:
            self._notify(title, lines)
        except Exception:  # noqa: BLE001 — the alarm engine must never raise into the scheduler
            log.exception("alarm notification failed")
