"""Preference feedback loop, weekend/holiday specialization, graceful degradation."""
import dataclasses
from datetime import date, datetime

import pytest
import requests

from clients.amap_client import AmapClient
from features import outing
from features.daytype import day_info, off_days
from features.events import LocalEvent, LocalNotice
from features.preferences import Feedback, PreferenceStore
from messages import Message
from tests import fixtures as fx
from tests.conftest import FakeResponse, FakeSession

TUE = date(2026, 10, 13)
FRI = date(2026, 10, 16)
SAT = date(2026, 10, 17)


def down(url, kw):
    raise requests.ConnectionError("network down")


def amap(weather=None, poi=None, transit=None, driving=None):
    return AmapClient("k", session=FakeSession({
        "/weather": weather or [FakeResponse(fx.LIVE), FakeResponse(fx.FORECAST)],
        "/place/text": [poi or fx.poi_echo()],
        "/direction/transit": [transit or FakeResponse(fx.TRANSIT)],
        "/direction/driving": [driving or FakeResponse(fx.DRIVING)],
    }))


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ================================================================ 1. preferences

@pytest.fixture
def prefs(state):
    clock = Clock()
    p = PreferenceStore(state, clock=clock)
    p.clock = clock
    p.remember_recommendations([
        {"title": "民歌音乐会", "venue": "内蒙古体育馆", "category": "演出"},
        {"title": "少儿美术展", "venue": "呼和浩特市美术馆", "category": "展览"},
    ])
    return p


@pytest.mark.parametrize("text,raw,expected", [
    # shapes GLM actually returned in live tests, plus the parent's words
    ("这个太远了", {"target": "venue", "value": "内蒙古体育馆·演出", "reason": "too_far"},
     Feedback("venue", "内蒙古体育馆", "too_far")),
    ("美术馆那个我们上周去过了", {"target": "category", "value": "展览", "reason": "visited"},
     Feedback("venue", "呼和浩特市美术馆", "visited")),           # place, not "all exhibitions"
    ("我们不太喜欢看展览", {"target": "展览", "value": "美术馆展览", "reason": "not_interested"},
     Feedback("category", "展览", "not_interested")),
    ("上次推荐的演出挺好的", {"target": "民歌音乐会", "value": "内蒙古体育馆·演出", "reason": "like"},
     Feedback("category", "演出", "like")),
    ("那个音乐会没意思", {"reason": "not_interested", "value": ""}, Feedback("venue", "内蒙古体育馆", "not_interested")),
    ("长城去过了", {"reason": "visited", "value": "长城"}, None),     # never recommended
    ("挺好", {"reason": "hate"}, None),
])
def test_feedback_target_is_resolved_from_words_and_recent_picks(prefs, text, raw, expected):
    assert prefs.resolve(raw, text) == expected


def test_resolve_rejects_non_dict(prefs):
    assert prefs.resolve("not a dict", "这个太远了") is None


def test_recent_recommendations_expire_after_three_days(prefs):
    assert len(prefs.recent_recommendations()) == 2
    prefs.clock.t += 4 * 86400
    assert prefs.recent_recommendations() == []
    assert prefs.resolve({"target": "venue", "value": "内蒙古体育馆", "reason": "visited"}, "体育馆去过了") is None


def test_recorded_feedback_blocks_venues_penalizes_categories_and_expires(prefs):
    prefs.record(Feedback("venue", "内蒙古体育馆", "visited"))
    assert prefs.venue_blocked("内蒙古体育馆") and prefs.venue_blocked("内蒙古体育馆-主馆")
    prefs.record(Feedback("category", "展览", "not_interested"))
    assert prefs.category_penalty("展览") == 1
    prefs.record(Feedback("category", "展览", "not_interested"))
    assert prefs.category_penalty("展览") == 2  # said twice → excluded
    prefs.record(Feedback("category", "民俗", "like"))
    assert prefs.category_penalty("民俗") == -1
    assert "不去内蒙古体育馆" in prefs.summary() and "少推荐展览" in prefs.summary()
    prefs.clock.t += 31 * 86400
    assert not prefs.venue_blocked("内蒙古体育馆")  # "visited" lasts 30 days


