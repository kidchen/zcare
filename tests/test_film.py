import dataclasses
import time

from features import film
from tests.conftest import FakeResponse, FakeSession


def item(id_, title, year, rating, credits, kind="电影"):
    return {"layout": "subject", "type_name": kind,
            "target": {"id": id_, "title": title, "year": year, "rating": {"value": rating}, "card_subtitle": credits}}


DOUBAN = {"count": 6, "items": [   # the shape m.douban.com returned for 「周润发」 on 2026-11-08
    item("1297574", "英雄本色", "1986", 8.6, "中国香港 / 剧情 动作 犯罪 / 吴宇森 / 周润发 狄龙"),
    item("1295409", "纵横四海", "1991", 8.8, "中国香港 / 剧情 喜剧 动作 犯罪 / 吴宇森 / 周润发 张国荣"),
    item("26425063", "无双", "2018", 8.0, "中国大陆 中国香港 / 剧情 动作 悬疑 犯罪 / 庄文强 / 周润发 郭富城"),
    item("1301168", "卧虎藏龙", "2000", 8.4, "中国台湾 中国香港 / 剧情 爱情 武侠 / 李安 / 周润发 杨紫琼"),
    item("9", "某烂片", "2019", 4.5, "中国大陆 / 剧情 / 某导演 / 周润发 某人"),             # rating too low
    item("8", "上海滩", "1980", 9.0, "中国香港 / 剧情 / 招振强 / 周润发 赵雅芝", kind="电视剧"),
    item("7", "无关的片", "2020", 9.5, "美国 / 剧情 / 别人 / 别人"),                         # name not in credits
]}


def test_only_real_matches_best_rated_first_and_kind_respected():
    got = film.search("周润发", "movie", session=FakeSession({"search/movie": [FakeResponse(DOUBAN)]}))
    assert [t.name for t in got] == ["纵横四海", "英雄本色", "卧虎藏龙", "无双"]       # no 4.5, no series, no unrelated
    series = film.search("周润发", "tv", session=FakeSession({"search/movie": [FakeResponse(DOUBAN)]}))
    assert [t.name for t in series] == ["上海滩"]


def test_message_has_ratings_credits_and_tv_search_tip():
    msg = film.build_film_message("周润发", "movie", "爸爸", session=FakeSession({"search/movie": [FakeResponse(DOUBAN)]}))
    assert "**爸爸你好 🎬 周润发的电影**" in msg.markdown and "**1. 《纵横四海》** ⭐ 8.8 · 1991 · 剧情、喜剧、动作、犯罪" in msg.markdown
    assert "> 吴宇森 / 周润发 张国荣" in msg.markdown and "搜「ZHSH」看看有没有" in msg.markdown
    assert "m.douban.com/movie/subject/1295409" in msg.markdown and msg.headline == "周润发的电影：《纵横四海》"


def test_nothing_found_asks_back():
    msg = film.build_film_message("张三丰", "movie", "爸爸", session=FakeSession({"search/movie": [FakeResponse({"items": []})]}))
    assert "没找到「张三丰」相关的电影" in msg.markdown


def test_query_must_be_in_their_words_or_previous_question():
    assert film.verified_query("周润发", "给我推荐两部周润发的电影") == "周润发"
    assert film.verified_query("周润发的电影", "给我推荐两部周润发的电影") == "周润发"
    assert film.verified_query("周润发", "电影名字给我") == ""
    assert film.verified_query("周润发", "电影名字给我", previous="给我推荐两部周润发的电影") == "周润发"
    assert film.verified_query("刘德华", "给我推荐两部周润发的电影") == ""          # not what they said


def test_dads_two_messages_get_douban_films_not_weekly_picks(monkeypatch, settings, state):
    """The 2026-11-08 conversation: 「给我推荐两部周润发的电影」 then 「电影名字给我」."""
    import main
    from features.assistant import AssistantResult

    sent = []
    monkeypatch.setattr(main, "build_film_message", lambda q, k, name, app="": sent.append((q, k)) or main.Message("t", "md"))
    monkeypatch.setattr(main, "build_tv_message", lambda *a, **k: sent.append("weekly") or main.Message("t", "md"))
    sv = main.Services(dataclasses.replace(settings, dry_run=False), object(), None, state)
    monkeypatch.setattr(main, "get_services", lambda: sv)
    out = []
    ch = main.Chatter("claw:father", "father", out.append, lambda m: out.append(m.markdown), "clawbot")
    main._last_tv.clear()
    for text, glm_query in (("给我推荐两部周润发的电影", "周润发"), ("电影名字给我", "")):
        r = AssistantResult("tv", "好的～", False, "llm", tv_query=glm_query)
        q, k = main._tv_query(ch, r, text)
        main.run_on_demand(main.Command.TV, ch.key, ch, tv_query=q, tv_kind=k)
    assert sent == [("周润发", "movie"), ("周润发", "movie")]
    main._last_tv[ch.key] = ("给我推荐两部周润发的电影", "周润发", "movie", time.time() - 3600)   # long ago
    q, _ = main._tv_query(ch, AssistantResult("tv", "x", False, "llm"), "电影名字给我")
    assert q == ""                                                   # stale → the weekly picks again


def test_title_question_gets_a_title_heading():
    one = {"items": [item("19965220", "父母爱情", "2014", 9.3, "中国大陆 / 爱情 家庭 / 孔笙 / 郭涛 梅婷", kind="电视剧")]}
    msg = film.build_film_message("父母爱情", "tv", "爸爸", session=FakeSession({"search/movie": [FakeResponse(one)]}))
    assert "**爸爸你好 🎬 《父母爱情》**" in msg.markdown and msg.headline == "《父母爱情》豆瓣 9.3 分"
