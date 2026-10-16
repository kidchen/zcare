import dataclasses
import json
from datetime import datetime

import pytest
import requests

from bot import RATE_LIMIT_TEXT, ChatBot, Command
from clients.llm_client import LLMClient, LLMError
from clients.wecom_callback import IncomingMessage
from features.assistant import FamilyAssistant, keyword_fallback
from tests.conftest import FakeResponse, FakeSession

NOW = datetime(2026, 10, 13, 10, 30)
DAD, MOM, ME = "dad01", "mom01", "me01"


def completion(content, finish="stop"):
    return FakeResponse({"choices": [{"message": {"role": "assistant", "content": content},
                                      "finish_reason": finish}]})


def llm(*responses, provider="bigmodel"):
    session = FakeSession({"/chat/completions": list(responses)})
    return LLMClient("sk-test", provider=provider, session=session, retry_delay_s=0), session


def api_error(status, code, message="x"):
    return FakeResponse({"error": {"code": code, "message": message}}, status=status)


# ---------------------------------------------------------------- LLM client

def test_payload_disables_thinking_and_requests_json():
    client, session = llm(completion('{"intent": "chat", "reply": "好"}'))
    assert client.chat_json([{"role": "user", "content": "hi"}]) == {"intent": "chat", "reply": "好"}
    _, url, kw = session.calls[0]
    assert url == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer sk-test"
    body = kw["json"]
    assert body["model"] == "glm-4-flash-250414"
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"} and body["stream"] is False


def test_retries_once_on_empty_content_then_succeeds():
    client, session = llm(completion(""), completion('{"intent": "tv", "reply": "好的"}'))
    assert client.chat_json([])["intent"] == "tv" and len(session.calls) == 2


def test_auth_error_is_not_retried_and_has_hint():
    client, session = llm(api_error(401, "1000", "身份验证失败"))
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        client.chat_json([])
    assert len(session.calls) == 1


def test_arrears_429_is_not_retried_and_points_to_console():
    client, session = llm(api_error(429, "1113", "您的账户已欠费"))
    with pytest.raises(LLMError, match="arrears.*bigmodel.cn"):
        client.chat_json([])
    assert len(session.calls) == 1


def test_overload_429_is_retried():
    client, session = llm(api_error(429, "1305"), completion('{"intent": "chat", "reply": "嗯"}'))
    assert client.chat_json([])["reply"] == "嗯" and len(session.calls) == 2


def test_fallback_model_is_used_when_primary_is_overloaded():
    client, session = llm(api_error(429, "1305"), completion('{"intent": "chat", "reply": "嗯"}'))
    client.chat_json([])
    assert [kw["json"]["model"] for _, _, kw in session.calls] == ["glm-4-flash-250414", "glm-4.7-flash"]


def test_fallback_can_be_overridden_or_disabled():
    assert LLMClient("k", fallback_model="glm-4.5-air").models == ["glm-4-flash-250414", "glm-4.5-air"]
    assert LLMClient("k", fallback_model="none").models == ["glm-4-flash-250414"]
    assert LLMClient("k", provider="deepseek").models == ["deepseek-flash"]


def test_rate_limit_backs_off_exponentially_and_succeeds_on_third_try(monkeypatch):
    import clients.llm_client as mod
    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    session = FakeSession({"/chat/completions": [
        api_error(429, "1302"), api_error(429, "1302"), completion('{"intent": "tv", "reply": "好"}')]})
    client = LLMClient("k", session=session)  # default delays
    assert client.chat_json([])["intent"] == "tv"
    assert sleeps == [2.0, 4.0] and len(session.calls) == 3


def test_gives_up_after_two_overloaded_responses():
    client, _ = llm(api_error(429, "1305"), api_error(429, "1305"))
    with pytest.raises(LLMError, match="overloaded"):
        client.chat_json([])


def test_sensitive_finish_reason_raises():
    client, _ = llm(completion("", finish="sensitive"))
    with pytest.raises(LLMError, match="safety"):
        client.chat_json([])


def test_deepseek_preset_and_overrides():
    client, session = llm(completion('{"a": 1}'), provider="deepseek")
    client.chat_json([])
    assert session.calls[0][1] == "https://api.deepseek.com/chat/completions"
    assert session.calls[0][2]["json"]["model"] == "deepseek-flash"
    custom = LLMClient("k", model="glm-4.5-air", base_url="https://proxy.example/v4/")
    assert custom.model == "glm-4.5-air" and custom._url == "https://proxy.example/v4/chat/completions"
    with pytest.raises(ValueError):
        LLMClient("k", provider="openai")


# ---------------------------------------------------------------- assistant

