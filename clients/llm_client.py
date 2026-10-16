"""Chat-completions client for OpenAI-style LLM APIs reachable from mainland China.

Providers (choose with LLM_PROVIDER):
- ``bigmodel`` (default) — 智谱 BigModel / GLM. Docs: https://docs.bigmodel.cn/cn/api/introduction
  POST https://open.bigmodel.cn/api/paas/v4/chat/completions, ``Authorization: Bearer <key>``.
  Errors carry a business code in ``{"error": {"code": "...", "message": "..."}}``; HTTP 429 is
  used for arrears (1113, not retryable) as well as rate limit (1302) / overload (1305).
- ``deepseek`` — https://api-docs.deepseek.com (balance exhausted = HTTP 402).

Both: ``response_format={"type": "json_object"}`` (prompt must ask for JSON) and
``thinking={"type": "disabled"}`` for fast, cheap replies (thinking is ON by default).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    default_model: str
    default_fallback_model: str  # tried when the primary is rate-limited/overloaded/slow; "" = none
    console: str  # where to manage keys / balance (for error hints)


PROVIDERS: dict[str, Provider] = {
    # glm-4-flash-250414: fast (~1 s), no thinking. glm-4.7-flash: smarter but often overloaded (1305).
    "bigmodel": Provider("bigmodel", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash-250414",
                         "glm-4.7-flash", "https://bigmodel.cn/usercenter/proj-mgmt/apikeys"),
    "deepseek": Provider("deepseek", "https://api.deepseek.com", "deepseek-flash", "",
                         "https://platform.deepseek.com"),
}

RETRYABLE_STATUS = {500, 502, 503}
NON_RETRYABLE_CODES = {"1000", "1001", "1003", "1005", "1113", "1301"}
BUSINESS_HINTS = {
    "1000": "authentication failed — check LLM_API_KEY",
    "1001": "missing Authorization header",
    "1003": "API key expired — regenerate it",
    "1005": "account requires secondary verification",
    "1113": "account in arrears (欠费) — top up",
    "1301": "blocked by content safety review",
    "1302": "rate limited",
    "1305": "model overloaded",
}
STATUS_HINTS = {
    400: "invalid request", 401: "wrong LLM_API_KEY", 402: "balance exhausted — top up",
    422: "invalid parameters (check LLM_MODEL)", 429: "rate limited", 500: "server error",
    503: "server overloaded",
}
BLOCKED_FINISH_REASONS = {"sensitive", "content_filter"}


class LLMError(RuntimeError):
    """Any failure to obtain a usable completion."""


def _error_info(resp: requests.Response) -> tuple[str, str]:
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        return "", resp.text[:200]
    if isinstance(err, dict):
        return str(err.get("code", "")), str(err.get("message", ""))[:200]
    return "", str(err or resp.text)[:200]


class LLMClient:
    def __init__(
        self,
        api_key: str,
        provider: str = "bigmodel",
        model: str = "",
        base_url: str = "",
        timeout: float = 20.0,
        fallback_model: str = "",  # "" = provider default, "none" = disabled
        session: requests.Session | None = None,
        retry_delay_s: float = 2.0,
        max_attempts: int = 3,
    ):
        if not api_key:
            raise ValueError("LLM API key is required")
        if provider not in PROVIDERS:
            raise ValueError(f"unknown LLM provider {provider!r}; choose from {sorted(PROVIDERS)}")
        self.provider = PROVIDERS[provider]
        self.model = model or self.provider.default_model
        fallback = self.provider.default_fallback_model if not fallback_model else fallback_model
        if fallback.lower() == "none":
            fallback = ""
        # Attempts alternate primary → fallback → primary ...
        self.models = [self.model] + ([fallback] if fallback and fallback != self.model else [])
        self._url = (base_url or self.provider.base_url).rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._timeout = timeout
        self._session = session or requests.Session()
        self._retry_delay_s = retry_delay_s
        self._max_attempts = max(1, max_attempts)
        self.last_usage: dict[str, Any] = {}  # usage of the last successful completion
        self._stats: dict[tuple[str, str], dict[str, float]] = {}  # (day, purpose) → counters
        self._stats_lock = threading.Lock()
        self.on_call = None  # optional callback(purpose=, ok=, prompt=, completion=, latency=) — activity log

    def _fail(self, status: int, code: str, message: str) -> LLMError:
        hint = BUSINESS_HINTS.get(code) or STATUS_HINTS.get(status, "unexpected status")
        if code == "1113" or status == 402:
            hint += f" at {self.provider.console}"
        return LLMError(f"{self.provider.name} HTTP {status}{f' code {code}' if code else ''} ({hint}): {message}")

    def chat_json(
        self, messages: list[dict[str, str]], max_tokens: int = 400, temperature: float = 0.7,
        timeout: float | None = None, max_attempts: int | None = None, purpose: str = "chat",
    ) -> dict[str, Any]:
        """Return the model's reply parsed as a JSON object.

        Transient failures (rate limit 1302, overload 1305, 5xx, timeouts, empty/non-JSON content)
        are retried with exponential backoff: retry_delay_s, 2x, ... up to max_attempts total,
        alternating between the primary and fallback model.
        """
        payload = {
            "messages": messages,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        started = time.monotonic()
        try:
            result = self._chat_json(payload, headers, max_tokens, timeout, max_attempts, purpose)
        except LLMError:
            self._count(purpose, ok=False, latency=time.monotonic() - started)
            raise
        usage = self.last_usage
        self._count(purpose, ok=True, latency=time.monotonic() - started,
                    prompt=usage.get("prompt_tokens") or 0, completion=usage.get("completion_tokens") or 0)
        return result

    def _count(self, purpose: str, ok: bool, latency: float, prompt: int = 0, completion: int = 0) -> None:
        if self.on_call is not None:
            try:
                self.on_call(purpose=purpose, ok=ok, prompt=prompt, completion=completion, latency=round(latency, 1))
            except Exception:  # noqa: BLE001 — stats must never break a call
                log.debug("on_call hook failed", exc_info=True)
        key = (date.today().isoformat(), purpose)
        with self._stats_lock:
            s = self._stats.setdefault(key, {"calls": 0, "ok": 0, "failed": 0, "prompt_tokens": 0,
                                             "completion_tokens": 0, "latency_s": 0.0})
            s["calls"] += 1
            s["ok" if ok else "failed"] += 1
            s["prompt_tokens"] += prompt
            s["completion_tokens"] += completion
            s["latency_s"] += latency
            for old in [k for k in self._stats if k[0] < (date.today().replace(day=1)).isoformat()]:
                del self._stats[old]  # keep roughly the current month

    def usage_summary(self, days: int = 2) -> dict[str, dict[str, dict[str, float]]]:
        """{day: {purpose: {calls, ok, failed, prompt_tokens, completion_tokens, avg_latency_s}}} (in-memory,
        since the last restart)."""
        with self._stats_lock:
            out: dict[str, dict[str, dict[str, float]]] = {}
            for (day, purpose), s in sorted(self._stats.items(), reverse=True):
                if len(out) >= days and day not in out:
                    continue
                avg = round(s["latency_s"] / s["calls"], 1) if s["calls"] else 0.0
                out.setdefault(day, {})[purpose] = {**{k: v for k, v in s.items() if k != "latency_s"},
                                                    "avg_latency_s": avg}
            return out

    def _chat_json(self, payload: dict, headers: dict, max_tokens: int, timeout: float | None,
                   max_attempts: int | None, purpose: str) -> dict[str, Any]:
        last_error = "unknown"
        attempts = max_attempts or self._max_attempts
        call_timeout = timeout or self._timeout
        for attempt in range(attempts):
            if attempt:
                time.sleep(self._retry_delay_s * 2 ** (attempt - 1))
            model = self.models[attempt % len(self.models)]
            try:
                resp = self._session.post(
                    self._url, json={**payload, "model": model}, headers=headers, timeout=call_timeout
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = f"network: {exc}"
                continue
            if resp.status_code != 200:
                code, message = _error_info(resp)
                retryable = resp.status_code in RETRYABLE_STATUS or (
                    resp.status_code == 429 and code not in NON_RETRYABLE_CODES
                )
                error = self._fail(resp.status_code, code, message)
                if not retryable:
                    raise error
                last_error = str(error)
                continue
            try:
                choice = resp.json()["choices"][0]
                content = (choice.get("message") or {}).get("content") or ""
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"malformed {self.provider.name} response: {exc}") from exc
            if choice.get("finish_reason") in BLOCKED_FINISH_REASONS:
                raise LLMError("reply blocked by content safety review")
            if choice.get("finish_reason") == "length":
                # Output hit max_tokens: the JSON is cut off. Retrying the same request can't help.
                raise LLMError(f"[{purpose}] reply truncated at max_tokens={max_tokens}")
            if not content.strip():
                last_error = f"empty content (finish_reason={choice.get('finish_reason')})"
                continue
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                last_error = f"non-JSON content: {content[:120]!r}"
                continue
            if not isinstance(parsed, dict):
                last_error = "JSON was not an object"
                continue
            if attempt:
                log.info("LLM succeeded on attempt %d with %s", attempt + 1, model)
            usage = resp.json().get("usage") or {}
            self.last_usage = usage
            log.info("LLM usage [%s] %s: prompt=%s cached=%s completion=%s",
                     purpose, model, usage.get("prompt_tokens"),
                     (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0), usage.get("completion_tokens"))
            return parsed
        raise LLMError(f"{self.provider.name} [{purpose}] failed after {attempts} attempts: {last_error}")