def test_two_too_far_complaints_limit_route_length(prefs):
    assert prefs.max_route_minutes() is None
    prefs.record(Feedback("venue", "内蒙古体育馆", "too_far"))
    prefs.record(Feedback("venue", "呼和浩特市美术馆", "too_far"))
    assert prefs.max_route_minutes() == 45


def test_keyword_fallback_uses_latest_recommendation(prefs):
    assert prefs.fallback_from_text("这个太远了") == Feedback("venue", "内蒙古体育馆", "too_far")
    assert prefs.fallback_from_text("美术馆我们去过了") == Feedback("venue", "呼和浩特市美术馆", "visited")
    assert prefs.fallback_from_text("今天天气不错") is None


def test_curated_places_and_events_respect_preferences(prefs):
    prefs.record(Feedback("venue", "内蒙古博物院", "visited"))
    prefs.record(Feedback("category", "公园", "not_interested"))
    prefs.record(Feedback("category", "公园", "not_interested"))
    names = [p.name for p in outing.order_candidates(outing.CURATED_PLACES, TUE, "any", [], prefs)]
    assert "内蒙古博物院" not in names and not any("公园" in n for n in names)
    ev = [LocalEvent("A", "内蒙古博物院", TUE, None, "", "", "展览", 3),
          LocalEvent("B", "乌兰恰特大剧院", TUE, None, "", "", "演出", 2)]
    assert [e.title for e in outing.rank_events(ev, TUE, day_info(TUE), prefs)] == ["B"]


def test_too_far_preference_drops_long_routes(settings, state):
    prefs = PreferenceStore(state)
    for v in ("x", "y"):
        prefs.record(Feedback("venue", v, "too_far"))
    e = LocalEvent("音乐会", "内蒙古体育馆", TUE, None, "", "", "演出", 3)
    # fixture direct bus takes 2100 s = 35 min → kept; a 50-minute cap would keep it, 30 would not
    assert outing.pick_events(amap(), settings, [e], "any", prefs=prefs, max_minutes=45)
    assert outing.pick_events(amap(), settings, [e], "any", prefs=prefs, max_minutes=30) == []


def test_outing_remembers_what_it_recommended(settings, state):
    prefs = PreferenceStore(state)
    outing.build_outing_message(amap(), settings, state, TUE, event_sources=[], prefs=prefs, remember=True)
    (rec,) = prefs.recent_recommendations()
    assert rec["venue"] and rec["category"]
    other = PreferenceStore(state.__class__(state._path.with_name("other.json")))
    outing.build_outing_message(amap(), settings, state, TUE, event_sources=[], prefs=other, remember=False)
    assert other.recent_recommendations() == []  # previews / dry runs don't count


def test_chat_feedback_is_recorded_only_when_verified(prefs):
    import main
    from features.assistant import AssistantResult
    ok = AssistantResult("feedback", "好的，记下了", False, "llm",
                         {"target": "venue", "value": "内蒙古体育馆", "reason": "too_far"})
    main._apply_feedback(prefs, ok, "这个太远了")
    assert prefs.venue_blocked("内蒙古体育馆")
    bogus = AssistantResult("feedback", "好的", False, "llm", {"target": "venue", "value": "长城", "reason": "visited"})
    main._apply_feedback(prefs, bogus, "长城去过了")
    assert not prefs.venue_blocked("长城")
    like = AssistantResult("feedback", "好的，记下了，以后少给您推荐这类", False, "llm", {"reason": "like"})
    assert main._apply_feedback(prefs, like, "上次的演出挺好的").reply == main.FEEDBACK_LIKE_ACK
    # LLM down: keyword fallback + latest pick, with a confirming reply
    offline = AssistantResult("chat", "您好！给我发…", False, "fallback")
    out = main._apply_feedback(prefs, offline, "美术馆没意思")
    assert out.intent == "feedback" and out.reply == main.FEEDBACK_ACK
    assert prefs.category_penalty("展览") == 0 and prefs.venue_blocked("呼和浩特市美术馆")


