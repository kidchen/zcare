"""FastAPI app + APScheduler daily jobs (Asia/Shanghai).

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000      # single worker only!
    python main.py preview commute                    # print a message, send nothing
    python main.py send outing                        # send one now

Run exactly ONE process: every uvicorn worker would start its own scheduler
and your parents would get duplicate messages.
"""
from __future__ import annotations

import argparse
import logging
import re
import secrets
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from bot import ChatBot, Command
from clients.amap_client import AmapClient
from clients.llm_client import LLMClient
from clients.nmc_client import NmcClient
from clients.wemp_admin import WeMpAdminClient, WeMpAdminError, is_image
from clients.wemp_rss import WeMpRssClient
from clients.wecom_callback import (
    CallbackFormatError,
    build_passive_reply,
    extract_encrypt,
    parse_message,
)
from clients.wecom_client import WeComClient
from clients.wecom_crypto import WeComCrypto, WeComCryptoError
from config import Settings, get_settings
from features.assistant import FamilyAssistant
from features.activity import ActivityLog
from features.commute import build_commute_message, build_offday_greeting
from features.daytype import day_info
from features.detail import DetailStore, render_page
from features.digest import DigestBuilder, format_digest_message
from features.events import EventFinder
from features.outing import build_outing_message
from features.report import build_daily_report
from features.preferences import FEEDBACK_KEYWORDS, PreferenceStore
from features.tv_recommend import PosterCache, build_tv_message
from features.weather import build_weather_message
from messages import Message, add_notices, markdown_to_text
from storage import StateStore

log = logging.getLogger("zcare")


@dataclass
class Services:
    settings: Settings
    amap: AmapClient
    wecom: WeComClient | None
    state: StateStore
    posters: PosterCache | None = None
    events: EventFinder | None = None  # WeChat-article events/notices + official weather warnings
    details: DetailStore | None = None  # H5 detail pages behind news cards
    prefs: PreferenceStore | None = None  # tastes learned from chat feedback
    activity: ActivityLog | None = None  # data/activity.jsonl → daily admin report

    def record(self, type_: str, **fields) -> None:
        if self.activity is not None:
            self.activity.record(type_, **fields)

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.timezone))


@lru_cache(maxsize=1)
def get_services() -> Services:
    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    wecom = None
    if s.wecom_corp_id and s.wecom_agent_id and s.wecom_secret:
        wecom = WeComClient(s.wecom_corp_id, s.wecom_agent_id, s.wecom_secret)
    posters = PosterCache(s.state_file.parent / "posters", s.public_base_url)
    state = StateStore(s.state_file)
    llm = get_llm()
    articles = WeMpRssClient(s.wemp_rss_url, s.wemp_rss_feeds) if s.wemp_rss_url else None
    warnings = NmcClient(s.nmc_province, s.nmc_city_keyword) if s.nmc_province else None
    # Events need articles + the (free) LLM for extraction; weather warnings need neither.
    events = (EventFinder(llm, state, articles=articles, warnings=warnings,
                          events_max_age_days=min(21, s.wemp_retention_days),
                          extract_store=StateStore(s.state_file.parent / "article_extracts.json"))
              if (articles or warnings) else None)
    details = DetailStore(s.state_file.parent / "details", s.public_base_url)
    activity = ActivityLog(s.state_file.parent / "activity.jsonl")
    if llm is not None:
        llm.on_call = lambda **k: activity.record("llm", **k)
    if events is not None:
        events.on_stats = lambda **k: activity.record("extract", **k)
    return Services(s, AmapClient(s.amap_key), wecom, state, posters, events, details, PreferenceStore(state),
                    activity)


def role_of(s: Settings, userid: str) -> str:
    for role, uid in (("father", s.father.wecom_userid), ("mother", s.mother.wecom_userid), ("admin", s.admin_userid)):
        if uid and uid == userid:
            return role
    return "other"


JOB_AUDIENCE = {"commute": "father", "outing": "mother", "tv": "father", "digest": "both"}


def deliver(sv: Services, recipients: list[str], message: Message) -> None:
    """Send a feature message; news/textcard cards that would open AMap open a detail page instead."""
    if sv.details is not None and sv.settings.wecom_message_type in ("news", "textcard"):
        message = sv.details.publish(message)
    sv.wecom.send_message(recipients, message, sv.settings.wecom_message_type)


@lru_cache(maxsize=1)
def get_llm() -> LLMClient | None:
    s = get_settings()
    if not s.llm_api_key:
        return None
    return LLMClient(
        s.llm_api_key, provider=s.llm_provider, model=s.llm_model, base_url=s.llm_base_url,
        timeout=s.llm_timeout_s, fallback_model=s.llm_fallback_model,
    )


