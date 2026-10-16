import base64
import dataclasses

import pytest

from tests.conftest import FakeResponse, FakeSession

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PNG_B64 = base64.b64encode(PNG).decode()


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import config
    import main
    caches = (config.get_settings, main.get_services, main.get_wecom_crypto, main.get_bot,
              main.get_assistant, main.get_llm, main.get_wemp_admin)

    def make(token="s3cret", admin="me01"):
        monkeypatch.setenv("WEMP_NOTIFY_TOKEN", token)
        monkeypatch.setenv("ADMIN_WECOM_USERID", admin)
        for fn in caches:
            fn.cache_clear()
        sent = []
        monkeypatch.setattr(main, "submit_background", lambda fn, *a: sent.append((fn.__name__, a)))
        return TestClient(main.app), sent
    yield make
    for fn in caches:
        fn.cache_clear()


# ---------------------------------------------------------------- /wemp/notify

def test_expiry_notice_with_embedded_qr_is_sent_as_image(client):
    c, sent = client()
    body = {"title": "WeRss授权过期,扫码授权",
            "content": f"- 服务名：we-mp-rss\n- 发送时间：2026-10-12 08:00:00![描述](data:image/png;base64,{PNG_B64})"}
    with c:
        r = c.post("/wemp/notify?token=s3cret", json=body)
    assert r.status_code == 200 and r.json()["qr"] is True
    ((fn, (sv, image)),) = sent
    assert fn == "send_qr_to_admin" and image == PNG


def test_notice_without_valid_image_is_text_with_reply_hint(client):
    c, sent = client()
    body = {"title": "WeRss授权过期", "content": "**二维码已过期，请重新获取** ![qr](data:image/png;base64,AAAABBBB)"}
    with c:
        assert c.post("/wemp/notify?token=s3cret", json=body).json()["qr"] is False
    ((fn, (sv, users, text)),) = sent
    assert fn == "_send_text" and users == ["me01"]
    assert "二维码已过期" in text and "回复「扫码」" in text and "base64" not in text and "**" not in text


@pytest.mark.parametrize("url,token", [
    ("/wemp/notify?token=wrong", "s3cret"),
    ("/wemp/notify", "s3cret"),
    ("/wemp/notify?token=", ""),  # feature disabled when no token is configured
])
def test_notify_requires_the_shared_token(client, url, token):
    c, sent = client(token=token)
    with c:
        assert c.post(url, json={"title": "x", "content": "y"}).status_code == 401
    assert sent == []


def test_notify_rejects_non_json(client):
    c, _ = client()
    with c:
        assert c.post("/wemp/notify?token=s3cret", content=b"not json",
                      headers={"Content-Type": "application/json"}).status_code == 400


def test_extract_qr_image_rejects_non_images_and_oversize():
    import main
    assert main.extract_qr_image(f"![x](data:image/png;base64,{PNG_B64})") == PNG
    assert main.extract_qr_image(f"![x](data:image/png;base64,{base64.b64encode(b'<html>').decode()})") is None
    assert main.extract_qr_image("no image here") is None
    big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024)).decode()
    assert main.extract_qr_image(f"data:image/png;base64,{big}") is None


# ---------------------------------------------------------------- WeCom image

def test_wecom_upload_and_send_image():
    from clients.wecom_client import WeComClient
    session = FakeSession({
        "/gettoken": [FakeResponse({"errcode": 0, "access_token": "T", "expires_in": 7200})],
        "/media/upload": [FakeResponse({"errcode": 0, "type": "image", "media_id": "MID"})],
        "/message/send": [FakeResponse({"errcode": 0})],
    })
    wc = WeComClient("corp", 1, "sec", session=session)
    assert wc.upload_image(PNG) == "MID"
    _, url, kw = session.calls[1]
    assert kw["params"] == {"access_token": "T", "type": "image"}
    name, data, mime = kw["files"]["media"]
    assert data == PNG and mime == "image/png"
    wc.send_image(["me01"], "MID")
    body = session.calls[-1][2]["json"]
    assert body["msgtype"] == "image" and body["image"] == {"media_id": "MID"} and body["touser"] == "me01"


