"""Central configuration, loaded from environment variables / .env via python-dotenv.

Call ``get_settings()`` rather than reading os.environ elsewhere.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
if os.getenv("ZCARE_SKIP_DOTENV") != "1":  # tests set this so a real .env never leaks in
    load_dotenv(PROJECT_ROOT / ".env")

VALID_MESSAGE_TYPES = ("markdown", "text", "news", "textcard")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_list(name: str, default: str = "") -> tuple[str, ...]:
    raw = _env(name, default)
    return tuple(part.strip() for part in raw.replace("，", ",").split(",") if part.strip())


@dataclass(frozen=True)
class Location:
    """A named point. ``coords`` is AMap GCJ-02 "lng,lat" (longitude first!)."""

    name: str
    coords: str

    @classmethod
    def parse(cls, env_name: str, label: str, raw: str) -> Location:
        try:
            lng_s, lat_s = (p.strip() for p in raw.split(","))
            lng, lat = float(lng_s), float(lat_s)
        except ValueError as exc:
            raise ConfigError(f"{env_name} must be 'lng,lat', got {raw!r}") from exc
        # Everywhere in China longitude (73..135) > latitude (18..54); catch swapped input.
        if lng < lat:
            raise ConfigError(f"{env_name} looks like 'lat,lng' — AMap expects 'lng,lat': {raw!r}")
        if not (-180 <= lng <= 180 and -90 <= lat <= 90):
            raise ConfigError(f"{env_name} out of range: {raw!r}")
        return cls(name=label, coords=f"{lng:.6f},{lat:.6f}")


@dataclass(frozen=True)
class FamilyMember:
    display_name: str  # how messages address them, e.g. 爸爸
    wecom_userid: str  # WeCom member UserID (通讯录 → 成员 → 账号)


@dataclass(frozen=True)
class Settings:
    # --- AMap ---
    amap_key: str
    city_name: str
    city_adcode: str

    # --- WeCom ---
    wecom_corp_id: str
    wecom_agent_id: int
    wecom_secret: str
    wecom_message_type: str
    wecom_callback_token: str  # 接收消息 → Token (empty = webhook disabled)
    wecom_encoding_aes_key: str  # 接收消息 → EncodingAESKey (43 chars)

    # --- People & places ---
    father: FamilyMember
    mother: FamilyMember
    admin_userid: str  # you; receives failure alerts. Empty = disabled.
    father_home: Location
    father_work: Location
    mother_home: Location

    # --- Schedules (crontab syntax, APScheduler 3.x semantics) ---
    timezone: str
    commute_cron: str
    outing_cron: str
    tv_cron: str
    daily_report_cron: str  # admin activity report; empty = off
    digest_cron: str  # evening local-news digest to both parents; empty = off
    digest_max_items: int

    # --- Feature tuning ---
    commute_typical_minutes: int  # 0 = unknown; enables "slower than usual" hints
    outing_max_transfers: int
    outing_max_walk_m: int
    wemp_rss_url: str  # e.g. http://we-mp-rss:8001 — WeChat articles; empty = no events/notices from articles
    wemp_rss_feeds: tuple[str, ...]  # we-mp-rss feed (account) ids; empty = all subscribed accounts
    wemp_notify_token: str  # shared secret for we-mp-rss → zcare login-expiry webhook; empty = disabled
    wemp_retention_days: int  # we-mp-rss articles older than this are deleted daily (also the events look-back)
    wemp_admin_user: str  # we-mp-rss admin login, used to request a fresh WeChat login QR
    wemp_admin_password: str
    nmc_province: str  # official weather warnings (nmc.cn); empty = disabled
    nmc_city_keyword: str
    outing_event_url: str
    outing_event_item_selector: str
    outing_event_venue: str
    tv_recipients: tuple[str, ...]  # roles: father / mother
    tv_classic_genres: tuple[str, ...]  # Douban genre lists for the classics pool
    tv_trending_tag: str  # Douban 热门 tag for the new-hits pool
    tv_new_picks: int  # how many of TV_PICKS are new hits
    tv_preferred_genres: tuple[str, ...]
    tv_excluded_genres: tuple[str, ...]
    tv_preferred_platform: str  # Douban vendor name, e.g. 爱奇艺
    tv_app_name: str  # what Dad opens on the TV, e.g. 奇异果TV
    tv_min_rating: float
    tv_picks: int
    douban_cookie: str

    # --- LLM assistant. Empty key = keyword bot only. ---
    llm_provider: str  # bigmodel | deepseek
    llm_api_key: str
    llm_base_url: str  # empty = provider default
    llm_model: str  # empty = provider default
    llm_fallback_model: str  # empty = provider default, "none" = disabled
    llm_timeout_s: float
    llm_max_per_hour: int
    assistant_name: str

    # --- Runtime ---
    public_base_url: str  # e.g. http://<server-ip>:8000 — used for poster picurl; empty = no pictures
    dry_run: bool
    admin_token: str
    state_file: Path
    log_level: str

    def member(self, role: str) -> FamilyMember:
        if role == "father":
            return self.father
        if role == "mother":
            return self.mother
        raise ConfigError(f"Unknown family role {role!r} (expected father/mother)")


def load_settings() -> Settings:
    dry_run = _env_bool("DRY_RUN", False)

    required = ["AMAP_KEY", "FATHER_HOME", "FATHER_WORK"]
    if not dry_run:
        required += [
            "WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET",
            "FATHER_WECOM_USERID", "MOTHER_WECOM_USERID",
        ]
    missing = [name for name in required if not _env(name)]
    if missing:
        raise ConfigError(f"Missing required env vars: {', '.join(missing)} (see .env.example)")

    message_type = _env("WECOM_MESSAGE_TYPE", "news").lower()
    if message_type not in VALID_MESSAGE_TYPES:
        raise ConfigError(f"WECOM_MESSAGE_TYPE must be one of {VALID_MESSAGE_TYPES}")

    llm_provider = _env("LLM_PROVIDER", "bigmodel").lower()
    if llm_provider not in ("bigmodel", "deepseek"):
        raise ConfigError("LLM_PROVIDER must be 'bigmodel' or 'deepseek'")
    if _env("DEEPSEEK_API_KEY") and not _env("LLM_API_KEY"):
        logging.getLogger(__name__).warning(
            "DEEPSEEK_API_KEY is no longer read; set LLM_API_KEY (and LLM_PROVIDER) instead"
        )

    callback_token = _env("WECOM_CALLBACK_TOKEN")
    aes_key = _env("WECOM_ENCODING_AES_KEY")
    if bool(callback_token) != bool(aes_key):
        raise ConfigError("Set both WECOM_CALLBACK_TOKEN and WECOM_ENCODING_AES_KEY, or neither")
    if aes_key and len(aes_key) != 43:
        raise ConfigError("WECOM_ENCODING_AES_KEY must be exactly 43 characters")

    father_home = Location.parse("FATHER_HOME", "家", _env("FATHER_HOME"))
    mother_home_raw = _env("MOTHER_HOME")
    mother_home = (
        Location.parse("MOTHER_HOME", "家", mother_home_raw) if mother_home_raw else father_home
    )

    state_file = Path(_env("STATE_FILE", "data/state.json"))
    if not state_file.is_absolute():
        state_file = PROJECT_ROOT / state_file

    tv_recipients = _env_list("TV_RECIPIENTS", "father")
    settings = Settings(
        amap_key=_env("AMAP_KEY"),
        city_name=_env("CITY_NAME", "呼和浩特"),
        city_adcode=_env("CITY_ADCODE", "150100"),
        wecom_corp_id=_env("WECOM_CORP_ID"),
        wecom_agent_id=_env_int("WECOM_AGENT_ID", 0),
        wecom_secret=_env("WECOM_SECRET"),
        wecom_message_type=message_type,
        wecom_callback_token=callback_token,
        wecom_encoding_aes_key=aes_key,
        father=FamilyMember(_env("FATHER_NAME", "爸爸"), _env("FATHER_WECOM_USERID")),
        mother=FamilyMember(_env("MOTHER_NAME", "妈妈"), _env("MOTHER_WECOM_USERID")),
        admin_userid=_env("ADMIN_WECOM_USERID"),
        father_home=father_home,
        father_work=Location.parse("FATHER_WORK", "单位", _env("FATHER_WORK")),
        mother_home=mother_home,
        timezone=_env("TIMEZONE", "Asia/Shanghai"),
        # NOTE: APScheduler 3.x numeric day_of_week is 0=Monday (not cron's 0=Sunday).
        # Use names (mon-fri, fri) to avoid off-by-one-day surprises.
        # Daily: the job itself checks the official calendar (workdays incl. 调休 Saturdays get the
        # commute report; weekends/holidays only a short greeting when there is a notice).
        commute_cron=_env("COMMUTE_CRON", "30 7 * * *"),
        outing_cron=_env("OUTING_CRON", "0 9 * * *"),
        tv_cron=_env("TV_CRON", "0 19 * * fri"),
        daily_report_cron=_env("DAILY_REPORT_CRON", "0 21 * * *"),
        digest_cron=_env("DIGEST_CRON", "30 19 * * *"),
        digest_max_items=max(1, min(8, _env_int("DIGEST_MAX_ITEMS", 5))),
        commute_typical_minutes=_env_int("COMMUTE_TYPICAL_MINUTES", 0),
        outing_max_transfers=_env_int("OUTING_MAX_TRANSFERS", 1),
        outing_max_walk_m=_env_int("OUTING_MAX_WALK_METERS", 800),
        wemp_rss_url=_env("WEMP_RSS_URL").rstrip("/"),
        wemp_rss_feeds=_env_list("WEMP_RSS_FEEDS"),
        wemp_notify_token=_env("WEMP_NOTIFY_TOKEN"),
        wemp_retention_days=max(1, _env_int("WEMP_RETENTION_DAYS", 7)),
        wemp_admin_user=_env("WEMP_ADMIN_USER", "admin"),
        wemp_admin_password=_env("WEMP_ADMIN_PASSWORD"),
        nmc_province=_env("NMC_PROVINCE", "内蒙古自治区"),
        nmc_city_keyword=_env("NMC_CITY_KEYWORD", "呼和浩特"),
        outing_event_url=_env("OUTING_EVENT_URL"),
        outing_event_item_selector=_env("OUTING_EVENT_ITEM_SELECTOR"),
        outing_event_venue=_env("OUTING_EVENT_VENUE"),
        tv_recipients=tv_recipients,
        tv_classic_genres=_env_list("TV_CLASSIC_GENRES", "历史,家庭,战争,悬疑,犯罪,戏曲"),
        tv_trending_tag=_env("TV_TRENDING_TAG", "国产剧"),
        tv_new_picks=_env_int("TV_NEW_PICKS", 2),
        tv_preferred_genres=_env_list("TV_PREFERRED_GENRES", "历史,家庭,悬疑,犯罪,战争,剧情"),
        tv_excluded_genres=_env_list("TV_EXCLUDED_GENRES", "动画,恐怖,真人秀,脱口秀,儿童"),
        tv_preferred_platform=_env("TV_PREFERRED_PLATFORM", "爱奇艺"),
        tv_app_name=_env("TV_APP_NAME", "奇异果TV"),
        tv_min_rating=_env_float("TV_MIN_RATING", 7.5),
        tv_picks=_env_int("TV_PICKS", 3),
        douban_cookie=_env("DOUBAN_COOKIE"),
        llm_provider=llm_provider,
        llm_api_key=_env("LLM_API_KEY"),
        llm_base_url=_env("LLM_BASE_URL"),
        llm_model=_env("LLM_MODEL"),
        llm_fallback_model=_env("LLM_FALLBACK_MODEL"),
        llm_timeout_s=_env_float("LLM_TIMEOUT_SECONDS", 20.0),
        llm_max_per_hour=_env_int("LLM_MAX_MESSAGES_PER_HOUR", 30),
        assistant_name=_env("ASSISTANT_NAME", "小暖"),
        public_base_url=_env("PUBLIC_BASE_URL").rstrip("/"),
        dry_run=dry_run,
        admin_token=_env("ADMIN_TOKEN"),
        state_file=state_file,
        log_level=_env("LOG_LEVEL", "INFO").upper(),
    )
    for role in settings.tv_recipients:
        settings.member(role)  # validates role names early
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