class ScriptedLLM:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def chat_json(self, messages, **kw):
        self.calls.append(messages)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_classifies_and_maps_to_command():
    a = FamilyAssistant(ScriptedLLM({"intent": "traffic", "reply": "好的爸爸，我这就帮您查查～"}))
    r = a.respond(DAD, "爸爸", "今天上班堵吗", NOW)
    assert r.command is Command.COMMUTE and r.reply.startswith("好的爸爸") and r.source == "llm"
    assert not r.urgent


def test_system_prompt_is_static_and_context_goes_in_user_turn():
    from datetime import timedelta

    from features.assistant import SYSTEM_PROMPT
    fake = ScriptedLLM(*[{"intent": "chat", "reply": "嗯"}] * 3)
    a = FamilyAssistant(fake, assistant_name="小暖")
    a.respond(MOM, "妈妈", "吃了吗", NOW)
    a.respond(DAD, "爸爸", "你好", NOW + timedelta(hours=5, minutes=17))
    a.respond(MOM, "妈妈", "在吗", NOW + timedelta(days=1))
    systems = {call[0]["content"] for call in fake.calls}
    assert systems == {SYSTEM_PROMPT}  # byte-identical across users and times → cacheable
    assert all(call[0]["role"] == "system" for call in fake.calls)
    assert "JSON" in SYSTEM_PROMPT and "{" in SYSTEM_PROMPT  # json mode requirement + literal schema
    for forbidden in ("妈妈，", "爸爸，", "2026", "星期", "小暖"):
        assert forbidden not in SYSTEM_PROMPT, forbidden
    user_turn = fake.calls[1][-1]["content"]
    assert user_turn == ("[Context: Time=2026-10-13 15:47 星期二, User=爸爸, Location=呼和浩特, Assistant=小暖]\n"
                         "User Query: 你好")


def test_history_is_replayed_verbatim_so_prefix_grows():
    fake = ScriptedLLM({"intent": "chat", "reply": "一"}, {"intent": "chat", "reply": "二"})
    a = FamilyAssistant(fake)
    a.respond(MOM, "妈妈", "第一句", NOW)
    a.respond(MOM, "妈妈", "第二句", NOW)
    first_call, second_call = fake.calls
    assert second_call[:2] == first_call  # call 2 starts with call 1's exact messages
    assert second_call[1]["content"].endswith("User Query: 第一句")


def test_system_prompt_is_a_module_constant_never_formatted():
    import inspect

    import features.assistant as mod
    assert isinstance(mod.SYSTEM_PROMPT, str)
    assert "SYSTEM_PROMPT.format" not in inspect.getsource(mod)

def test_history_is_kept_per_user():
    fake = ScriptedLLM({"intent": "chat", "reply": "一"}, {"intent": "chat", "reply": "二"},
                       {"intent": "chat", "reply": "三"})
    a = FamilyAssistant(fake)
    a.respond(MOM, "妈妈", "第一句", NOW)
    a.respond(MOM, "妈妈", "第二句", NOW)
    a.respond(DAD, "爸爸", "别人", NOW)
    roles = [m["role"] for m in fake.calls[1]]
    assert roles == ["system", "user", "assistant", "user"]
    assert json.loads(fake.calls[1][2]["content"])["reply"] == "一"
    assert len(fake.calls[2]) == 2  # dad has no history


def test_unknown_intent_becomes_chat_and_long_reply_truncated():
    r = FamilyAssistant(ScriptedLLM({"intent": "weather??", "reply": "好" * 500})).respond(MOM, "妈妈", "x", NOW)
    assert r.intent == "chat" and r.command is None and len(r.reply) <= 301


def test_keyword_safety_net_overrides_llm():
    r = FamilyAssistant(ScriptedLLM({"intent": "chat", "reply": "注意休息"})).respond(MOM, "妈妈", "有点胸闷", NOW)
    assert r.urgent and r.intent == "chat"


def test_falls_back_to_keywords_when_llm_down():
    a = FamilyAssistant(ScriptedLLM(LLMError("402"), requests.ConnectionError("x")))
    r = a.respond(DAD, "爸爸", "看看电视剧", NOW)
    assert (r.intent, r.source) == ("tv", "fallback")
    r2 = a.respond(MOM, "妈妈", "有人让我转账到安全账户", NOW)
    assert r2.urgent and "120" in r2.reply


def test_keyword_fallback_help():
    assert keyword_fallback("你好", "爸爸").intent == "chat"


# ---------------------------------------------------------------- bot LLM mode

def msg(content, user=DAD, msg_id="1"):
    return IncomingMessage("corp", user, 0, "text", content, msg_id, "1")