# ------------------------------------------------------------------ job table


@dataclass(frozen=True)
class Job:
    build: Callable[[Services, bool], Message | None]  # (services, record_state) -> Message (None = skip today)
    recipients: Callable[[Settings], list[str]]
    cron: Callable[[Settings], str]


def build_commute_job(sv: Services, record: bool) -> Message | None:
    """Workdays (incl. 调休 make-up Saturdays): full commute report. Weekends / public holidays:
    no traffic — only a short greeting if there is a weather or utility notice, else nothing."""
    info = day_info(sv.now().date())
    if info.workday:
        return build_commute_message(sv.amap, sv.settings, events=sv.events, today_date=info.day)
    log.info("commute: %s (%s) — no commute report", info.day, info.label)
    return build_offday_greeting(sv.amap, sv.settings, info, events=sv.events)


def build_digest(sv: Services, record: bool, name: str = "爸爸妈妈", greeting: str = "晚上好") -> Message | None:
    """Evening local news: up to DIGEST_MAX_ITEMS original articles. None (send nothing) when there
    are no new articles or the feed is down — never a broken message."""
    if sv.events is None:
        return None
    try:
        builder = DigestBuilder(get_llm(), sv.state, max_items=sv.settings.digest_max_items)
        items = builder.build(sv.events.recent_articles(), sv.now().date())
    except Exception:  # noqa: BLE001 — a digest is nice-to-have; skip rather than send a fallback
        log.exception("digest build failed; skipping today")
        return None
    if not items:
        log.info("digest: no new articles today")
        return None
    if record:
        builder.remember(items)
    return format_digest_message(items, name=name, greeting=greeting)


def _parents(s: Settings) -> list[str]:
    return list(dict.fromkeys(u for u in (s.father.wecom_userid, s.mother.wecom_userid) if u))


JOBS: dict[str, Job] = {
    "commute": Job(
        build=build_commute_job,
        recipients=lambda s: [s.father.wecom_userid],
        cron=lambda s: s.commute_cron,
    ),
    "outing": Job(
        build=lambda sv, record: build_outing_message(
            sv.amap, sv.settings, sv.state, sv.now().date(), record=record, events=sv.events,
            prefs=sv.prefs, remember=record,
        ),
        recipients=lambda s: [s.mother.wecom_userid],
        cron=lambda s: s.outing_cron,
    ),
    "tv": Job(
        build=lambda sv, record: build_tv_message(sv.settings, sv.state, record=record, posters=sv.posters),
        recipients=lambda s: [s.member(r).wecom_userid for r in s.tv_recipients],
        cron=lambda s: s.tv_cron,
    ),
    "digest": Job(
        build=build_digest,
        recipients=_parents,
        cron=lambda s: s.digest_cron,
    ),
}


def _alert_admin(sv: Services, job_name: str, exc: Exception) -> None:
    sv.record("error", what=job_name, error=repr(exc)[:200])
    if not (sv.wecom and sv.settings.admin_userid) or sv.settings.dry_run:
        return
    try:
        sv.wecom.send_text([sv.settings.admin_userid], f"[zcare] 任务 {job_name} 失败：{exc!r}")
    except Exception:  # noqa: BLE001 — alerting must never raise
        log.exception("failed to alert admin")


def fallback_message(name: str, sv: Services) -> Message:
    """Last resort when building a message failed unexpectedly: warm, short, never empty —
    plus official weather warnings if they are reachable. Used so Mom and Dad hear from us every day."""
    s = sv.settings
    who = s.father.display_name if name in ("commute", "tv") else s.mother.display_name
    body = {
        "commute": "今天的路况暂时没查到，出门前可以在高德里看一眼。路上慢点开，注意安全～",
        "outing": "今天的出行推荐暂时没查到。天气好的话，就在家附近的公园走走、晒晒太阳吧～",
        "tv": "这周的好剧单暂时没整理好，过两天再给您推荐。可以先在奇异果TV里看看喜欢的老剧～",
    }.get(name, "今天的消息暂时没整理好，稍后再发给您～")
    lines = [f"**{who}{'早上好' if name != 'tv' else '晚上好'} 🌷**", "", body]
    notices: list[str] = []
    if sv.events is not None:
        try:
            audience = "father" if name in ("commute", "tv") else "mother"
            notices = [n.line(sv.now().date()) for n in sv.events.notices(sv.now().date(), audience)][:2]
        except Exception:  # noqa: BLE001 — truly best effort
            notices = []
    return add_notices(Message(title=f"{who}，今日问候", markdown="\n".join(lines)), notices)


