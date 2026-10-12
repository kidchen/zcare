"""Issue 1: webhook answers WeCom immediately; Issue 2: proper news cards, no HTML."""
import base64
import dataclasses
import os
import re
import threading
import time
from datetime import datetime

import pytest

from bot import Command, parse_command
from clients.wecom_client import WeComClient
from features import tv_recommend as tv
from features.assistant import FamilyAssistant
from messages import Article, Message
from tests.conftest import FakeResponse, FakeSession

TOKEN_RESP = FakeResponse({"errcode": 0, "access_token": "T", "expires_in": 7200})
HTML = re.compile(r"<[^>]+>")


def wecom():
    session = FakeSession({"/gettoken": [TOKEN_RESP], "/message/send": [FakeResponse({"errcode": 0})]})
    return WeComClient("corp", 1, "sec", session=session), session


def sent_body(session):
    return session.calls[-1][2]["json"]


def all_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from all_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from all_strings(v)


DRAMAS = [tv.Drama(str(i), f"剧{i}", 9.0 - i / 10, f"u{i}", "2020", ("剧情", "悬疑"), "简介" * 80,
                   ("腾讯视频（VIP免费观看）",), poster=f"https://img/p{i}.jpg") for i in range(1, 4)]


# ---------------------------------------------------------------- Issue 2: cards

def test_tv_news_has_one_independent_article_per_drama():
    msg = tv.format_tv_message(DRAMAS, "爸爸", picurl_for=lambda d: f"http://srv/posters/{d.subject_id}.jpg")
    client, session = wecom()
    client.send_message(["dad"], msg, "news")
    body = sent_body(session)
    assert body["msgtype"] == "news"
    arts = body["news"]["articles"]
    assert len(arts) == 3
    assert [a["url"] for a in arts] == [f"https://m.douban.com/movie/subject/{i}/" for i in (1, 2, 3)]
    assert [a["picurl"] for a in arts] == [f"http://srv/posters/{i}.jpg" for i in (1, 2, 3)]
    assert arts[1]["title"].startswith("2.《剧2》")
    assert all(len(a["description"].encode()) <= 512 for a in arts)
    assert not any(HTML.search(s) for s in all_strings(body))


def test_textcard_setting_sends_multi_item_results_as_news():
    client, session = wecom()
    client.send_message(["dad"], tv.format_tv_message(DRAMAS, "爸爸"), "textcard")
    body = sent_body(session)
    assert body["msgtype"] == "news" and len(body["news"]["articles"]) == 3
    assert all("picurl" not in a for a in body["news"]["articles"])  # no fake/broken image URLs


def test_single_item_textcard_has_plain_description_without_html():
    msg = Message("标题", '**加粗**\n> 路况：<font color="info">畅通</font>', url="https://uri.amap.com/x")
    client, session = wecom()
    client.send_message(["dad"], msg, "textcard")
    card = sent_body(session)["textcard"]
    assert card["description"] == "加粗\n路况：畅通"
    assert not any(HTML.search(s) for s in all_strings(card))


def test_text_mode_strips_any_stray_tags():
    client, session = wecom()
    client.send_message(["dad"], Message("t", '<div class="normal">你好</div>'), "text")
    assert sent_body(session)["text"]["content"] == "你好"


def test_message_without_link_falls_back_to_text_in_news_mode():
    client, session = wecom()
    client.send_message(["mom"], Message("在家休息", "**妈妈早上好**\n今天在家歇一歇"), "news")
    assert sent_body(session)["msgtype"] == "text"


def test_commute_and_outing_build_single_linked_articles(settings, state):
    from clients.amap_client import AmapClient
    from features.commute import build_commute_message
    from features import outing
    from tests import fixtures as fx
    def amap():
        return AmapClient("k", session=FakeSession({
            "/direction/driving": [FakeResponse(fx.DRIVING)],
            "/weather": [FakeResponse(fx.LIVE), FakeResponse(fx.FORECAST)],
            "/place/text": [fx.poi_echo(photo="http://store.is.autonavi.com/showpic/abc")],
            "/direction/transit": [FakeResponse(fx.TRANSIT)],
        }))
    c = build_commute_message(amap(), settings)
    (art,) = c.articles
    assert art.url.startswith("https://uri.amap.com/navigation") and "约22分钟" in art.title
    assert "**" not in art.description and not HTML.search(art.description) and "兴安北路" in art.description

    o = outing.build_outing_message(amap(), settings, state, datetime(2026, 10, 13).date(), event_sources=[])
    (oart,) = o.articles
    assert oart.picurl == "https://store.is.autonavi.com/showpic/abc"  # public AMap photo, https
    assert "12路" in oart.description and "**" not in oart.description


# ---------------------------------------------------------------- posters

def test_poster_cache_downloads_once_with_referer_and_serves_public_url(tmp_path):
    jpg = b"\xff\xd8\xff" + b"0" * 100
    session = FakeSession({"img/p1.jpg": [FakeResponse(content=jpg, headers={"Content-Type": "image/jpeg"})]})
    cache = tv.PosterCache(tmp_path, "http://203.0.113.10:8000/", session=session)
    assert cache.picurl(DRAMAS[0]) == "http://203.0.113.10:8000/posters/1.jpg"
    assert cache.picurl(DRAMAS[0]) == "http://203.0.113.10:8000/posters/1.jpg"
    assert len(session.calls) == 1 and session.calls[0][2]["headers"]["Referer"].startswith("https://movie.douban.com")
    assert (tmp_path / "1.jpg").read_bytes() == jpg


