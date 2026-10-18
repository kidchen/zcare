import os
import re
from pathlib import Path

import pytest
import requests

# Isolate tests from the developer's real configuration:
# 1) never load .env, 2) drop any app setting inherited from the shell.
os.environ["ZCARE_SKIP_DOTENV"] = "1"
os.environ.pop("DEEPSEEK_API_KEY", None)  # legacy name
_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"
for _key in re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", _EXAMPLE.read_text(encoding="utf-8"), re.M):
    os.environ.pop(_key, None)

# Minimal env so config imports cleanly in tests (dry-run: WeCom creds optional).
os.environ.update({
    "DRY_RUN": "true",
    "AMAP_KEY": "test-key",
    "FATHER_HOME": "111.670801,40.818311",
    "FATHER_WORK": "111.751990,40.841490",
})


class FakeResponse:
    def __init__(self, payload=None, text="", status=200, url="", headers=None, content=None):
        self._payload, self.text, self.status_code, self.url = payload, text, status, url
        self.headers = headers or {}
        self.content = content if content is not None else text.encode()
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    """Routes GET/POST by URL substring to queued responses; records calls."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []
        self.headers = {}

    def _dispatch(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for key, queue in self.routes.items():
            if key in url:
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                return item(url, kw) if callable(item) else item  # callables build a response per request
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kw):
        return self._dispatch("GET", url, **kw)

    def post(self, url, **kw):
        return self._dispatch("POST", url, **kw)

    def request(self, method, url, **kw):
        return self._dispatch(method.upper(), url, **kw)


@pytest.fixture
def settings():
    from config import load_settings
    return load_settings()


@pytest.fixture
def state(tmp_path: Path):
    from storage import StateStore
    return StateStore(tmp_path / "state.json")


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    """Fail loudly if any code path tries a real HTTP request (pytest.fail is a BaseException,
    so broad `except Exception` handlers can't swallow it)."""
    def _blocked(self, request, **kwargs):
        pytest.fail(f"real HTTP request attempted in tests: {request.method} {request.url}")
    monkeypatch.setattr(requests.Session, "send", _blocked)


@pytest.fixture(autouse=True)
def _no_amap_pacing(monkeypatch):
    """Production paces AMap calls (~3/s) and waits before a QPS retry; tests don't need to wait."""
    import clients.amap_client as amap_client
    monkeypatch.setattr(amap_client, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(amap_client, "QPS_RETRY_WAIT_S", 0.0)