def test_bot_llm_mode_routes_everything_to_chat_with_hourly_budget():
    now = [0.0]
    bot = ChatBot([DAD], clock=lambda: now[0], llm_enabled=True, llm_max_per_hour=2)
    assert bot.handle(msg("路况", msg_id="a")).chat
    assert bot.handle(msg("你好", msg_id="b")).chat
    assert bot.handle(msg("再聊", msg_id="c")).reply == RATE_LIMIT_TEXT
    assert bot.handle(msg("再聊", msg_id="c")).reply is None  # duplicate MsgId
    now[0] += 3601
    assert bot.handle(msg("又来了", msg_id="d")).chat
    assert bot.handle(msg("x", user="stranger", msg_id="e")).reply is None


# ---------------------------------------------------------------- run_llm_chat

class FakeWeCom:
    def __init__(self):
        self.texts = []

    def send_text(self, users, content):
        self.texts.append((users, content))


@pytest.fixture
def wired(monkeypatch, settings, state):
    import main
    s = dataclasses.replace(
        settings, dry_run=False, admin_userid=ME,
        father=dataclasses.replace(settings.father, wecom_userid=DAD),
        mother=dataclasses.replace(settings.mother, wecom_userid=MOM),
    )
    wecom = FakeWeCom()
    jobs = []
    monkeypatch.setattr(main, "get_services", lambda: main.Services(s, object(), wecom, state))
    monkeypatch.setattr(main, "get_bot", lambda bot=ChatBot([DAD, MOM], llm_enabled=True): bot)
    monkeypatch.setattr(main, "run_on_demand", lambda cmd, user: jobs.append((cmd, user)))

    def use(*llm_results):
        assistant = FamilyAssistant(ScriptedLLM(*llm_results))
        monkeypatch.setattr(main, "get_assistant", lambda: assistant)
    return main, wecom, jobs, use


def test_run_llm_chat_replies_then_runs_job_once_per_cooldown(wired):
    main, wecom, jobs, use = wired
    use({"intent": "tv", "reply": "好的爸爸～"}, {"intent": "tv", "reply": "马上～"})
    main.run_llm_chat(DAD, "有啥好剧")
    main.run_llm_chat(DAD, "再推荐点剧")
    assert wecom.texts == [([DAD], "好的爸爸～"), ([DAD], "马上～")]
    assert jobs == [(Command.TV, DAD)]  # second request inside 60 s cooldown


def test_run_llm_chat_urgent_alerts_admin(wired):
    main, wecom, jobs, use = wired
    use({"intent": "urgent", "reply": "妈妈，请马上拨打120！"})
    main.run_llm_chat(MOM, "心口疼得厉害")
    assert wecom.texts[0] == ([MOM], "妈妈，请马上拨打120！")
    assert wecom.texts[1][0] == [ME] and "心口疼得厉害" in wecom.texts[1][1]
    assert jobs == []


def test_route_with_llm_enabled_returns_empty_and_schedules_chat(monkeypatch):
    import base64
    import os
    import time

    from fastapi.testclient import TestClient

    import config
    import main
    from clients.wecom_crypto import WeComCrypto
    key = base64.b64encode(os.urandom(32)).decode().rstrip("=")
    for k, v in {"WECOM_CALLBACK_TOKEN": "t", "WECOM_ENCODING_AES_KEY": key, "WECOM_CORP_ID": "corp",
                 "FATHER_WECOM_USERID": DAD, "LLM_API_KEY": "sk-test"}.items():
        monkeypatch.setenv(k, v)
    caches = (config.get_settings, main.get_services, main.get_wecom_crypto, main.get_bot, main.get_assistant, main.get_llm, main.get_wemp_admin)
    for fn in caches:
        fn.cache_clear()
    calls = []
    monkeypatch.setattr(main, "run_llm_chat", lambda user, text: calls.append((user, text)))
    monkeypatch.setattr(main, "submit_background", lambda fn, *a: fn(*a))  # deterministic
    try:
        crypto = WeComCrypto("t", key, "corp")
        inner = (f"<xml><ToUserName>corp</ToUserName><FromUserName>{DAD}</FromUserName><CreateTime>1</CreateTime>"
                 "<MsgType>text</MsgType><Content>今天心情不错</Content><MsgId>77</MsgId></xml>")
        enc = crypto.encrypt(inner)
        ts = str(int(time.time()))
        body = f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>".encode()
        with TestClient(main.app) as client:
            r = client.post("/wecom/webhook", content=body,
                            params={"msg_signature": crypto.signature(ts, "n", enc), "timestamp": ts, "nonce": "n"})
        assert r.status_code == 200 and r.text == ""
        assert calls == [(DAD, "今天心情不错")]
    finally:
        for fn in caches:
            fn.cache_clear()