# ---------------------------------------------------------------- we-mp-rss admin client

def test_admin_client_logs_in_starts_session_waits_and_downloads_qr():
    from clients.wemp_admin import WeMpAdminClient
    session = FakeSession({
        "/auth/token": [FakeResponse({"access_token": "JWT", "token_type": "bearer"})],
        "/auth/qr/code": [FakeResponse({"code": 0, "data": {"code": "/static/wx_qrcode.png?t=1"}})],
        "/auth/qr/image": [FakeResponse({"code": 0, "data": False}), FakeResponse({"code": 0, "data": True})],
        "/static/wx_qrcode.png": [FakeResponse(content=PNG)],
    })
    sleeps = []
    qr = WeMpAdminClient("http://we-mp-rss:8001", "admin", "pw", session=session, sleep=sleeps.append).request_qr()
    assert qr == PNG and sleeps == [1.0]
    login = session.calls[0]
    assert login[0] == "POST" and login[1] == "http://we-mp-rss:8001/api/v1/wx/auth/token"
    assert login[2]["data"] == {"username": "admin", "password": "pw"}
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer JWT"


def test_admin_client_wrong_password_and_non_image():
    from clients.wemp_admin import WeMpAdminClient, WeMpAdminError
    bad_login = FakeSession({"/auth/token": [FakeResponse({"detail": {"code": 40101}}, status=202)]})
    with pytest.raises(WeMpAdminError, match="WEMP_ADMIN_PASSWORD"):
        WeMpAdminClient("http://x", "admin", "wrong", session=bad_login).request_qr()
    html = FakeSession({
        "/auth/token": [FakeResponse({"access_token": "JWT"})],
        "/auth/qr/code": [FakeResponse({"code": 0})], "/auth/qr/image": [FakeResponse({"code": 0, "data": True})],
        "/static/wx_qrcode.png": [FakeResponse(text="<html>login</html>")],
    })
    with pytest.raises(WeMpAdminError, match="not return an image"):
        WeMpAdminClient("http://x", "admin", "pw", session=html, sleep=lambda s: None).request_qr()


def test_admin_client_relogs_in_once_on_expired_jwt():
    from clients.wemp_admin import WeMpAdminClient
    session = FakeSession({
        "/auth/token": [FakeResponse({"access_token": "OLD"}), FakeResponse({"access_token": "NEW"})],
        "/auth/qr/status": [FakeResponse({}, status=401), FakeResponse({"code": 0, "data": "waiting"})],
    })
    c = WeMpAdminClient("http://x", "admin", "pw", session=session)
    assert c.status() == "waiting"
    assert [k["headers"]["Authorization"] for _, u, k in session.calls if "qr/status" in u] == ["Bearer OLD", "Bearer NEW"]


# ---------------------------------------------------------------- 扫码 command

def msg(content, user):
    from clients.wecom_callback import IncomingMessage
    return IncomingMessage("corp", user, 0, "text", content, f"id-{content}-{user}", "1")


def test_scan_command_is_admin_only_and_rate_limited():
    from bot import ADMIN_QR_ACK, ADMIN_QR_COOLDOWN_TEXT, ChatBot
    now = [0.0]
    bot = ChatBot(["dad", "me"], clock=lambda: now[0], llm_enabled=True, admin_user="me")
    r = bot.handle(msg("扫码", "me"))
    assert r.admin_command == "wemp_qr" and r.reply == ADMIN_QR_ACK and not r.chat
    assert bot.handle(msg("再来一个二维码", "me")).reply == ADMIN_QR_COOLDOWN_TEXT
    now[0] += 61
    assert bot.handle(msg("重新授权", "me")).admin_command == "wemp_qr"
    dad = bot.handle(msg("扫码", "dad"))  # a parent typing 扫码 is just chat
    assert dad.admin_command is None and dad.chat