def run_job(name: str, dry_run: bool | None = None) -> dict:
    if name not in JOBS:
        raise KeyError(name)
    sv = get_services()
    dry = sv.settings.dry_run if dry_run is None else dry_run
    job = JOBS[name]
    try:
        recipients = job.recipients(sv.settings)
        try:
            message = job.build(sv, not dry)
        except Exception as exc:  # unexpected bug / outage: never leave the parents without a message
            log.exception("job %s failed to build; sending fallback message", name)
            if not dry:
                _alert_admin(sv, f"{name}（已发送备用问候）", exc)
            message = fallback_message(name, sv)
            used_fallback = True
        else:
            used_fallback = False
        if message is None:
            log.info("job %s: nothing to send today", name)
            if not dry:
                sv.record("sent", job=name, to=JOB_AUDIENCE.get(name, ""), ok=True, skipped=True)
            return {"job": name, "sent": False, "skipped": True, "recipients": recipients,
                    "title": "", "markdown": ""}
        sent = False
        if dry:
            log.info("[dry-run] %s → %s\n%s", name, recipients, message.markdown)
        elif sv.wecom is None:
            raise RuntimeError("WeCom is not configured")
        else:
            deliver(sv, recipients, message)
            sent = True
            log.info("sent %s to %s", name, recipients)
            sv.record("sent", job=name, to=JOB_AUDIENCE.get(name, ""), ok=True, fallback=used_fallback)
    except Exception as exc:
        log.exception("job %s failed", name)
        if not dry:
            sv.record("sent", job=name, to=JOB_AUDIENCE.get(name, ""), ok=False)
            _alert_admin(sv, name, exc)
        raise
    return {"job": name, "sent": sent, "recipients": recipients, "title": message.title,
            "markdown": message.markdown}


def _scheduled_run(name: str) -> None:
    try:
        run_job(name)
    except Exception:  # already logged + alerted; keep the scheduler alive
        pass


WEMP_CLEANUP_CRON = "30 3 * * *"  # daily 03:30, well away from the 06:30/11:30/17:30 fetches
EVENTS_WARM_CRON = "45 6,11,17 * * *"  # 15 min after the we-mp-rss fetches: extract new articles ahead of time


def run_events_warm() -> None:
    """Pre-extract newly fetched articles (and refresh notices) so parents' messages don't wait on GLM."""
    sv = get_services()
    if sv.events is None:
        return
    try:
        today = sv.now().date()
        processed = sv.events.refresh(today, report=True)
        sv.events.notices(today, "father")  # also refreshes the cached notices used by the chat
        log.info("events warm-up: %d new articles extracted", processed)
    except Exception as exc:  # noqa: BLE001 — housekeeping must never take the scheduler down
        log.warning("events warm-up failed: %s", exc)


def run_wemp_cleanup() -> None:
    """Delete we-mp-rss articles older than WEMP_RETENTION_DAYS (keeps its SQLite small)."""
    s = get_settings()
    admin = get_wemp_admin()
    if admin is None:
        return
    try:
        result = admin.clean_old(s.wemp_retention_days)
        log.info("we-mp-rss cleanup: older than %d days → %s", s.wemp_retention_days, result)
        get_services().record("cleanup", deleted=(result or {}).get("deleted_count", (result or {}).get("total_count")))
    except Exception as exc:  # noqa: BLE001 — housekeeping must never take the scheduler down
        log.warning("we-mp-rss cleanup failed: %s", exc)
        get_services().record("cleanup", error=repr(exc)[:120])


def build_report(sv: Services) -> tuple[str, str]:
    s = sv.settings
    names = {"father": s.father.display_name, "mother": s.mother.display_name, "admin": "你", "other": "其他成员"}
    return build_daily_report(sv.activity.since(86400) if sv.activity else [], sv.now(), names)