def test_chat_context_carries_day_recent_and_prefs_but_prompt_stays_static():
    from features.assistant import SYSTEM_PROMPT, FamilyAssistant, build_user_content

    class Capture:
        calls = []

        def chat_json(self, messages, **kw):
            Capture.calls.append(messages)
            return {"intent": "feedback", "reply": "好的，记下了",
                    "feedback": {"target": "venue", "value": "内蒙古体育馆", "reason": "too_far"}}

    r = FamilyAssistant(Capture()).respond(
        "mom", "妈妈", "这个太远了", datetime(2026, 10, 17, 9),
        extras={"Day": "周末", "Recent": "民歌音乐会@内蒙古体育馆·演出", "Prefs": ""})
    assert r.intent == "feedback" and r.feedback["value"] == "内蒙古体育馆" and r.command is None
    sys_msg, user_msg = Capture.calls[0][0], Capture.calls[0][-1]
    assert sys_msg["content"] == SYSTEM_PROMPT and "feedback" in SYSTEM_PROMPT
    assert "Day=周末" in user_msg["content"] and "Recent=民歌音乐会@内蒙古体育馆·演出" in user_msg["content"]
    assert "Prefs=" not in user_msg["content"]  # empty extras are omitted
    assert "Day=" not in build_user_content("hi", "爸爸", datetime(2026, 10, 13, 9), "小暖")


# ================================================================ 2. weekend & holidays

@pytest.mark.parametrize("d,workday,label", [
    (date(2026, 10, 1), False, "国庆节假期"),
    (date(2026, 10, 10), True, "调休上班"),   # make-up Saturday
    (date(2026, 10, 11), False, "周末"),
    (date(2026, 10, 12), True, "工作日"),
    (date(2027, 3, 6), False, "周末"),        # no official data yet → plain weekday rule
])
def test_day_info_uses_official_calendar(d, workday, label):
    info = day_info(d)
    assert (info.workday, info.label) == (workday, label)


def test_weekend_mode_and_off_days():
    assert day_info(FRI).weekend_mode and day_info(SAT).weekend_mode and not day_info(TUE).weekend_mode
    assert off_days(date(2026, 10, 5), 6) == {date(2026, 10, d) for d in (5, 6, 7, 11)}  # 10/10 is a workday


def test_weekend_ranking_prefers_events_on_days_off():
    weekday_talk = LocalEvent("工作日讲座", "内蒙古图书馆", date(2026, 10, 16), None, "", "", "讲座", 3)
    sat_show = LocalEvent("周六演出", "乌兰恰特大剧院", SAT, None, "19:00", "", "演出", 2)
    ranked = outing.rank_events([weekday_talk, sat_show], FRI, day_info(FRI))
    assert [e.title for e in ranked] == ["周六演出", "工作日讲座"]
    assert [e.title for e in outing.rank_events([weekday_talk, sat_show], TUE, day_info(TUE))] == ["工作日讲座", "周六演出"]


def test_event_headings():
    assert outing.events_heading(day_info(FRI)) == "这个周末去哪儿玩"
    assert outing.events_heading(day_info(SAT)) == "周末去哪儿逛逛"
    assert outing.events_heading(day_info(date(2026, 10, 2))) == "国庆节快乐！假期去哪儿逛逛"
    assert outing.events_heading(day_info(TUE)) == "呼和浩特近期活动"


class FakeEvents:
    def __init__(self, notices):
        self.items = notices

    def notices(self, today, audience):
        return [n for n in self.items if n.audience in ("both", audience)]

    def find(self, today):
        return []


def services(settings, state, now, events=None, wecom=None):
    import main
    sv = main.Services(dataclasses.replace(settings, dry_run=False), amap(), wecom, state, events=events)
    sv.now = lambda: now  # pin "today" for the test
    return sv


def test_commute_job_full_report_on_workdays_incl_tiaoxiu(settings, state):
    import main
    for d in (TUE, date(2026, 10, 10)):  # normal workday and a 调休 Saturday
        msg = main.build_commute_job(services(settings, state, datetime.combine(d, datetime.min.time())), True)
        assert "预计车程" in msg.markdown


