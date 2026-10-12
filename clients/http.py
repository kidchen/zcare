"""Shared requests.Session factory with conservative retries."""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def build_session(user_agent: str | None = None, retries: int = 3) -> requests.Session:
    """Retries idempotent GETs only — never POST, so a message is never sent twice."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    if user_agent:
        session.headers["User-Agent"] = user_agent
    return session
