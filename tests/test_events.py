from datetime import date

import pytest
import requests

from clients.amap_client import AmapClient
from clients.llm_client import LLMError
from clients.wemp_rss import Article as FeedArticle
from features import events as ev
from features import outing
from tests import fixtures as fx
from tests.conftest import FakeResponse, FakeSession

TODAY = date(2026, 10, 13)  # Tuesday
SRC = [
    "又一精彩演出即将亮相呼和浩特\n民歌音乐会《鄂尔多斯奈日嘎》 演出地点：内蒙古体育馆（成吉思汗大街28号） 演出日期：2026年10月17日 19:00 票价：198、158、128、98",
    "呼和浩特雕塑艺术馆 10月18日（周日）电影放映片单 免费观影，需提前预约",
    "内蒙古博物院 特展《草原丝路》展期：2026.09.01—2026.11.30",
]


def raw(**kw):
    base = {"title": "民歌音乐会《鄂尔多斯奈日嘎》", "venue": "内蒙古体育馆（成吉思汗大街28号）", "date": "2026-10-17",
            "end_date": "", "time": "19:00", "price": "98-198元", "category": "演出", "senior_score": 3, "source": 1}
    base.update(kw)
    return base


def test_valid_event_passes_and_venue_address_is_stripped():
    e = ev.validate_event(raw(), SRC, TODAY, 7)
    assert e and e.venue == "内蒙古体育馆" and e.when() == "10月17日（周六） 19:00" and e.is_evening()


@pytest.mark.parametrize("override,why", [
    ({"date": "2026-10-16"}, "date not written in the source"),
    ({"venue": "乌兰恰特大剧院"}, "venue not in the source"),
    ({"source": 9}, "bad citation"),
    ({"date": "2026-10-30", "source": 1}, "beyond the 7-day horizon"),
    ({"date": "2026-10-10"}, "already past"),
    ({"title": ""}, "missing title"),
])
def test_unverifiable_events_are_rejected(override, why):
    assert ev.validate_event(raw(**override), SRC, TODAY, 7) is None, why


def test_month_day_matching_is_not_fooled_by_prefixes():
    assert ev._date_in_text(date(2026, 10, 1), "10月17日") is False
    assert ev._date_in_text(date(2026, 10, 17), "10.17 19:00") is True
    assert ev._date_in_text(date(2026, 10, 18), "10月18日（周日）") is True


def test_running_exhibition_is_kept():
    e = ev.validate_event(raw(title="《草原丝路》特展", venue="内蒙古博物院", date="2026-09-01", end_date="2026-11-30",
                              time="", category="展览", source=3), SRC, TODAY, 7)
    assert e and e.end == date(2026, 11, 30) and e.when() == "展期至11月30日"