def test_health_emergency_reply_always_leads_with_120():
    soft = {"intent": "urgent", "reply": "妈妈，您先休息一下，如果没有好转再打120。"}
    r = FamilyAssistant(ScriptedLLM(soft)).respond(MOM, "妈妈", "胸闷喘不上气", NOW)
    assert r.reply.startswith("妈妈，请马上拨打 120！")
    ok = {"intent": "urgent", "reply": "妈妈，请马上拨打120！我已经告诉孩子了。"}
    r2 = FamilyAssistant(ScriptedLLM(ok)).respond(MOM, "妈妈", "胸闷", NOW)
    assert r2.reply == ok["reply"]  # not doubled
    scam = {"intent": "urgent", "reply": "妈妈，这可能是诈骗，先别转账。"}
    r3 = FamilyAssistant(ScriptedLLM(scam)).respond(MOM, "妈妈", "让我转账", NOW)
    assert "120" not in r3.reply  # scams don't get an ambulance prompt


@pytest.mark.parametrize("llm_reply", [
    "妈妈，请马上拨打 120！先坐下别走动，把门打开，我已经通知孩子了。",  # ambulance script for a scam
    "妈妈，别担心，我帮您看看。",                                         # no scam advice at all
])
def test_scam_messages_always_get_scam_advice(llm_reply):
    r = FamilyAssistant(ScriptedLLM({"intent": "urgent", "reply": llm_reply})).respond(
        MOM, "妈妈", "有人打电话说我账户被冻结了要转账", NOW)
    assert "120" not in r.reply and "先别转账" in r.reply and "给孩子打个电话" in r.reply and r.urgent


def test_good_scam_reply_from_llm_is_kept():
    good = {"intent": "urgent", "reply": "妈妈，先别转账，也别说验证码，先给孩子打个电话问问。"}
    r = FamilyAssistant(ScriptedLLM(good)).respond(MOM, "妈妈", "让我转账到安全账户", NOW)
    assert r.reply == good["reply"]


def test_truncated_reply_fails_fast_without_retrying():
    client, session = llm(completion('{"events": [{"title": "半截', finish="length"))
    with pytest.raises(LLMError, match="truncated"):
        client.chat_json([], max_tokens=1500, purpose="event-extract")
    assert len(session.calls) == 1


def test_per_call_timeout_and_attempts_override_defaults(monkeypatch):
    import clients.llm_client as mod
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    session = FakeSession({"/chat/completions": [api_error(429, "1305"), api_error(429, "1305"), api_error(429, "1305")]})
    client = LLMClient("k", session=session, timeout=20)
    with pytest.raises(LLMError, match="2 attempts"):
        client.chat_json([], timeout=120, max_attempts=2)
    assert [kw["timeout"] for _, _, kw in session.calls] == [120, 120]


def test_extraction_uses_long_timeout_and_large_output_budget(state):
    from datetime import date

    from clients.wemp_rss import Article
    from features import events as ev
    seen = {}

    class Spy:
        def chat_json(self, messages, **kw):
            seen.update(kw)
            return {"events": []}

    class Feed:
        def articles(self):
            return [Article("演出预告", "10月17日 二人台专场 乌兰恰特大剧院", date(2026, 10, 12), "a")]

    ev.EventFinder(Spy(), state, articles=Feed()).find(date(2026, 10, 13))
    assert seen["timeout"] == ev.EXTRACTION_TIMEOUT_S >= 60 and seen["max_attempts"] == 2
    assert seen["max_tokens"] >= 2000 and seen["purpose"] == "extract"



def test_usage_counters_per_purpose():
    client, _ = llm(completion('{"intent": "chat", "reply": "嗯"}'))
    client.chat_json([], purpose="chat")
    failing, _ = llm(api_error(401, "1000"))
    with pytest.raises(LLMError):
        failing.chat_json([], purpose="extract")
    (day, by_purpose), = client.usage_summary().items()
    assert by_purpose["chat"]["calls"] == 1 and by_purpose["chat"]["ok"] == 1
    assert failing.usage_summary()[day]["extract"]["failed"] == 1


def test_chat_uses_lower_temperature():
    seen = {}

    class Spy:
        def chat_json(self, messages, **kw):
            seen.update(kw)
            return {"intent": "chat", "reply": "嗯"}
    FamilyAssistant(Spy()).respond("mom", "妈妈", "你好", NOW)
    assert seen["temperature"] == 0.5 and seen["purpose"] == "chat"


def test_warm_up_job_is_scheduled_after_wemp_fetches(settings):
    import dataclasses

    import main
    sched = main.build_scheduler(dataclasses.replace(settings, wemp_rss_url="http://we-mp-rss:8001", llm_api_key="k"))
    job = {j.id: j for j in sched.get_jobs()}["events_warm"]
    assert "hour='6,11,17'" in repr(job.trigger) and "minute='45'" in repr(job.trigger)