def run_daily_report() -> None:
    """Daily snapshot to ADMIN_WECOM_USERID: short text + link to the full report page."""
    sv = get_services()
    if not sv.settings.admin_userid:
        return
    try:
        short, full = build_report(sv)
        url = sv.details.publish_page("zcare 日报", full) if sv.details else None
        text = short + (f"\n\n完整报告：{url}" if url else "\n\n" + full)
        _send_text(sv, [sv.settings.admin_userid], text)
        if sv.activity:
            sv.activity.prune()
        log.info("daily report sent")
    except Exception as exc:  # noqa: BLE001
        log.warning("daily report failed: %s", exc)


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        timezone=settings.timezone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900},
    )
    for name, job in JOBS.items():
        if not job.cron(settings).strip():
            continue  # empty cron = feature turned off (e.g. DIGEST_CRON=)
        trigger = CronTrigger.from_crontab(job.cron(settings), timezone=settings.timezone)
        scheduler.add_job(_scheduled_run, trigger, args=[name], id=name, name=name, replace_existing=True)
    if settings.daily_report_cron and settings.admin_userid:
        scheduler.add_job(run_daily_report, CronTrigger.from_crontab(settings.daily_report_cron, timezone=settings.timezone),
                          id="daily_report", name="daily_report", replace_existing=True)
    if settings.wemp_rss_url and settings.llm_api_key:
        scheduler.add_job(run_events_warm, CronTrigger.from_crontab(EVENTS_WARM_CRON, timezone=settings.timezone),
                          id="events_warm", name="events_warm", replace_existing=True)
    if settings.wemp_rss_url and settings.wemp_admin_password:
        scheduler.add_job(run_wemp_cleanup, CronTrigger.from_crontab(WEMP_CLEANUP_CRON, timezone=settings.timezone),
                          id="wemp_cleanup", name="wemp_cleanup", replace_existing=True)
    return scheduler


# ------------------------------------------------------------------ FastAPI