def test_poster_cache_rejects_non_images_and_needs_public_url(tmp_path):
    html = FakeSession({"img/": [FakeResponse(text="<html>418</html>", headers={"Content-Type": "text/html"})]})
    assert tv.PosterCache(tmp_path, "http://srv", session=html).picurl(DRAMAS[0]) == ""
    assert tv.PosterCache(tmp_path, "", session=html).picurl(DRAMAS[0]) == ""  # no PUBLIC_BASE_URL
    assert tv.PosterCache(tmp_path, "http://srv").path_for("../etc/passwd") is None


def test_poster_route_serves_cached_file_and_404s_otherwise(monkeypatch, tmp_path, settings, state):
    from fastapi.testclient import TestClient
    import main
    cache = tv.PosterCache(tmp_path, "http://srv")
    (tmp_path / "123.jpg").write_bytes(b"\xff\xd8\xffjpeg")
    monkeypatch.setattr(main, "get_services", lambda: main.Services(settings, object(), None, state, cache))
    with TestClient(main.app) as c:
        r = c.get("/posters/123.jpg")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert c.get("/posters/999.jpg").status_code == 404
        assert c.get("/posters/..%2Fstate.json").status_code == 404
        assert c.get("/posters/abc.jpg").status_code == 404


# ---------------------------------------------------------------- Issue 1: async + intent

def test_webhook_responds_before_slow_background_work_finishes(monkeypatch):
    """Real worker pool: the HTTP response must not wait for the LLM/data work."""
    from fastapi.testclient import TestClient
    import config
    import main
    from clients.wecom_crypto import WeComCrypto
    key = base64.b64encode(os.urandom(32)).decode().rstrip("=")
    for k, v in {"WECOM_CALLBACK_TOKEN": "t", "WECOM_ENCODING_AES_KEY": key, "WECOM_CORP_ID": "corp",
                 "FATHER_WECOM_USERID": "dad01", "LLM_API_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    caches = (config.get_settings, main.get_services, main.get_wecom_crypto, main.get_bot, main.get_assistant, main.get_llm, main.get_wemp_admin)
    for fn in caches:
        fn.cache_clear()
    release, finished = threading.Event(), threading.Event()

    def slow_llm_chat(user, text):
        release.wait(10)  # simulates a 10 s LLM + Douban + send
        finished.set()

    monkeypatch.setattr(main, "run_llm_chat", slow_llm_chat)
    try:
        crypto = WeComCrypto("t", key, "corp")
        inner = ("<xml><ToUserName>corp</ToUserName><FromUserName>dad01</FromUserName><CreateTime>1</CreateTime>"
                 "<MsgType>text</MsgType><Content>今天有什么活动？</Content><MsgId>501</MsgId></xml>")
        enc, ts = crypto.encrypt(inner), str(int(time.time()))
        with TestClient(main.app) as client:
            t0 = time.monotonic()
            r = client.post("/wecom/webhook", content=f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>".encode(),
                            params={"msg_signature": crypto.signature(ts, "n", enc), "timestamp": ts, "nonce": "n"})
            elapsed = time.monotonic() - t0
            assert r.status_code == 200 and r.text == ""
            assert elapsed < 1.0 and not finished.is_set()  # answered while work still running
            release.set()
            assert finished.wait(5)
    finally:
        release.set()
        for fn in caches:
            fn.cache_clear()


def test_events_question_is_an_outing_request():
    assert parse_command("今天有什么活动？") is Command.OUTING
    assert parse_command("最近有展览吗") is Command.OUTING
    assert parse_command("在家活动活动筋骨") is Command.HELP  # bare 活动 doesn't trigger


class OneShot:
    def __init__(self, result):
        self.result = result

    def chat_json(self, messages, **kw):
        return self.result


def test_llm_chat_classification_is_overridden_when_message_asks_for_data():
    promise = {"intent": "chat", "reply": "爸爸，我帮您看看今天有什么活动～"}
    r = FamilyAssistant(OneShot(promise)).respond("dad", "爸爸", "今天有什么活动？", datetime(2026, 10, 13, 9))
    assert r.intent == "outing" and r.command is Command.OUTING


def test_run_llm_chat_pushes_reply_then_data_via_send_api(monkeypatch, settings, state):
    import main
    from bot import ChatBot
    s = dataclasses.replace(settings, dry_run=False, father=dataclasses.replace(settings.father, wecom_userid="dad"))

    class Recorder:
        def __init__(self):
            self.sent = []

        def send_text(self, users, text):
            self.sent.append(("text", text))

        def send_message(self, users, message, fmt):
            self.sent.append((fmt, message.title))

    rec = Recorder()
    monkeypatch.setattr(main, "get_services", lambda: main.Services(s, object(), rec, state))
    monkeypatch.setattr(main, "get_bot", lambda b=ChatBot(["dad"], llm_enabled=True): b)
    a = FamilyAssistant(OneShot({"intent": "chat", "reply": "爸爸，我帮您看看～"}))
    monkeypatch.setattr(main, "get_assistant", lambda: a)
    monkeypatch.setattr(main, "build_on_demand_message", lambda sv, cmd, name, record=False: Message(f"data:{cmd.value}", "md"))
    main.run_llm_chat("dad", "今天有什么活动？")
    assert rec.sent == [("text", "爸爸，我帮您看看～"), (s.wecom_message_type, "data:outing")]