class FakeFeed:
    def __init__(self, articles, fail=False):
        self.items, self.fail, self.calls = articles, fail, 0

    def articles(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("we-mp-rss down")
        return self.items


def feed_from(texts, published):
    return FakeFeed([FeedArticle(t.split("\n")[0], t.split("\n", 1)[-1], published, f"u{i}", "新闻天天看")
                     for i, t in enumerate(texts)])


class FakeLLM:
    def __init__(self, extraction):
        self.extraction, self.chats = extraction, 0

    def chat_json(self, messages, **kw):
        self.chats += 1
        self.last_docs = messages[1]["content"]
        assert messages[0]["role"] == "system" and messages[0]["content"] == ev.ARTICLE_EXTRACTION_PROMPT
        assert "Today=" not in messages[1]["content"] and "[1] 标题" in messages[1]["content"]  # date-independent
        return self.extraction


def test_each_article_is_extracted_once_and_dedupes(state):
    llm = FakeLLM({"events": [raw(), raw(), raw(date="2026-10-16"),
                              raw(title="周日公益电影", venue="呼和浩特雕塑艺术馆", date="2026-10-18", time="",
                                  price="免费", category="电影", senior_score=2, source=2)]})
    now = [1000.0]
    feed = feed_from(SRC, date(2026, 10, 9))  # 4 days old → explicit dates required
    finder = ev.EventFinder(llm, state, articles=feed, clock=lambda: now[0])
    found = finder.find(TODAY)
    assert [e.title for e in found] == ["民歌音乐会《鄂尔多斯奈日嘎》", "周日公益电影"]  # dup + unverifiable dropped
    now[0] += 7 * 3600  # hours later, feed re-read: nothing new → no GLM call
    assert [e.title for e in finder.find(TODAY)] == [e.title for e in found] and llm.chats == 1
    feed.items = feed.items + [FeedArticle("新展览预告", "10月15日 内蒙古美术馆 书画展", date(2026, 10, 12), "new")]
    now[0] += 3600
    finder.find(TODAY)
    assert llm.chats == 2 and "新展览预告" in llm.last_docs and "活动预告" not in llm.last_docs  # only the new one


def test_failed_batch_is_retried_next_time(state):
    class Flaky(FakeLLM):
        def chat_json(self, messages, **kw):
            self.chats += 1
            if self.chats == 1:
                raise LLMError("1305 overloaded")
            return {"events": [raw()]}
    llm, now = Flaky({}), [1000.0]
    finder = ev.EventFinder(llm, state, articles=feed_from(SRC, date(2026, 10, 9)), clock=lambda: now[0])
    assert finder.find(TODAY) == []
    now[0] += 3600
    assert [e.title for e in finder.find(TODAY)] == ["民歌音乐会《鄂尔多斯奈日嘎》"] and llm.chats == 2


def test_validation_is_rechecked_against_today_from_cached_extractions(state):
    llm = FakeLLM({"events": [raw()]})  # 10/17 concert
    finder = ev.EventFinder(llm, state, articles=feed_from(SRC, date(2026, 10, 9)))
    assert finder.find(TODAY)  # 10/13: upcoming
    assert finder.find(date(2026, 10, 18)) == [] and llm.chats == 1  # 10/18: over — no new GLM call needed


def test_finder_survives_feed_and_llm_failures(state):
    assert ev.EventFinder(FakeLLM({"events": []}), state, articles=FakeFeed([], fail=True)).find(TODAY) == []

    class Broken(FakeLLM):
        def chat_json(self, *a, **k):
            raise LLMError("1113 arrears")
    assert ev.EventFinder(Broken({}), state, articles=feed_from(SRC, date(2026, 10, 9))).find(TODAY) == []


def test_no_web_search_anywhere():
    import inspect

    import clients.llm_client as llm_mod
    assert not hasattr(llm_mod.LLMClient, "web_search")
    assert "web_search" not in inspect.getsource(ev)


def test_old_and_irrelevant_articles_never_reach_the_llm(state):
    class Capture(FakeLLM):
        def chat_json(self, messages, **kw):
            self.docs = messages[1]["content"]
            return {"events": []}
    llm = Capture({})
    feed = FakeFeed([
        FeedArticle("乌兰恰特大剧院本周演出", "10月17日 二人台专场", date(2026, 10, 12), "a"),
        FeedArticle("一个月前的展览", "展览", date(2026, 9, 1), "b"),
        FeedArticle("市长调研企业", "经济工作会议", date(2026, 10, 12), "c"),
        FeedArticle("无日期的演出", "演出", None, "d"),
    ])
    ev.EventFinder(llm, state, articles=feed).find(TODAY)
    assert "乌兰恰特大剧院本周演出" in llm.docs and "来源：" in llm.docs
    assert "一个月前" not in llm.docs and "市长调研" not in llm.docs and "无日期" not in llm.docs


def amap(poi_type="科教文化服务;博物馆", photo="https://store.is.autonavi.com/showpic/x", known=None):
    def poi(url, kw):
        name = kw["params"]["keywords"]
        if known is not None and name not in known:  # AMap returns some unrelated place
            return FakeResponse({"status": "1", "pois": [{"name": "大召无量寺", "location": "1,1", "type": "风景名胜"}]})
        return FakeResponse({"status": "1", "pois": [{"name": name, "location": "111.7,40.8", "type": poi_type,
                                                       "photos": [{"url": photo}]}]})
    return AmapClient("k", session=FakeSession({
        "/weather": [FakeResponse(fx.LIVE), FakeResponse(fx.FORECAST)],
        "/place/text": [poi], "/direction/transit": [FakeResponse(fx.TRANSIT)],
    }))


class StaticFinder:
    def __init__(self, events):
        self.events = events

    def find(self, today):
        return self.events


def test_outing_lists_verified_events_with_routes_and_cards(settings, state):
    e1 = ev.validate_event(raw(), SRC, TODAY, 7)
    e2 = ev.LocalEvent("某演出", "不存在的剧场", date(2026, 10, 14), None, "", "", "演出", 3)
    msg = outing.build_outing_message(amap(known={"内蒙古体育馆"}), settings, state, TODAY,
                                      event_sources=[], events=StaticFinder([e1, e2]))
    assert "呼和浩特近期活动" in msg.markdown and "不存在的剧场" not in msg.markdown  # unverifiable venue dropped
    assert "10月17日（周六） 19:00" in msg.markdown and "坐 12路，不用换乘" in msg.markdown
    assert "回家建议打车" in msg.markdown  # evening event tip
    (card,) = msg.articles
    assert card.title.startswith("10月17日") and card.picurl.startswith("https://store.is.autonavi.com")
    assert card.url.startswith("https://uri.amap.com/navigation") and "mode=bus" in card.url


def test_outdoor_events_skipped_in_bad_weather():
    e = ev.LocalEvent("广场民俗", "大召广场", date(2026, 10, 13), None, "", "免费", "民俗", 3)
    square = amap(poi_type="风景名胜;公园广场;城市广场")
    assert outing.pick_events(square, _settings(), [e], advice="indoor") == []
    assert len(outing.pick_events(amap(poi_type="风景名胜;公园广场;城市广场"), _settings(), [e], advice="any")) == 1


def _settings():
    from config import load_settings
    return load_settings()


def test_curated_places_are_noncommercial_and_include_requested_venues():
    names = {p.name for p in outing.CURATED_PLACES}
    assert {"乌兰夫公园", "大召广场", "内蒙古博物院"} <= names


@pytest.mark.parametrize("value,expected", [
    ("2026-10-17", date(2026, 10, 17)), ("2026年10月17日", date(2026, 10, 17)), ("2026.10.17", date(2026, 10, 17)),
    ("10月17日", date(2026, 10, 17)), ("1月5日", date(2027, 1, 5)), ("13月40日", None), ("", None),
])
def test_parse_date_formats(value, expected):
    assert ev._parse_date(value, TODAY) == expected


def test_chinese_dates_from_llm_are_accepted():
    assert ev.validate_event(raw(date="2026年10月17日"), SRC, TODAY, 7) is not None


def test_price_must_be_backed_by_source():
    assert ev.validate_event(raw(price="98-198元"), SRC, TODAY, 7).price == "98-198元"
    assert ev.validate_event(raw(price="198-298元"), SRC, TODAY, 7).price == ""  # 298 not in source
    assert ev.validate_event(raw(price="免费"), SRC, TODAY, 7).price == ""        # source doesn't say free


# ---------------------------------------------------------------- real-time layer

RT_TODAY = date(2026, 10, 11)
RT_HITS = [  # shaped like real same-day results seen in live probing
    {"title": "明天出门，穿衣请调整一下！", "publish_date": "2026-10-11",
     "content": "呼和浩特即将大幅降温！ 明天出门 记得穿厚一些 据呼和浩特市气象台消息 12日白天至13日风力增大"},
    {"title": "已确认！10月24日零时起，呼和浩特这些路段正式启用电子警察！", "publish_date": "2026-10-11",
     "content": "为保障道路交通有序、安全、畅通，10月24日零时起启用"},
    {"title": "呼和浩特一地提前供暖！", "publish_date": "2026-10-11",
     "content": "10月10日 武川县百川热力有限公司 提前点火启炉 预计10月11日至12日 居民家中便可陆续感受到热度"},
    {"title": "大召广场今天有民俗展演", "publish_date": "2026-10-11",
     "content": "今天上午10点，大召广场举办非遗民俗展演，免费观看"},
    {"title": "旧闻：去年的降温", "publish_date": "2025-10-11", "content": "今天呼和浩特降温 明天大风"},
    {"title": "无日期的帖子", "publish_date": "", "content": "今天大召广场有活动"},
]


RT_FEED = FakeFeed([FeedArticle(h["title"], h["content"], ev._parse_date(h["publish_date"]), f"rt{i}", "新闻天天看")
                    for i, h in enumerate(RT_HITS)])


class RealtimeLLM:
    """Items may cite ``source_title`` instead of a number; it is resolved against the prompt it
    was actually shown (articles are pre-filtered, so positions differ from RT_HITS)."""

    def __init__(self, extraction):
        self.extraction, self.calls = extraction, []

    def chat_json(self, messages, **kw):
        import copy
        import re as _re
        self.calls.append(messages)
        if messages[0]["content"] != ev.ARTICLE_EXTRACTION_PROMPT:
            return {"events": []}
        shown = dict((t, int(i)) for i, t in _re.findall(r"\[(\d+)\] 标题：(.+)", messages[1]["content"]))
        out = copy.deepcopy(self.extraction)
        for item in out.get("events", []) + out.get("notices", []):
            if "source_title" in item:
                item["source"] = shown.get(item.pop("source_title"), 99)
        return out


def n(**kw):
    base = {"category": "天气", "title": "明天降温，出门多穿点", "date": "2026-10-12",
            "quote": "明天出门 记得穿厚一些", "source": 1}
    base.update(kw)
    return base


def finder_with(extraction, state, warnings=None):
    return ev.EventFinder(RealtimeLLM(extraction), state, articles=RT_FEED, warnings=warnings)


def test_realtime_layer_only_sends_recent_articles(state):
    llm = RealtimeLLM({"events": [], "notices": []})
    ev.EventFinder(llm, state, articles=RT_FEED).notices(RT_TODAY, "father")
    docs = next(c for c in llm.calls if c[0]["content"] == ev.ARTICLE_EXTRACTION_PROMPT)[1]["content"]
    assert "旧闻" not in docs and "无日期的帖子" not in docs and "发布日期：2026-10-11" in docs


@pytest.mark.parametrize("override,ok,why", [
    ({}, True, "明天 in a post published today → tomorrow"),
    ({"quote": "明天一定要穿羽绒服"}, False, "quote not verbatim"),
    ({"date": "2026-10-10"}, False, "in the past"),
    ({"date": "2026-10-20"}, False, "beyond 3-day horizon"),
    ({"date": "2026-10-13", "quote": "12日白天至13日风力增大", "title": "13日风大，出门注意"}, True, "day-only 13日 in text"),
    ({"quote": "明天出门，记得穿厚一些"}, True, "punctuation differs from the source only"),
    ({"date": "2026-10-14", "quote": "12日白天至13日风力增大"}, False, "14日 not in the text"),
    ({"category": "人事"}, False, "not a care category"),
    ({"title": "降温提醒"}, False, "too vague"),
    ({"title": "天气变化请注意身体"}, False, "weather without specifics"),
    ({"title": "降温到【原文温度】，出门穿厚点"}, False, "placeholder copied from the prompt"),
    ({"source": 5}, False, "source is a 2025 post"),
])
def test_notice_verification(state, override, ok, why):
    sources = [f"{h['title']}\n{h['content']}" for h in RT_HITS]
    published = [ev._parse_date(h["publish_date"]) for h in RT_HITS]
    assert (ev.validate_notice(n(**override), sources, published, RT_TODAY) is not None) is ok, why


class FakeWarnings:
    def __init__(self, warnings):
        self.items = warnings

    def warnings(self):
        return self.items


def W(signal, issued, detail="48小时内全市最低气温将要下降8℃以上。", lifted=False):
    from datetime import datetime as dt

    from clients.nmc_client import WeatherWarning
    return WeatherWarning(f"内蒙古自治区呼和浩特市气象台{'解除' if lifted else '发布'}{signal}", signal,
                          dt.fromisoformat(issued), detail, "u", lifted)


def test_notices_are_routed_by_audience_and_cached(state):
    extraction = {"events": [], "notices": [
        n(),  # LLM weather notice → ignored: weather comes only from NMC
        n(category="交通", title="24日起这些路段启用电子警察", date="2026-10-24", quote="10月24日零时起",
          source_title=RT_HITS[1]["title"]),
        n(category="交通", title="今天下午东二环临时封闭", date="2026-10-11", quote="不存在的原文",
          source_title=RT_HITS[1]["title"]),
        n(category="民生", title="武川今明两天开始供暖", date="2026-10-12", quote="提前点火启炉",
          source_title=RT_HITS[2]["title"]),
    ]}
    finder = finder_with(extraction, state, warnings=FakeWarnings([W("寒潮蓝色预警信号", "2026-10-11 07:30")]))
    dad = finder.notices(RT_TODAY, "father")
    mom = finder.notices(RT_TODAY, "mother")
    assert [x.category for x in dad] == ["天气", "民生"]  # 10/24 is beyond 3 days; fake quote dropped
    assert [x.category for x in mom] == ["天气", "民生"]
    assert dad[0].line(RT_TODAY) == "寒潮蓝色预警（10月11日7时发布）：48小时内全市最低气温将要下降8℃以上。"
    assert ev.LocalNotice("民生", "武川开始供暖", date(2026, 10, 12), "q").line(RT_TODAY) == "明天：武川开始供暖"
    assert finder.cached_notices(RT_TODAY, "mother") == mom
    assert finder.cached_notices(date(2026, 10, 12), "mother") == []  # yesterday's cache isn't reused


def test_realtime_events_accept_today_wording_and_are_marked_fresh(state):
    extraction = {"events": [{"title": "非遗民俗展演", "venue": "大召广场", "date": "2026-10-11", "end_date": "",
                              "time": "10:00", "price": "免费", "category": "民俗", "senior_score": 3,
                              "source_title": RT_HITS[3]["title"]}],
                  "notices": []}
    found = finder_with(extraction, state).find(RT_TODAY)
    assert [(e.title, e.fresh, e.price) for e in found] == [("非遗民俗展演", True, "免费")]


def test_traffic_notice_only_for_dad_and_no_relative_wording_in_old_posts(state):
    sources = ["旧闻\n今天呼和浩特降温"]
    assert ev.validate_notice(n(quote="今天呼和浩特降温", date="2026-10-11", source=1), sources,
                              [date(2026, 10, 9)], RT_TODAY) is None  # "今天" in a 2-day-old post ≠ today
    t = ev.LocalNotice("交通", "x", RT_TODAY, "q")
    assert t.audience == "father" and ev.LocalNotice("天气", "x", RT_TODAY, "q").audience == "both"


# ---------------------------------------------------------------- notices in messages / chat

def test_add_notices_inserts_section_before_link_and_into_first_card():
    from messages import Article, Message, add_notices
    msg = Message("t", "**爸爸早上好**\n> 路况畅通\n\n路上慢点开\n[打开高德导航](https://x)", url="https://x",
                  articles=(Article("路况", "畅通", "https://x"),))
    out = add_notices(msg, ["明天降温，出门多穿点", "24日起启用电子警察"])
    assert out.markdown.index("📢 今日提醒") < out.markdown.index("[打开高德导航]")
    assert "- 明天降温，出门多穿点" in out.text and "- 24日起启用电子警察" in out.text
    assert out.articles[0].description.startswith("📢 明天降温，出门多穿点\n")
    assert add_notices(msg, []) is msg


def test_commute_includes_driver_notices_and_survives_failures(settings):
    from clients.amap_client import AmapClient
    from features.commute import build_commute_message

    def amap():
        return AmapClient("k", session=FakeSession({
            "/direction/driving": [FakeResponse(fx.DRIVING)],
            "/weather": [FakeResponse(fx.LIVE), FakeResponse(fx.FORECAST)]}))

    class Notices:
        def notices(self, today, audience):
            assert audience == "father"
            return [ev.LocalNotice("交通", "东二环今天临时封闭", today, "q")]

    class Broken:
        def notices(self, today, audience):
            raise RuntimeError("search down")

    ok = build_commute_message(amap(), settings, events=Notices(), today_date=RT_TODAY)
    assert "📢 今日提醒" in ok.markdown and "东二环今天临时封闭" in ok.markdown
    plain = build_commute_message(amap(), settings, events=Broken(), today_date=RT_TODAY)
    assert "📢" not in plain.markdown and "预计车程" in plain.markdown


def test_stay_home_message_still_gets_notices(settings, state):
    from clients.amap_client import AmapClient

    class Notices:
        def notices(self, today, audience):
            return [ev.LocalNotice("民生", "武川开始供暖", today, "q")]

        def find(self, today):
            return []

    storm = AmapClient("k", session=FakeSession({
        "/weather": [FakeResponse(fx.LIVE_STORM), FakeResponse(fx.FORECAST)]}))
    msg = outing.build_outing_message(storm, settings, state, RT_TODAY, event_sources=[], events=Notices())
    assert "在家歇一歇" in msg.markdown and "武川开始供暖" in msg.markdown


def test_chat_context_carries_notes_but_system_prompt_stays_static():
    from datetime import datetime

    from features.assistant import SYSTEM_PROMPT, FamilyAssistant

    class Capture:
        def __init__(self):
            self.calls = []

        def chat_json(self, messages, **kw):
            self.calls.append(messages)
            return {"intent": "chat", "reply": "妈妈，明天降温，记得加衣服～"}

    cap = Capture()
    a = FamilyAssistant(cap)
    a.respond("mom", "妈妈", "今天冷吗", datetime(2026, 10, 11, 9), notes=["明天降温，出门多穿点"])
    a.respond("dad", "爸爸", "你好", datetime(2026, 10, 11, 9))
    assert cap.calls[0][0]["content"] == cap.calls[1][0]["content"] == SYSTEM_PROMPT
    assert "Notes=明天降温，出门多穿点]" in cap.calls[0][-1]["content"]
    assert "Notes=" not in cap.calls[1][-1]["content"]


def test_stitched_quote_from_same_post_is_accepted_but_invented_clause_is_not():
    text = "武川县第四热源厂点火启炉。武川县百川热力有限公司现已启动。今冬集中供暖工作全面启动"
    assert ev.quote_in_source("武川县第四热源厂点火启炉。今冬集中供暖工作全面启动", text)
    assert not ev.quote_in_source("武川县第四热源厂点火启炉。全市明天全部通暖", text)


def test_ongoing_closure_is_kept_with_duration_evidence():
    src = ["为期31天！G6京藏高速呼和浩特收费站将双向封闭施工!\n10月10日8:00起 收费站所有进出通道全部关闭"]
    raw = {"category": "交通", "title": "G6京藏高速呼和浩特收费站双向封闭", "date": "2026-10-10",
           "end_date": "2026-11-10", "quote": "收费站所有进出通道全部关闭", "source": 1}
    notice = ev.validate_notice(raw, src, [date(2026, 10, 9)], date(2026, 10, 12))
    assert notice and notice.end == date(2026, 11, 10)
    assert notice.line(date(2026, 10, 12)) == "G6京藏高速呼和浩特收费站双向封闭（持续到11月10日）"
    assert ev.validate_notice(dict(raw, end_date="2026-12-31"), src, [date(2026, 10, 9)], date(2026, 10, 12)) is None
    assert ev.validate_notice(dict(raw, end_date=""), src, [date(2026, 10, 9)], date(2026, 10, 12)) is None


def test_title_relative_day_and_numbers_are_sanitized():
    src = ["明天出门，穿衣请调整一下！\n呼和浩特即将大幅降温！ 明天出门 记得穿厚一些 跌破0°C"]
    pub = [date(2026, 10, 11)]
    ok = ev.validate_notice({"category": "天气", "title": "明天降温跌破0°C，出门穿厚点", "date": "2026-10-12",
                             "quote": "明天出门 记得穿厚一些", "source": 1}, src, pub, date(2026, 10, 11))
    assert ok and ok.title == "降温跌破0°C，出门穿厚点" and ok.line(date(2026, 10, 11)) == "明天：降温跌破0°C，出门穿厚点"
    invented = {"category": "天气", "title": "最低3°C，有大风，出门穿厚点", "date": "2026-10-12",
                "quote": "明天出门 记得穿厚一些", "source": 1}
    assert ev.validate_notice(invented, src, pub, date(2026, 10, 11)) is None  # 3 not in source


def test_heating_start_is_not_treated_as_ongoing_disruption():
    src = ["呼和浩特供暖，提前发力！\n全市大部分供热系统于10月10日具备完整供热条件，10月15日全面送暖"]
    raw = {"category": "民生", "title": "全市供热系统已具备供热条件", "date": "2026-10-10", "end_date": "2026-10-15",
           "quote": "全市大部分供热系统于10月10日具备完整供热条件", "source": 1}
    assert ev.validate_notice(raw, src, [date(2026, 10, 9)], date(2026, 10, 12)) is None


def test_numbers_must_match_with_their_unit():
    src = "预计明天以晴转多云为主 西南风3级左右 跌破0°C"
    assert ev._numbers_backed("跌破0°C，有3级风", src)
    assert ev._numbers_backed("跌破0度", src)           # °C / ℃ / 度 are the same unit
    assert not ev._numbers_backed("最低3°C", src)        # the 3 in the source is wind force, not °C
    assert not ev._numbers_backed("5级大风", src)


def test_notice_line_does_not_repeat_the_date():
    n15 = ev.LocalNotice("民生", "供暖提前，10月15日全面送暖", date(2026, 10, 15), "q")
    assert n15.line(date(2026, 10, 12)) == "供暖提前，10月15日全面送暖"
    assert ev.LocalNotice("民生", "武川开始供暖", date(2026, 10, 15), "q").line(date(2026, 10, 12)) == "10月15日：武川开始供暖"



# ---------------------------------------------------------------- NMC + we-mp-rss clients

def test_nmc_warnings_lifted_and_stale_are_handled(state):
    warnings = FakeWarnings([
        W("大风蓝色预警信号", "2026-10-11 06:00"),
        W("大风蓝色预警信号", "2026-10-11 15:00", lifted=True),     # cancels the 06:00 one
        W("寒潮蓝色预警信号", "2026-10-08 06:00"),                   # older than 48 h
        W("暴雪黄色预警信号", "2026-10-11 20:00", detail=""),
    ])
    notices = ev.EventFinder(None, state, warnings=warnings).notices(RT_TODAY, "mother")  # works without any LLM
    assert [x.title for x in notices] == ["暴雪黄色预警（10月11日20时发布）"]


def test_nmc_client_parses_list_and_detail():
    from clients.nmc_client import NmcClient
    listing = {"code": 0, "data": {"page": {"list": [
        {"alertid": "1", "issuetime": "2026/10/12 07:09", "title": "内蒙古自治区呼和浩特市气象台发布大风蓝色预警信号",
         "url": "/publish/alarm/1.html"},
        {"alertid": "2", "issuetime": "2026/10/12 07:09", "title": "内蒙古自治区锡林郭勒盟气象台发布大风蓝色预警信号",
         "url": "/publish/alarm/2.html"},
    ]}}}
    detail = ("<html><div>呼和浩特市气象台2026年10月12日07时08分发布大风蓝色预警信号：24小时内全市将受大风影响，"
              "平均风力可达6级以上。请有关单位做好防范。</div></html>")
    session = FakeSession({"/rest/findAlarm": [FakeResponse(listing)], "/publish/alarm/": [FakeResponse(text=detail)]})
    (w,) = NmcClient(session=session).warnings()
    assert w.signal == "大风蓝色预警信号" and w.issued.hour == 7 and not w.lifted
    assert w.detail == "24小时内全市将受大风影响，平均风力可达6级以上。"
    assert session.calls[0][2]["params"]["province"] == "内蒙古自治区"


def test_wemp_feed_parsing_matches_server_json_shape():
    from clients.wemp_rss import parse_feed
    # shape of core/rss.py generate_json() in we-mp-rss
    feed = {"name": "WeRss", "link": "http://we-mp-rss:8001/feed/all.json", "items": [
        {"id": "1", "title": "呼和浩特这个路段封闭", "description": "东二环封闭施工",
         "link": "https://mp.weixin.qq.com/s/abc", "updated": "2026-10-11T23:30:00+08:00",
         "content": "<p>10月14日 <b>东二环</b>封闭</p>", "channel_name": "新闻天天看",
         "feed": {"id": "MP_WXS_1", "name": "新闻天天看"}},
        {"id": "2", "title": "无正文的文章", "description": "只有摘要", "link": "u2",
         "updated": "2026-10-11T08:00:00", "content": "", "channel_name": ""},
        {"id": "3", "title": "", "content": "x"},
    ]}
    a, b = parse_feed(feed)
    assert a.content == "10月14日 东二环 封闭" and a.account == "新闻天天看" and a.url.startswith("https://mp.weixin")
    assert a.published == date(2026, 10, 11)  # Beijing date kept (23:30 +08:00)
    assert b.content == "只有摘要" and b.published == date(2026, 10, 11)  # naive time = Beijing


def test_wemp_client_requests_json_feed():
    from clients.wemp_rss import WeMpRssClient
    feed = {"items": [{"id": "1", "title": "t", "link": "u", "updated": "2026-10-11T08:00:00+08:00", "content": "c"}]}
    s1 = FakeSession({"/feed/": [FakeResponse(feed)]})
    assert len(WeMpRssClient("http://we-mp-rss:8001/", ["MP_WXS_1"], session=s1).articles()) == 1
    assert s1.calls[0][1] == "http://we-mp-rss:8001/feed/MP_WXS_1.json" and s1.calls[0][2]["params"] == {"limit": 100}
    s2 = FakeSession({"/feed/": [FakeResponse(feed)]})
    WeMpRssClient("http://we-mp-rss:8001", [], session=s2).articles()
    assert s2.calls[0][1].endswith("/feed/all.json")
    s3 = FakeSession({"/feed/": [FakeResponse({}, status=502)]})
    with pytest.raises(requests.HTTPError):
        WeMpRssClient("http://x", [], session=s3).articles()

def test_dead_feed_is_tried_once_per_message_not_per_layer(state):
    feed = FakeFeed([], fail=True)
    ev.EventFinder(FakeLLM({"events": []}), state, articles=feed).find(TODAY)  # events + real-time layers
    assert feed.calls == 1


@pytest.mark.parametrize("text,relevant", [
    ("市政府开展安全生产大检查活动", False),      # bare 活动 no longer counts
    ("社区开展主题党日活动", False),
    ("文化馆周末公益活动预告", True),
    ("乌兰恰特大剧院本周二人台演出", True),
    ("东二环封闭施工", True),
])
def test_relevance_prefilter(text, relevant):
    assert bool(ev.RELEVANT.search(text)) is relevant