def test_commute_job_on_days_off_drops_traffic_and_skips_quiet_days(settings, state):
    import main
    holiday = datetime(2026, 10, 2, 7, 30)
    notices = FakeEvents([LocalNotice("交通", "东二环封闭施工", date(2026, 10, 2), "q"),
                          LocalNotice("民生", "全市开始供暖", date(2026, 10, 2), "q")])
    msg = main.build_commute_job(services(settings, state, holiday, events=notices), True)
    assert "国庆节快乐" in msg.markdown and "供暖" in msg.markdown
    assert "东二环" not in msg.markdown and "预计车程" not in msg.markdown
    only_traffic = FakeEvents([LocalNotice("交通", "东二环封闭施工", date(2026, 10, 2), "q")])
    assert main.build_commute_job(services(settings, state, holiday, events=only_traffic), True) is None


def test_run_job_skips_cleanly_when_nothing_to_send(monkeypatch, settings, state):
    import main
    monkeypatch.setattr(main, "get_services", lambda: services(settings, state, datetime(2026, 10, 11, 7, 30),
                                                                  events=FakeEvents([]), wecom=object()))
    assert main.run_job("commute")["skipped"] is True


# ================================================================ 3. graceful degradation

def test_outing_survives_amap_weather_failure(settings, state):
    bad_weather = [FakeResponse({"status": "0", "info": "DAILY_QUERY_OVER_LIMIT", "infocode": "10003"})]
    msg = outing.build_outing_message(amap(weather=bad_weather), settings, state, TUE, event_sources=[])
    assert "今天出去走走" in msg.markdown and "暂时没查到" in msg.markdown and "12路" in msg.markdown


def test_outing_with_map_service_down_still_recommends_a_classic_place(settings, state):
    msg = outing.build_outing_message(amap(weather=[down], poi=down, transit=down), settings, state, TUE,
                                      event_sources=[])
    assert "推荐：" in msg.markdown and "公交路线暂时没查到" in msg.markdown
    (card,) = msg.articles
    assert card.url.startswith("https://uri.amap.com/search?keyword=") and "[📍 打开高德地图导航](" in msg.markdown


def test_outing_with_feed_down_and_no_events_uses_curated_plus_official_warnings(settings, state):
    class DeadFeedFinder:
        def find(self, today):
            return []  # EventFinder returns [] when we-mp-rss is down (tested in test_events)

        def notices(self, today, audience):
            return [LocalNotice("天气", "寒潮蓝色预警（10月13日7时发布）：最低气温将下降8℃以上", today, "q")]

    msg = outing.build_outing_message(amap(), settings, state, TUE, event_sources=[], events=DeadFeedFinder())
    assert "今天出去走走" in msg.markdown and "寒潮蓝色预警" in msg.markdown


def test_commute_survives_route_failure(settings):
    from features.commute import build_commute_message
    msg = build_commute_message(amap(driving=down), settings, today_date=TUE)
    assert "实时路况暂时没查到" in msg.markdown and "[📍 打开高德地图导航](" in msg.markdown


def test_run_job_sends_warm_fallback_when_builder_crashes(monkeypatch, settings, state):
    import main

    class Rec:
        def __init__(self):
            self.messages, self.texts = [], []

        def send_message(self, users, message, fmt):
            self.messages.append((users, message))

        def send_text(self, users, text):
            self.texts.append((users, text))

    rec = Rec()
    s = dataclasses.replace(settings, dry_run=False, admin_userid="me",
                            mother=dataclasses.replace(settings.mother, wecom_userid="mom"))
    sv = main.Services(s, amap(), rec, state, events=FakeEvents([LocalNotice("天气", "大风蓝色预警", TUE, "q")]))
    monkeypatch.setattr(main, "get_services", lambda: sv)

    def boom(sv_, record):
        raise RuntimeError("unexpected bug")

    monkeypatch.setitem(main.JOBS, "outing", dataclasses.replace(main.JOBS["outing"], build=boom))
    result = main.run_job("outing")
    assert result["sent"] is True
    ((users, msg),) = rec.messages
    assert users == ["mom"] and "妈妈早上好" in msg.markdown and "大风蓝色预警" in msg.markdown
    assert any("已发送备用问候" in t for _, t in rec.texts)  # you still get told
