import base64
import dataclasses
import os
import time

import pytest

from bot import ACK_TEXT, COOLDOWN_TEXT, HELP_TEXT, ChatBot, Command, parse_command
from clients.wecom_callback import (
    CallbackFormatError,
    IncomingMessage,
    build_passive_reply,
    extract_encrypt,
    parse_message,
)
from clients.wecom_crypto import WeComCrypto

TOKEN, CORP = "tok", "wwcorp123"
AES_KEY = base64.b64encode(os.urandom(32)).decode().rstrip("=")
DAD, MOM = "dad01", "mom01"


def text_msg(content, user=DAD, msg_id="1"):
    return IncomingMessage(CORP, user, 0, "text", content, msg_id, "1000002")


# ---------------------------------------------------------------- command parsing

@pytest.mark.parametrize("text,cmd", [
    ("路况", Command.COMMUTE), ("今天堵不堵？", Command.COMMUTE),
    ("有什么好看的电视剧", Command.TV), ("剧", Command.TV),
    ("今天去哪玩", Command.OUTING), ("想出门逛逛", Command.OUTING),
    ("你好", Command.HELP), ("", Command.HELP),
])
def test_parse_command(text, cmd):
    assert parse_command(text) is cmd


def test_bot_reactions():
    now = [1000.0]
    bot = ChatBot([DAD, MOM], cooldown_s=60, clock=lambda: now[0])
    assert bot.handle(text_msg("路况", msg_id="a")) .command is Command.COMMUTE
    assert bot.handle(text_msg("路况", msg_id="a")).reply is None        # WeCom retry, same MsgId
    assert bot.handle(text_msg("路况", msg_id="b")).reply == COOLDOWN_TEXT
    assert bot.handle(text_msg("路况", user=MOM, msg_id="c")).command is Command.COMMUTE  # per-user
    now[0] += 61
    r = bot.handle(text_msg("路况", msg_id="d"))
    assert r.command is Command.COMMUTE and r.reply == ACK_TEXT[Command.COMMUTE]
    assert bot.handle(text_msg("嗨", msg_id="e")) .reply == HELP_TEXT
    assert bot.handle(text_msg("路况", user="stranger", msg_id="f")).reply is None
    event = IncomingMessage(CORP, DAD, 0, "event", "", "", "1", event="enter_agent")
    assert bot.handle(event).reply is None


# ---------------------------------------------------------------- XML

INNER = (f"<xml><ToUserName><![CDATA[{CORP}]]></ToUserName><FromUserName><![CDATA[{DAD}]]></FromUserName>"
         "<CreateTime>1700000000</CreateTime><MsgType><![CDATA[text]]></MsgType>"
         "<Content><![CDATA[今天路况怎么样]]></Content><MsgId>123</MsgId><AgentID>1000002</AgentID></xml>")


def test_parse_message():
    m = parse_message(INNER)
    assert (m.from_user, m.msg_type, m.content, m.msg_id) == (DAD, "text", "今天路况怎么样", "123")


def test_rejects_entity_expansion():
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><xml><Encrypt>&a;</Encrypt></xml>'
    with pytest.raises(CallbackFormatError):
        extract_encrypt(evil.encode())


def test_rejects_missing_encrypt_and_oversize():
    with pytest.raises(CallbackFormatError):
        extract_encrypt(b"<xml><Other/></xml>")
    with pytest.raises(CallbackFormatError):
        extract_encrypt(b"<xml>" + b"a" * 70000 + b"</xml>")


def test_passive_reply_roundtrip_and_cdata_escape():
    crypto = WeComCrypto(TOKEN, AES_KEY, CORP)
    xml = build_passive_reply(crypto, to_user=DAD, corp_id=CORP, content="a]]>b", timestamp="1", nonce="n")
    from xml.etree.ElementTree import fromstring
    root = fromstring(xml)
    enc = root.findtext("Encrypt")
    assert root.findtext("TimeStamp") == "1" and root.findtext("Nonce") == "n"
    crypto.verify_signature(root.findtext("MsgSignature"), "1", "n", enc)  # signature taken from reply
    inner = parse_message(crypto.decrypt(enc))
    assert inner.to_user == DAD and inner.content == "a]]>b"


# ---------------------------------------------------------------- route

