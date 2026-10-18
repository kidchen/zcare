import dataclasses

import pytest

from clients.pushplus_client import SEND_URL, PushPlusClient, PushPlusError
from features.push import push_title, render_push_html
from messages import Article, Message
from tests.conftest import FakeResponse, FakeSession

OK = FakeResponse({"code": 200, "msg": "执行成功", "data": "msgid1"})


def test_client_posts_html_over_https_and_returns_message_id():
    s = FakeSession({"pushplus.plus/send": [OK]})
    assert PushPlusClient("tok", session=s).send("标题", "<p>内容</p>", to=["f1", "f1", "f2"]) == "msgid1"
    (method, url, kw), = s.calls
    assert method == "POST" and url == SEND_URL and url.startswith("https://")
    assert kw["json"] == {"token": "tok", "title": "标题", "content": "<p>内容</p>", "template": "html",
                          "channel": "wechat", "to": "f1,f2"}
    PushPlusClient("tok", session=FakeSession({"send": [OK]})).send("t", "c")  # no `to` → the owner


@pytest.mark.parametrize("resp", [FakeResponse({"code": 905, "msg": "未实名"}), FakeResponse({}, status=500),
                                  FakeResponse("not json")])
def test_client_errors_never_contain_the_token(resp):
    with pytest.raises(PushPlusError) as err:
        PushPlusClient("SECRET-TOKEN", session=FakeSession({"send": [resp]})).send("t", "c")
    assert "SECRET-TOKEN" not in str(err.value)


def test_client_limits():
    c = PushPlusClient("tok", session=FakeSession({"send": [OK]}))
    with pytest.raises(PushPlusError):
        c.send("t", "c", to=[f"f{i}" for i in range(11)])
    with pytest.raises(PushPlusError):
        c.send("t", "x" * 20_001)
    with pytest.raises(ValueError):
        PushPlusClient("")


def test_push_title_puts_the_headline_first_without_emoji():
    assert push_title(Message("爸爸早上好 · 路况整体畅通", "md", headline="路况整体畅通 车程22分钟")) == "路况整体畅通 车程22分钟"
    assert push_title(Message("📰 今天呼和浩特的新鲜事（5条）", "md")) == "今天呼和浩特的新鲜事（5条）"
    assert push_title(Message("☀️", "")) == "家里的小助手"


def test_push_page_inline_styles_buttons_and_only_https_covers():
    md = "**妈妈早上好 🌷**\n> 🌤 天气：晴 9°C\n\n**1. 新闻标题**\n> 摘要\n[阅读原文](https://mp.weixin.qq.com/s/a)"
    page = render_push_html(Message("t", md, cover="https://mmbiz.qpic.cn/x/0?wx_fmt=jpeg"))
    assert "<style" not in page and 'class="' not in page                     # inline styles only
    assert 'href="https://mp.weixin.qq.com/s/a"' in page and "background:#0B57D0" in page
    assert 'referrerpolicy="no-referrer"' in page and page.index("<img") > page.index("</h1>")
    assert "<img" not in render_push_html(Message("t", md, cover="http://1.2.3.4:8000/posters/wx1.jpg"))  # mixed content
    assert "&lt;script&gt;" in render_push_html(Message("t", "<script>x</script>"))  # escaped


def test_feature_headlines_lead_with_the_key_fact(settings):
    from clients.amap_client import AmapClient
    from features import digest as dg
    from features.commute import build_commute_message
    from tests import fixtures as fx
    amap = AmapClient("k", session=FakeSession({"/direction/driving": [FakeResponse(fx.DRIVING)],
                                                "/weather": [FakeResponse(fx.LIVE), FakeResponse(fx.FORECAST)]}))
    assert build_commute_message(amap, settings).headline == "局部缓行 车程22分钟"
    items = [dg.DigestItem("供暖开始", "s", "https://mp.weixin.qq.com/s/a", "号", "k", "供暖开始",
                           "https://mmbiz.qpic.cn/c/0?wx_fmt=jpeg")]
    d = dg.format_digest_message(items)
    assert d.headline == "今晚1条本地新闻：供暖开始" and d.cover.startswith("https://mmbiz.qpic.cn/")