class _DropHealthChecks(logging.Filter):
    """Hide the once-a-minute Docker health-check lines from uvicorn's access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        return not (len(args) >= 3 and args[2] == "/health")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.getLogger("uvicorn.access").addFilter(_DropHealthChecks())
    sv = get_services()
    scheduler = build_scheduler(sv.settings)
    scheduler.start()
    app.state.scheduler = scheduler
    for job in scheduler.get_jobs():
        log.info("scheduled %-8s next run %s", job.id, job.next_run_time)
    yield
    scheduler.shutdown(wait=False)


# Port 8000 is public (WeCom posts to /wecom/webhook), so expose nothing else by default:
# no interactive docs / OpenAPI schema for scanners to enumerate.
app = FastAPI(title="zcare", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def _token_ok(token: str | None) -> bool:
    expected = get_settings().admin_token
    return bool(expected and token and secrets.compare_digest(token, expected))


def _authorize(token: str | None) -> None:
    if not get_settings().admin_token:
        raise HTTPException(403, "Set ADMIN_TOKEN in .env to use /jobs endpoints")
    if not _token_ok(token):
        raise HTTPException(401, "invalid X-Admin-Token")


@app.get("/health")
def health(request: Request, x_admin_token: str | None = Header(default=None)) -> dict:
    """Public: just {"status": "ok"}. Details for the in-container health check or with the token."""
    local = request.client is not None and request.client.host in ("127.0.0.1", "::1")
    if not (local or _token_ok(x_admin_token)):
        return {"status": "ok"}
    scheduler: BackgroundScheduler = request.app.state.scheduler
    return {
        "status": "ok",
        "dry_run": get_settings().dry_run,
        "jobs": {
            j.id: j.next_run_time.isoformat() if j.next_run_time else None
            for j in scheduler.get_jobs()
        },
        # GLM calls per day and purpose (chat / extract) since the last restart
        "llm_usage": get_llm().usage_summary() if get_llm() else {},
    }


@app.get("/detail/{report_id}", response_class=HTMLResponse)
def detail_page(report_id: str) -> HTMLResponse:
    """Full, untruncated message behind a news card, with a big AMap button (senior-friendly)."""
    details = get_services().details
    data = details.load(report_id) if details else None
    if data is None:
        page = render_page("内容已过期", "**这条内容已经过期了**\n\n请等下一条消息，或者直接问问小助手。")
        return HTMLResponse(page, status_code=404, headers={"Cache-Control": "no-store"})
    return HTMLResponse(render_page(data.get("title", ""), data.get("markdown", "")),
                        headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex",
                                 "Referrer-Policy": "no-referrer"})


_POSTER_FILE = re.compile(r"^(\d{1,12})\.jpg$")


@app.get("/posters/{filename}")
def poster(filename: str) -> FileResponse:
    """Public poster images for WeCom news cards (cached copies of Douban posters)."""
    match = _POSTER_FILE.match(filename)
    posters = get_services().posters
    path = posters.path_for(match.group(1)) if (match and posters) else None
    if path is None or not path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


WEMP_NOTICE_MAX_CHARS = 300
WEMP_QR_MAX_BYTES = 2 * 1024 * 1024
_DATA_IMAGE = re.compile(r"data:image/(?:png|jpe?g);base64,([A-Za-z0-9+/=\s]+)")
QR_CAPTION = ("📲 公众号后台授权已过期，请用管理该订阅号的微信扫码（长按二维码识别，或在另一块屏幕上打开后扫码）。"
              "二维码几分钟后失效，过期了就回复「扫码」再要一张。")
QR_FAILED_TEXT = ("[zcare·公众号订阅] 没能拿到登录二维码：{error}\n"
                  "备用办法：ssh -L 8001:127.0.0.1:8001 root@服务器，打开 http://localhost:8001 扫码。")


@lru_cache(maxsize=1)
def get_wemp_admin() -> WeMpAdminClient | None:
    s = get_settings()
    if not (s.wemp_rss_url and s.wemp_admin_password):
        return None
    return WeMpAdminClient(s.wemp_rss_url, s.wemp_admin_user, s.wemp_admin_password)


def extract_qr_image(content: str) -> bytes | None:
    """The QR that we-mp-rss embeds (SEND_CODE=True) as ![…](data:image/png;base64,…)."""
    import base64
    import binascii

    m = _DATA_IMAGE.search(content)
    if not m:
        return None
    try:
        data = base64.b64decode(re.sub(r"\s+", "", m.group(1)), validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if is_image(data) and len(data) <= WEMP_QR_MAX_BYTES else None


def send_qr_to_admin(sv: Services, image: bytes, caption: str = QR_CAPTION) -> None:
    admin = sv.settings.admin_userid
    if not admin:
        return
    if sv.settings.dry_run or sv.wecom is None:
        log.info("[dry-run] would send %d-byte QR image to %s", len(image), admin)
        return
    media_id = sv.wecom.upload_image(image, "wechat-login-qr.png")
    sv.wecom.send_image([admin], media_id)
    sv.wecom.send_text([admin], caption)


def run_wemp_qr(userid: str) -> None:
    """Admin asked for a fresh login QR ("扫码"): start a new we-mp-rss login session and send its QR."""
    sv = get_services()
    admin = get_wemp_admin()
    try:
        if admin is None:
            raise WeMpAdminError("WEMP_RSS_URL / WEMP_ADMIN_PASSWORD not configured")
        send_qr_to_admin(sv, admin.request_qr())
        log.info("fresh we-mp-rss login QR sent to %s", userid)
    except Exception as exc:  # noqa: BLE001
        log.exception("could not deliver we-mp-rss QR")
        _send_text(sv, [userid], QR_FAILED_TEXT.format(error=exc))


@app.post("/wemp/notify")
async def wemp_notify(request: Request, token: str = Query(default="")) -> dict:
    """we-mp-rss CUSTOM_WEBHOOK → you on WeCom.

    With SEND_CODE=True the login-expired notice embeds the new login QR (base64); it is forwarded
    as a WeCom image so you can re-authorize from your phone — no laptop or SSH tunnel. Other notices
    (e.g. "二维码已过期") are forwarded as text with the hint to reply 「扫码」 for a fresh QR.
    Protected by WEMP_NOTIFY_TOKEN because port 8000 is public.
    """
    s = get_settings()
    if not s.wemp_notify_token or not secrets.compare_digest(token, s.wemp_notify_token):
        raise HTTPException(401, "invalid token")
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "expected JSON") from exc
    title = str((body or {}).get("title", "")).strip()[:60] or "we-mp-rss 通知"
    raw = str((body or {}).get("content", ""))
    qr = extract_qr_image(raw)
    content = re.sub(r"!\[[^\]]*\]\([^)]*\)|data:image/[^\s)\"']+|<img[^>]*>", "", raw)
    content = re.sub(r"\s+", " ", markdown_to_text(content)).strip()[:WEMP_NOTICE_MAX_CHARS]
    sv = get_services()
    if s.admin_userid:
        if qr:
            submit_background(send_qr_to_admin, sv, qr)
        else:
            text = f"[zcare·公众号订阅] {title}\n{content}\n\n回复「扫码」获取新的登录二维码。"
            submit_background(_send_text, sv, [s.admin_userid], text)
    log.warning("we-mp-rss notice: %s — %s%s", title, content[:120], " (+QR image)" if qr else "")
    sv.record("wemp_notice", title=title, qr=bool(qr))
    return {"ok": True, "qr": bool(qr)}


@app.post("/jobs/{name}/run")
def trigger_job(name: str, dry_run: bool = True, x_admin_token: str | None = Header(default=None)) -> dict:
    """Manually run a job (requires X-Admin-Token). Defaults to dry-run: returns the message only."""
    _authorize(x_admin_token)
    if name not in JOBS:
        raise HTTPException(404, f"unknown job {name!r}; choose from {sorted(JOBS)}")
    try:
        return run_job(name, dry_run=dry_run)
    except Exception as exc:  # surface the error to the caller
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc


@lru_cache(maxsize=1)
def get_wecom_crypto() -> WeComCrypto | None:
    s = get_settings()
    if not (s.wecom_callback_token and s.wecom_encoding_aes_key):
        return None
    return WeComCrypto(s.wecom_callback_token, s.wecom_encoding_aes_key, receive_id=s.wecom_corp_id)


@app.get("/wecom/webhook", response_class=PlainTextResponse)
def wecom_verify_url(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
) -> PlainTextResponse:
    """WeCom "设置API接收" URL verification: check signature, decrypt echostr, echo plaintext.

    Must return the bare plaintext within 1s — no JSON quoting, no BOM, no newline.
    """
    crypto = get_wecom_crypto()
    if crypto is None:
        raise HTTPException(503, "WeCom callback not configured")
    # If WeCom (or a proxy) didn't percent-encode '+', it arrives as a space; base64 has no spaces.
    echostr = echostr.replace(" ", "+")
    try:
        plaintext = crypto.verify_url(msg_signature, timestamp, nonce, echostr)
    except (WeComCryptoError, UnicodeDecodeError) as exc:
        log.warning("WeCom URL verification failed: %s", exc)
        raise HTTPException(403, "verification failed") from exc
    return PlainTextResponse(plaintext)


# ------------------------------------------------------------ incoming messages

# Slow work (LLM, AMap, Douban, sending) runs here, fully decoupled from the HTTP request, so the
# webhook always answers WeCom within milliseconds (WeCom waits only 5 s, then retries).
_WORKER = ThreadPoolExecutor(max_workers=4, thread_name_prefix="zcare-bg")


def submit_background(fn: Callable[..., object], *args: object) -> Future:
    def runner() -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 — never lose a failure silently
            log.exception("background task %s failed", getattr(fn, "__name__", fn))
    return _WORKER.submit(runner)


CALLBACK_MAX_AGE_S = 3600  # reject replays of old signed requests


@lru_cache(maxsize=1)
def get_bot() -> ChatBot:
    s = get_settings()
    return ChatBot(
        [s.father.wecom_userid, s.mother.wecom_userid, s.admin_userid],
        llm_enabled=bool(s.llm_api_key),
        llm_max_per_hour=s.llm_max_per_hour,
        admin_user=s.admin_userid,
    )


@lru_cache(maxsize=1)
def get_assistant() -> FamilyAssistant | None:
    s = get_settings()
    llm = get_llm()
    if llm is None:
        return None
    log.info("LLM assistant enabled: %s / %s", llm.provider.name, " → ".join(llm.models))
    return FamilyAssistant(llm, assistant_name=s.assistant_name)


def _display_name(s: Settings, userid: str) -> str:
    for member in (s.father, s.mother):
        if member.wecom_userid and member.wecom_userid == userid:
            return member.display_name
    return ""


def build_on_demand_message(
    sv: Services, command: Command, name: str, record: bool = False
) -> Message | None:
    """The data message for an on-demand request (nothing sent).

    ``record`` remembers recommended dramas so later requests don't repeat them.
    Outing suggestions are never recorded on demand, so they don't shift the daily rotation.
    """
    if command is Command.COMMUTE:
        return build_commute_message(sv.amap, sv.settings, name=name, greeting="你好", events=sv.events,
                                     today_date=sv.now().date())
    if command is Command.OUTING:
        return build_outing_message(
            sv.amap, sv.settings, sv.state, sv.now().date(), record=False, name=name, greeting="你好",
            events=sv.events, prefs=sv.prefs, remember=record,  # remember what we suggested → "这个太远了"
        )
    if command is Command.TV:
        return build_tv_message(sv.settings, sv.state, record=record, name=name or "您", posters=sv.posters)
    if command is Command.NEWS:
        # on demand: not remembered, so the evening digest can still include these stories
        return build_digest(sv, record=False, name=name or "您", greeting="你好")
    if command is Command.WEATHER:
        audience = "father" if name == sv.settings.father.display_name else "mother"
        return build_weather_message(sv.amap, sv.settings, sv.now().date(), name=name or "您", greeting="你好",
                                     events=sv.events, audience=audience)
    return None


def run_on_demand(command: Command, userid: str) -> None:
    """Build the requested message and push it to the requester (runs after the HTTP reply)."""
    sv = get_services()
    name = _display_name(sv.settings, userid)
    try:
        message = build_on_demand_message(sv, command, name, record=not sv.settings.dry_run)
        if message is None:
            return
        if sv.settings.dry_run or sv.wecom is None:
            log.info("[dry-run] on-demand %s → %s\n%s", command.value, userid, message.markdown)
            return
        deliver(sv, [userid], message)
        log.info("on-demand %s sent to %s", command.value, userid)
        sv.record("on_demand", command=command.value, role=role_of(sv.settings, userid), ok=True)
    except Exception as exc:
        log.exception("on-demand %s for %s failed", command.value, userid)
        sv.record("on_demand", command=command.value, role=role_of(sv.settings, userid), ok=False,
                  error=repr(exc)[:120])
        if sv.wecom and not sv.settings.dry_run:
            try:  # traffic / outing: the same warm fallback as the daily messages; dramas: a short apology
                job = {Command.COMMUTE: "commute", Command.OUTING: "outing"}.get(command)
                if job:
                    sv.wecom.send_message([userid], fallback_message(job, sv), "text")
                else:
                    sv.wecom.send_text([userid], "抱歉，暂时没查到，请稍后再试～")
            except Exception:  # noqa: BLE001
                log.exception("failed to send fallback")
            _alert_admin(sv, f"on-demand {command.value}", exc)


def _send_text(sv: Services, userids: list[str], text: str) -> None:
    if sv.settings.dry_run or sv.wecom is None:
        log.info("[dry-run] text → %s: %s", userids, text)
        return
    sv.wecom.send_text(userids, text)


def run_llm_chat(userid: str, text: str) -> None:
    """LLM path: warm reply first, then urgent alert and/or the requested data job."""
    sv = get_services()
    assistant = get_assistant()
    if assistant is None:
        return
    name = _display_name(sv.settings, userid) or "您"
    audience = "mother" if userid == sv.settings.mother.wecom_userid else "father"
    today = sv.now().date()
    notes = ([n.line(today) for n in sv.events.cached_notices(today, audience)[:3]]
             if sv.events else [])  # cache only: chat never waits on a network call
    extras: dict[str, str] = {"Day": day_info(today).label}
    if sv.prefs is not None:
        recent = sv.prefs.recent_recommendations()
        extras["Recent"] = "；".join(f"{r['title']}@{r['venue']}·{r['category']}" for r in recent[:3])
        extras["Prefs"] = sv.prefs.summary()
    result = assistant.respond(userid, name, text, sv.now(), notes=notes, extras=extras)
    log.info("assistant[%s] intent=%s urgent=%s for %s", result.source, result.intent, result.urgent, userid)
    if sv.prefs is not None:
        result = _apply_feedback(sv.prefs, result, text,
                                 on_record=lambda fb: sv.record("feedback", role=role_of(sv.settings, userid),
                                                                target=fb.target, value=fb.value, reason=fb.reason))
    sv.record("chat", role=role_of(sv.settings, userid), intent=result.intent, source=result.source,
              urgent=result.urgent, text=text[:24])
    try:
        _send_text(sv, [userid], result.reply)
    except Exception:  # noqa: BLE001 — still try the alert / job below
        log.exception("failed to send assistant reply to %s", userid)
    if result.urgent and sv.settings.admin_userid and sv.settings.admin_userid != userid:
        try:
            _send_text(sv, [sv.settings.admin_userid], f"[zcare] ⚠️ {name}发来可能紧急的消息：「{text[:200]}」请尽快联系。")
        except Exception:  # noqa: BLE001
            log.exception("failed to send urgent alert")
    command = result.command
    if command is not None and get_bot().try_start(userid, command):
        run_on_demand(command, userid)


FEEDBACK_ACK = "好的，记下了！以后少给您推荐这类地方～"
FEEDBACK_LIKE_ACK = "好的，记下了！以后多给您找这样的～"


def _apply_feedback(prefs: PreferenceStore, result, text: str, on_record=None):
    """Store verified feedback ("这个太远了"). The LLM's target must match something we really
    recommended (or a known category); without an LLM, keyword wording + the latest pick is used."""
    from dataclasses import replace as dc_replace

    fb = prefs.resolve(result.feedback, text) if result.intent == "feedback" else None
    if fb is None and result.source == "fallback" and any(w in text for ws in FEEDBACK_KEYWORDS.values() for w in ws):
        fb = prefs.fallback_from_text(text)
        if fb is not None:
            result = dc_replace(result, intent="feedback", reply=FEEDBACK_ACK)
    if fb is not None:
        prefs.record(fb)
        log.info("preference recorded: %s %s (%s)", fb.target, fb.value, fb.reason)
        if on_record is not None:
            on_record(fb)
        if fb.reason == "like" and "少" in result.reply:  # small models copy the dislike wording
            result = dc_replace(result, reply=FEEDBACK_LIKE_ACK)
    elif result.intent == "feedback":
        log.info("feedback not recorded (target not among recent recommendations): %s", result.feedback)
    return result


def _check_timestamp(timestamp: str) -> None:
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError as exc:
        raise WeComCryptoError("timestamp not an integer") from exc
    if age > CALLBACK_MAX_AGE_S:
        raise WeComCryptoError(f"timestamp too old ({age:.0f}s)")


@app.post("/wecom/webhook")
async def wecom_receive(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
) -> Response:
    """Receive a member's message and answer WeCom immediately.

    The HTTP response is "" (LLM mode) or a short encrypted ack (keyword mode); the real answer is
    produced by a background worker and pushed with the active send API (/cgi-bin/message/send).
    """
    crypto = get_wecom_crypto()
    if crypto is None:
        raise HTTPException(503, "WeCom callback not configured")
    body = await request.body()
    try:
        encrypted = extract_encrypt(body)
        crypto.verify_signature(msg_signature, timestamp, nonce, encrypted)
        _check_timestamp(timestamp)
        msg = parse_message(crypto.decrypt(encrypted))
    except CallbackFormatError as exc:
        log.warning("bad WeCom callback body: %s", exc)
        raise HTTPException(400, "bad request") from exc
    except (WeComCryptoError, UnicodeDecodeError) as exc:
        log.warning("WeCom callback rejected: %s", exc)
        raise HTTPException(403, "verification failed") from exc

    reaction = get_bot().handle(msg)
    if not reaction.chat and (reaction.command or reaction.admin_command or reaction.reply):
        # keyword mode / admin commands (the LLM path records itself in run_llm_chat)
        get_services().record("chat", role=role_of(get_settings(), msg.from_user), source="keyword",
                              intent=(reaction.command.value if reaction.command else
                                      "admin" if reaction.admin_command else "help"), text=msg.content[:24])
    if reaction.admin_command == "wemp_qr":
        submit_background(run_wemp_qr, msg.from_user)
    if reaction.chat:
        submit_background(run_llm_chat, msg.from_user, msg.content)
    if reaction.command is not None:
        submit_background(run_on_demand, reaction.command, msg.from_user)
    if not reaction.reply:
        return PlainTextResponse("")  # empty body = "received, no reply"
    xml = build_passive_reply(
        crypto,
        to_user=msg.from_user,
        corp_id=get_settings().wecom_corp_id,
        content=reaction.reply,
        timestamp=timestamp,
        nonce=nonce,
    )
    return Response(content=xml, media_type="application/xml")


# ------------------------------------------------------------------ CLI


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="zcare")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run API + scheduler")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    for cmd in ("preview", "send"):
        p = sub.add_parser(cmd, help=f"{cmd} one job now")
        p.add_argument("job", choices=sorted(JOBS))
    sub.add_parser("report", help="print the daily admin report for the last 24 hours (sends nothing)")
    chat = sub.add_parser("chat", help="try the LLM assistant locally (calls the LLM, sends nothing)")
    chat.add_argument("text")
    chat.add_argument("--as", dest="role", choices=("father", "mother"), default="mother")
    chat.add_argument("--no-followup", action="store_true",
                      help="don't build the traffic/outing/tv data message (skips AMap/Douban calls)")
    args = parser.parse_args(argv)

    if args.cmd == "report":
        short, full = build_report(get_services())
        print(short + "\n\n" + full)
        return

    if args.cmd == "chat":
        assistant = get_assistant()
        if assistant is None:
            raise SystemExit("Set LLM_API_KEY in .env first")
        s = get_settings()
        member = s.member(args.role)
        sv = get_services()
        r = assistant.respond(f"cli-{args.role}", member.display_name, args.text, sv.now())
        print(f"intent={r.intent} urgent={r.urgent} source={r.source}")
        print(f"\n--- message 1: assistant reply ---\n{r.reply}")
        if r.urgent:
            print(f"\n--- would also alert ADMIN_WECOM_USERID ({s.admin_userid or 'NOT SET'}) ---")
        if r.command is not None and not args.no_followup:
            message = build_on_demand_message(sv, r.command, member.display_name)
            if message is not None:
                fmt = s.wecom_message_type
                print(f"\n--- message 2: {r.intent} data (sent right after, as {fmt}) ---")
                if fmt in ("news", "textcard") and message.articles:
                    for a in message.articles:
                        print(f"[card] {a.title}\n  {a.description.replace(chr(10), chr(10) + '  ')}"
                              f"\n  url: {a.url}\n  picurl: {a.picurl or '(none)'}")
                else:
                    print(message.markdown if fmt == "markdown" else message.text)
        print("\n(nothing was sent)")
        return

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run(app, host=args.host, port=args.port, workers=1)
    else:
        result = run_job(args.job, dry_run=args.cmd == "preview")
        print(result["markdown"])


if __name__ == "__main__":
    cli()
