import logging

import pytest


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import config
    import main
    caches = (config.get_settings, main.get_services, main.get_wecom_crypto, main.get_bot, main.get_assistant, main.get_llm, main.get_wemp_admin)

    def make(admin_token=""):
        monkeypatch.setenv("ADMIN_TOKEN", admin_token)
        for fn in caches:
            fn.cache_clear()
        return TestClient(main.app)
    yield make
    for fn in caches:
        fn.cache_clear()


def test_docs_and_schema_are_disabled(client):
    with client() as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path).status_code == 404


def test_health_is_minimal_for_strangers_detailed_with_token(client):
    with client("s3cret") as c:  # TestClient's peer is "testclient", i.e. not localhost
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/health", headers={"X-Admin-Token": "wrong"}).json() == {"status": "ok"}
        detailed = c.get("/health", headers={"X-Admin-Token": "s3cret"}).json()
        assert set(detailed["jobs"]) == {"commute", "outing", "tv", "digest"}


def test_jobs_require_token_even_for_dry_run(client):
    with client() as c:
        assert c.post("/jobs/commute/run?dry_run=true").status_code == 403  # no ADMIN_TOKEN configured
    with client("s3cret") as c:
        assert c.post("/jobs/commute/run?dry_run=true").status_code == 401
        assert c.post("/jobs/nope/run", headers={"X-Admin-Token": "s3cret"}).status_code == 404


def test_health_access_log_lines_are_filtered():
    from main import _DropHealthChecks
    f = _DropHealthChecks()
    mk = lambda path: logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                                        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", path, "1.1", 200), None)
    assert f.filter(mk("/health")) is False
    assert f.filter(mk("/wecom/webhook")) is True
    assert f.filter(mk("/HNAP1")) is True  # scanner probes stay visible