@pytest.fixture
def app_client(monkeypatch):
    from fastapi.testclient import TestClient

    import config
    import main
    monkeypatch.setenv("WECOM_CALLBACK_TOKEN", TOKEN)
    monkeypatch.setenv("WECOM_ENCODING_AES_KEY", AES_KEY)
    monkeypatch.setenv("WECOM_CORP_ID", CORP)
    monkeypatch.setenv("FATHER_WECOM_USERID", DAD)
    caches = (config.get_settings, main.get_services, main.get_wecom_crypto, main.get_bot, main.get_assistant, main.get_llm, main.get_wemp_admin)
    for fn in caches:
        fn.cache_clear()
    calls = []
    monkeypatch.setattr(main, "run_on_demand", lambda cmd, user: calls.append((cmd, user)))
    monkeypatch.setattr(main, "submit_background", lambda fn, *a: fn(*a))  # deterministic
    with TestClient(main.app) as client:
        yield client, WeComCrypto(TOKEN, AES_KEY, CORP), calls
    for fn in caches:
        fn.cache_clear()


def post(client, crypto, inner, ts=None, sig=None):
    ts = ts or str(int(time.time()))
    enc = crypto.encrypt(inner)
    body = f"<xml><ToUserName><![CDATA[{CORP}]]></ToUserName><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
    params = {"msg_signature": sig or crypto.signature(ts, "nn", enc), "timestamp": ts, "nonce": "nn"}
    return client.post("/wecom/webhook", params=params, content=body.encode())


def test_route_acks_and_schedules_background_job(app_client):
    client, crypto, calls = app_client
    resp = post(client, crypto, INNER)
    assert resp.status_code == 200
    reply = parse_message(crypto.decrypt(extract_encrypt(resp.content)))
    assert reply.to_user == DAD and reply.content == ACK_TEXT[Command.COMMUTE]
    assert calls == [(Command.COMMUTE, DAD)]


def test_route_rejects_bad_signature_and_stale_timestamp(app_client):
    client, crypto, calls = app_client
    assert post(client, crypto, INNER, sig="0" * 40).status_code == 403
    assert post(client, crypto, INNER, ts=str(int(time.time()) - 7200)).status_code == 403
    assert client.post("/wecom/webhook", params={"msg_signature": "x", "timestamp": "1", "nonce": "n"},
                       content=b"not xml").status_code == 400
    assert calls == []


def test_route_empty_reply_for_events(app_client):
    client, crypto, calls = app_client
    event = (f"<xml><ToUserName>{CORP}</ToUserName><FromUserName>{DAD}</FromUserName>"
             "<MsgType>event</MsgType><Event>enter_agent</Event></xml>")
    resp = post(client, crypto, event)
    assert resp.status_code == 200 and resp.text == "" and calls == []


# ---------------------------------------------------------------- on-demand runner

class FakeWeCom:
    def __init__(self, fail=False):
        self.sent, self.texts, self.fail = [], [], fail

    def send_message(self, users, message, fmt):
        self.sent.append((users, message))

    def send_text(self, users, content):
        self.texts.append((users, content))


def test_run_on_demand_sends_to_requester(monkeypatch, settings, state):
    import main
    s = dataclasses.replace(settings, dry_run=False, father=dataclasses.replace(settings.father, wecom_userid=DAD))
    wecom = FakeWeCom()
    monkeypatch.setattr(main, "get_services", lambda: main.Services(s, object(), wecom, state))
    seen = {}

    def fake_commute(amap, st, name=None, greeting="早上好", **kw):
        seen.update(name=name, greeting=greeting)
        from messages import Message
        return Message("t", "md")

    monkeypatch.setattr(main, "build_commute_message", fake_commute)
    main.run_on_demand(Command.COMMUTE, DAD)
    assert wecom.sent[0][0] == [DAD] and seen == {"name": "爸爸", "greeting": "你好"}


def test_run_on_demand_apologizes_on_failure(monkeypatch, settings, state):
    import main
    s = dataclasses.replace(settings, dry_run=False)
    wecom = FakeWeCom()
    monkeypatch.setattr(main, "get_services", lambda: main.Services(s, object(), wecom, state))
    monkeypatch.setattr(main, "build_tv_message", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("douban 418")))
    main.run_on_demand(Command.TV, MOM)
    assert wecom.texts[0] == ([MOM], "抱歉，暂时没查到，请稍后再试～")