class Rec:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def send_message(self, users, message, msg_type):
        if self.fail:
            raise RuntimeError("wecom down")
        self.calls.append(users)

    def send(self, title, html, to=()):
        if self.fail:
            raise PushPlusError("rejected: code 999")
        self.calls.append((title, tuple(to)))


def _sv(settings, state, tmp_path, wecom, pushplus, channels=("wecom", "pushplus"), father_to="", mother_to=""):
    import main
    from features.activity import ActivityLog
    s = dataclasses.replace(settings, dry_run=False, delivery_channels=channels, pushplus_token="tok",
                            father=dataclasses.replace(settings.father, wecom_userid="dad", pushplus_to=father_to),
                            mother=dataclasses.replace(settings.mother, wecom_userid="mom", pushplus_to=mother_to))
    return main.Services(s, object(), wecom, state, activity=ActivityLog(tmp_path / "a.jsonl"), pushplus=pushplus)


def test_scheduled_jobs_go_to_every_channel(monkeypatch, settings, state, tmp_path):
    import main
    wecom, pp = Rec(), Rec()
    sv = _sv(settings, state, tmp_path, wecom, pp, mother_to="momtoken")
    monkeypatch.setattr(main, "get_services", lambda: sv)
    monkeypatch.setitem(main.JOBS, "digest", dataclasses.replace(
        main.JOBS["digest"], build=lambda sv_, record: Message("📰 新鲜事", "**新鲜事**", headline="今晚3条本地新闻")))
    assert main.run_job("digest")["sent"]
    assert wecom.calls == [["dad", "mom"]]
    assert pp.calls == [("今晚3条本地新闻", ()), ("今晚3条本地新闻", ("momtoken",))]  # dad = owner ("") while testing
    (sent,) = [e for e in sv.activity.since(60) if e["type"] == "sent"]
    assert sent["channels"] == ["wecom", "pushplus"]


def test_one_channel_failing_still_delivers_the_other_and_is_reported(monkeypatch, settings, state, tmp_path):
    import main
    wecom = Rec()
    sv = _sv(settings, state, tmp_path, wecom, Rec(fail=True))
    monkeypatch.setattr(main, "get_services", lambda: sv)
    monkeypatch.setitem(main.JOBS, "tv", dataclasses.replace(main.JOBS["tv"], build=lambda sv_, record: Message("t", "md")))
    assert main.run_job("tv")["sent"] and wecom.calls
    errors = [e for e in sv.activity.since(60) if e["type"] == "error"]
    assert errors and "PushPlus" in errors[0]["what"]


def test_all_channels_failing_raises(monkeypatch, settings, state, tmp_path):
    import main
    sv = _sv(settings, state, tmp_path, Rec(fail=True), Rec(fail=True))
    monkeypatch.setattr(main, "get_services", lambda: sv)
    monkeypatch.setitem(main.JOBS, "tv", dataclasses.replace(main.JOBS["tv"], build=lambda sv_, record: Message("t", "md")))
    with pytest.raises(RuntimeError):
        main.run_job("tv")


def test_pushplus_only_works_without_wecom_for_scheduled_messages(monkeypatch, settings, state, tmp_path):
    import main
    pp = Rec()
    sv = _sv(settings, state, tmp_path, None, pp, channels=("pushplus",))
    monkeypatch.setattr(main, "get_services", lambda: sv)
    monkeypatch.setitem(main.JOBS, "commute", dataclasses.replace(
        main.JOBS["commute"], build=lambda sv_, record: Message("t", "md", articles=(Article("a", "b", "https://uri.amap.com/x"),))))
    assert main.run_job("commute")["sent"] and pp.calls == [("t", ())]


def test_config_validates_delivery_channels(monkeypatch):
    from config import ConfigError, load_settings
    monkeypatch.setenv("DELIVERY_CHANNELS", "wecom,pushplus")
    monkeypatch.delenv("PUSHPLUS_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("DELIVERY_CHANNELS", "wecom,sms")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("DELIVERY_CHANNELS", "pushplus, wecom")
    monkeypatch.setenv("PUSHPLUS_TOKEN", "tok")
    monkeypatch.setenv("MOTHER_PUSHPLUS_TO", "momtoken")
    s = load_settings()
    assert s.delivery_channels == ("pushplus", "wecom") and s.mother.pushplus_to == "momtoken" and s.father.pushplus_to == ""