def test_run_wemp_qr_sends_image_or_explains_failure(monkeypatch, settings, state):
    import main
    s = dataclasses.replace(settings, dry_run=False, admin_userid="me")

    class FakeWeCom:
        def __init__(self):
            self.calls = []

        def upload_image(self, data, filename):
            self.calls.append(("upload", len(data)))
            return "MID"

        def send_image(self, users, media_id):
            self.calls.append(("image", users, media_id))

        def send_text(self, users, text):
            self.calls.append(("text", users, text))

    wc = FakeWeCom()
    monkeypatch.setattr(main, "get_services", lambda: main.Services(s, object(), wc, state))

    class OkAdmin:
        def request_qr(self):
            return PNG

    monkeypatch.setattr(main, "get_wemp_admin", lambda: OkAdmin())
    main.run_wemp_qr("me")
    assert [c[0] for c in wc.calls] == ["upload", "image", "text"] and wc.calls[1] == ("image", ["me"], "MID")
    assert "回复「扫码」" in wc.calls[2][2]

    wc.calls.clear()

    class BrokenAdmin:
        def request_qr(self):
            raise RuntimeError("we-mp-rss down")

    monkeypatch.setattr(main, "get_wemp_admin", lambda: BrokenAdmin())
    main.run_wemp_qr("me")
    (call,) = wc.calls
    assert call[0] == "text" and "we-mp-rss down" in call[2] and "ssh -L 8001" in call[2]


# ---------------------------------------------------------------- daily article cleanup

def test_clean_old_calls_server_cleanup_with_retention():
    from clients.wemp_admin import WeMpAdminClient
    session = FakeSession({
        "/auth/token": [FakeResponse({"access_token": "JWT"})],
        "/articles/clean-old": [FakeResponse({"code": 0, "message": "success", "data": {"deleted_count": 42}})],
    })
    assert WeMpAdminClient("http://we-mp-rss:8001", "admin", "pw", session=session).clean_old(7) == {"deleted_count": 42}
    method, url, kw = session.calls[1]
    assert method == "DELETE" and url == "http://we-mp-rss:8001/api/v1/wx/articles/clean-old"
    assert kw["params"] == {"days": 7, "dry_run": False} and kw["headers"]["Authorization"] == "Bearer JWT"


def test_cleanup_job_is_scheduled_daily_only_when_configured(settings):
    import dataclasses

    import main
    on = main.build_scheduler(dataclasses.replace(settings, wemp_rss_url="http://we-mp-rss:8001", wemp_admin_password="pw"))
    job = {j.id: j for j in on.get_jobs()}["wemp_cleanup"]
    assert "hour='3'" in repr(job.trigger) and "minute='30'" in repr(job.trigger)
    off = main.build_scheduler(dataclasses.replace(settings, wemp_rss_url="", wemp_admin_password=""))
    assert "wemp_cleanup" not in {j.id for j in off.get_jobs()}


def test_run_cleanup_uses_retention_and_never_raises(monkeypatch, settings):
    import dataclasses

    import main
    calls = []

    class Admin:
        def __init__(self, fail=False):
            self.fail = fail

        def clean_old(self, days):
            calls.append(days)
            if self.fail:
                raise RuntimeError("we-mp-rss down")
            return {"deleted_count": 3}

    monkeypatch.setattr(main, "get_settings", lambda: dataclasses.replace(settings, wemp_retention_days=7))
    monkeypatch.setattr(main, "get_wemp_admin", lambda: Admin())
    main.run_wemp_cleanup()
    monkeypatch.setattr(main, "get_wemp_admin", lambda: Admin(fail=True))
    main.run_wemp_cleanup()  # logged, not raised
    assert calls == [7, 7]


def test_events_lookback_follows_retention(state):
    from datetime import date

    from clients.wemp_rss import Article
    from features import events as ev

    class Capture:
        docs = ""

        def chat_json(self, messages, **kw):
            Capture.docs += messages[1]["content"]
            return {"events": []}

    class Feed:
        def articles(self):
            return [Article("10天前公布的音乐会", "10月20日 演出", date(2026, 10, 3), "a"),
                    Article("昨天公布的展览", "展览", date(2026, 10, 12), "b")]

    ev.EventFinder(Capture(), state, articles=Feed(), events_max_age_days=7).find(date(2026, 10, 13))
    assert "昨天公布的展览" in Capture.docs and "10天前公布" not in Capture.docs
