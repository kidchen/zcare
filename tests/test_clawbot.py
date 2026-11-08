import json

import pytest

from clients import clawbot_client as cc
from features import clawbot as cb
from tests.conftest import FakeResponse, FakeSession

ME = "o9cq80x@im.wechat"


def upd(msgs=(), buf="buf2", **extra):
    return FakeResponse({"ret": 0, "msgs": list(msgs), "get_updates_buf": buf, **extra})


def user_msg(text, frm=ME, mid="1", kind=cc.TEXT):
    item = {"type": kind, "text_item": {"text": text}} if kind == cc.TEXT else {"type": kind, "voice_item": {"text": text}}
    return {"message_id": mid, "from_user_id": frm, "message_type": cc.USER_MESSAGE, "item_list": [item],
            "context_token": "ctx-" + mid}


def test_requests_carry_the_protocol_headers_and_never_leak_the_token():
    s = FakeSession({"getupdates": [upd([user_msg("明天冷不冷")])], "sendmessage": [FakeResponse({"ret": 0})]})
    c = cc.ClawBotClient("SECRET", session=s)
    msgs, cursor, _ = c.get_updates("buf1")
    assert [(m.from_user, m.text, m.context_token) for m in msgs] == [(ME, "明天冷不冷", "ctx-1")] and cursor == "buf2"
    c.send_text(ME, "不冷，最高14°C", msgs[0].context_token)
    (_, url1, kw1), (_, url2, kw2) = s.calls
    assert url1.startswith("https://ilinkai.weixin.qq.com/ilink/bot/getupdates")
    h = kw1["headers"]
    assert h["Authorization"] == "Bearer SECRET" and h["AuthorizationType"] == "ilink_bot_token" and h["iLink-App-Id"] == "bot"
    assert json.loads(kw1["data"])["get_updates_buf"] == "buf1"
    sent = json.loads(kw2["data"])["msg"]
    assert sent["to_user_id"] == ME and sent["context_token"] == "ctx-1" and sent["message_type"] == cc.BOT_MESSAGE
    assert sent["item_list"] == [{"type": cc.TEXT, "text_item": {"text": "不冷，最高14°C"}}]
    with pytest.raises(cc.ClawBotError) as err:
        cc.ClawBotClient("SECRET", session=FakeSession({"getupdates": [FakeResponse({}, status=500)]})).get_updates("")
    assert "SECRET" not in str(err.value)


def test_voice_messages_use_their_transcript_and_bot_echoes_are_ignored():
    bot_echo = {**user_msg("x"), "message_type": cc.BOT_MESSAGE}
    got = cc.parse_inbound([user_msg("我想去公园", kind=cc.VOICE), bot_echo, {"message_type": 1, "item_list": []}])
    assert [m.text for m in got] == ["我想去公园"]


def test_stale_session_error_code():
    s = FakeSession({"getupdates": [FakeResponse({"ret": -14, "errcode": -14, "errmsg": "session timeout"})]})
    with pytest.raises(cc.ClawBotError) as err:
        cc.ClawBotClient("t", session=s).get_updates("")
    assert err.value.code == cc.STALE_SESSION


def test_runner_answers_only_the_scanner_saves_the_cursor_and_pauses_on_stale(tmp_path):
    acct = cb.ClawAccount("admin", "tok", "bot1", ME)
    s = FakeSession({"getupdates": [upd([user_msg("你好"), user_msg("hi", frm="stranger@im.wechat", mid="2")], buf="c2")],
                     "sendmessage": [FakeResponse({"ret": 0})]})
    seen = []
    runner = cb.ClawBotRunner(acct, lambda a, m, reply: (seen.append(m.text), reply("您好～")),
                              tmp_path / "admin.cursor", client=cc.ClawBotClient("tok", session=s))
    runner.poll_once("", 35)
    assert seen == ["你好"] and (tmp_path / "admin.cursor").read_text() == "c2"
    assert json.loads(s.calls[-1][2]["data"])["msg"]["context_token"] == "ctx-1"

    slept, problems = [], []
    stale = FakeSession({"getupdates": [FakeResponse({"ret": -14, "errcode": -14})]})
    r2 = cb.ClawBotRunner(acct, lambda *a: None, tmp_path / "x.cursor", client=cc.ClawBotClient("tok", session=stale),
                          on_problem=problems.append, sleep=lambda s_: (slept.append(s_), r2.stop()))
    r2.run()
    assert slept == [cb.STALE_PAUSE_S] and len(problems) == 1


