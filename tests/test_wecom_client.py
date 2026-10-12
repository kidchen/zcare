import pytest

from clients.wecom_client import WeComClient, WeComError, truncate_utf8
from messages import Message
from tests.conftest import FakeResponse, FakeSession

TOKEN = FakeResponse({"errcode": 0, "access_token": "T1", "expires_in": 7200})


def test_token_is_cached():
    session = FakeSession({"/gettoken": [TOKEN], "/message/send": [FakeResponse({"errcode": 0})]})
    c = WeComClient("corp", 1000002, "sec", session=session)
    c.send_text(["dad"], "hi")
    c.send_text(["dad"], "hi again")
    assert sum(1 for _, url, _ in session.calls if "gettoken" in url) == 1


def test_expired_token_refreshes_and_retries_once():
    session = FakeSession({
        "/gettoken": [TOKEN, FakeResponse({"errcode": 0, "access_token": "T2", "expires_in": 7200})],
        "/message/send": [FakeResponse({"errcode": 42001, "errmsg": "expired"}), FakeResponse({"errcode": 0})],
    })
    WeComClient("corp", 1, "sec", session=session).send_markdown(["dad", "mom"], "**hi**")
    sends = [kw for m, url, kw in session.calls if "message/send" in url]
    assert [s["params"]["access_token"] for s in sends] == ["T1", "T2"]
    assert sends[0]["json"]["touser"] == "dad|mom"
    assert sends[0]["json"]["msgtype"] == "markdown"


def test_ip_not_allowed_error_has_hint():
    session = FakeSession({"/gettoken": [TOKEN], "/message/send": [FakeResponse({"errcode": 60020, "errmsg": "x"})]})
    with pytest.raises(WeComError, match="可信IP"):
        WeComClient("corp", 1, "sec", session=session).send_text(["dad"], "hi")


def test_text_fallback_strips_markdown():
    session = FakeSession({"/gettoken": [TOKEN], "/message/send": [FakeResponse({"errcode": 0})]})
    msg = Message("t", '**早上好**\n> 路况：<font color="info">畅通</font>')
    WeComClient("corp", 1, "sec", session=session).send_message(["dad"], msg, "text")
    body = session.calls[-1][2]["json"]
    assert body["text"]["content"] == "早上好\n路况：畅通"


def test_truncate_utf8_never_splits_characters():
    out = truncate_utf8("呼和浩特" * 300, 100)
    assert len(out.encode()) <= 100 and out.endswith("…")