def test_account_store_one_per_role(tmp_path):
    store = cb.AccountStore(tmp_path / "a.json")
    store.add(cb.ClawAccount("admin", "t1", "b1", "u1"))
    store.add(cb.ClawAccount("mother", "t2", "b2", "u2"))
    store.add(cb.ClawAccount("admin", "t3", "b3", "u1"))   # re-scan replaces
    assert sorted((a.role, a.bot_token) for a in store.all()) == [("admin", "t3"), ("mother", "t2")]
    assert store.remove("mother") and not store.remove("mother")


def test_login_flow_with_verify_code_and_redirect():
    statuses = iter([{"status": "scaned"}, {"status": "need_verifycode"}, {"status": "scaned_but_redirect", "redirect_host": "sh.ilinkai.weixin.qq.com"},
                     {"status": "confirmed", "bot_token": "T", "ilink_bot_id": "B", "ilink_user_id": ME, "baseurl": "https://ilinkai.weixin.qq.com"}])
    s = FakeSession({"get_bot_qrcode": [FakeResponse({"qrcode": "q1", "qrcode_img_content": "https://liteapp.weixin.qq.com/q/abc"})],
                     "get_qrcode_status": [lambda url, kw: FakeResponse(next(statuses))]})
    shown, said = [], []
    acct = cb.login(cc.ClawBotClient(session=s), "admin", shown.append, lambda p: "42", said.append, sleep=lambda s_: None)
    assert shown == ["https://liteapp.weixin.qq.com/q/abc"] and acct.bot_token == "T" and acct.user_id == ME
    status_calls = [c for c in s.calls if "get_qrcode_status" in c[1]]
    assert any(c[2]["params"].get("verify_code") == "42" for c in status_calls)
    assert status_calls[-1][1].startswith("https://sh.ilinkai.weixin.qq.com/")   # followed the redirect


def test_clawbot_message_reaches_the_assistant_and_replies_in_the_same_chat(monkeypatch, settings, state):
    import dataclasses

    import main
    from features.assistant import FamilyAssistant

    class OneShot:
        def chat_json(self, messages, **kw):
            return {"intent": "chat", "reply": "妈妈好，今天天气不错～"}

    sv = main.Services(dataclasses.replace(settings, dry_run=False, llm_api_key="k"), object(), None, state)
    monkeypatch.setattr(main, "get_services", lambda: sv)
    monkeypatch.setattr(main, "get_assistant", lambda: FamilyAssistant(OneShot(), assistant_name="小助理"))
    monkeypatch.setattr(main, "submit_background", lambda fn, *a: fn(*a))
    main.get_clawbot_bot.cache_clear()
    monkeypatch.setattr(main, "get_settings", lambda: sv.settings)
    replies = []
    main.on_clawbot_message(cb.ClawAccount("mother", "t", "b", ME), cc.Inbound("1", ME, "你好呀", "ctx"), replies.append)
    assert replies == ["妈妈好，今天天气不错～"]
    main.get_clawbot_bot.cache_clear()


def test_data_messages_keep_bold_and_markdown_links(settings, state):
    import dataclasses

    import main
    from messages import Message
    sv = main.Services(dataclasses.replace(settings, dry_run=False), object(), None, state)
    out = []
    ch = main.clawbot_chatter(sv, cb.ClawAccount("mother", "t", "b", ME), out.append)
    ch.send_message(Message("t", '**妈妈好**\n> 路况：<font color="info">畅通</font>\n\n\n\n[📍 打开高德地图导航](https://uri.amap.com/n?x=1)'))
    assert out == ["**妈妈好**\n路况：畅通\n\n[📍 打开高德地图导航](https://uri.amap.com/n?x=1)"]


def test_no_name_never_becomes_nin_as_a_name():
    from datetime import datetime

    from clients.amap_client import DailyForecast, LiveWeather
    from features.assistant import build_user_content
    from features.tv_recommend import _lead
    from features.weather import format_weather_message
    w = format_weather_message("", "你好", LiveWeather("晴", 9, "西", "≤3", 30, ""),
                               [DailyForecast("2026-11-02", "晴", "晴", 14, 0, "≤3")], datetime(2026, 11, 2).date())
    assert w.markdown.startswith("**你好 🌤") and "您你好" not in w.markdown
    assert _lead("") == "" and _lead("您") == "" and _lead("爸爸") == "爸爸，"
    ctx = build_user_content("早", "您", datetime(2026, 11, 2, 7), "小助理")
    assert "User=您" not in ctx and "不要称呼" in ctx
