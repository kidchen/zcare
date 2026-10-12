import pytest

from features import tv_recommend as tv
from tests.conftest import FakeResponse, FakeSession
from tests import fixtures as fx


def source(mobile=None):
    session = FakeSession({
        "/j/search_subjects": [FakeResponse(fx.DOUBAN_LIST)],
        "/j/new_search_subjects": [FakeResponse(fx.DOUBAN_GENRE_LIST)],
        "/rexxar/api/v2/tv/": [mobile or FakeResponse(fx.DOUBAN_MOBILE)],
        "/subject/": [FakeResponse(text=fx.DOUBAN_DETAIL)],
    })
    return tv.DoubanTvSource(session=session, delay_s=0)


def test_parse_mobile_detail_skips_inaccessible_and_shows_payment():
    d = tv.parse_mobile_detail(fx.DOUBAN_MOBILE, tv.Drama("1", "x", 9.1, "u"))
    assert d.genres == ("剧情", "悬疑") and d.summary == "完整的 剧情简介。"
    assert d.rating == 9.2 and d.year == "2026"
    assert d.platforms == ("腾讯视频（VIP免费）", "爱奇艺")


def test_enrich_falls_back_to_html_when_mobile_api_fails():
    src = source(mobile=FakeResponse({}, status=403))
    d = src.enrich(tv.Drama("1", "x", 9.1, "u"))
    assert d.platforms == ("腾讯视频", "爱奇艺")  # HTML parser output (no payment info)


def test_parse_detail_html():
    base = tv.Drama("1", "x", 9.1, "u")
    d = tv.parse_detail_html(fx.DOUBAN_DETAIL, base)
    assert d.genres == ("剧情", "悬疑")
    assert d.summary == "完整的 剧情简介。"
    assert d.rating == 9.2 and d.year == "2025"
    assert d.platforms == ("腾讯视频", "爱奇艺")


def test_recommend_filters_low_and_unrated_and_excludes_recent():
    picks = tv.recommend(source(), ("历史",), ("悬疑",), 7.5, 3, exclude_ids={"4"})
    assert [d.subject_id for d in picks] == ["1"]


def test_build_tv_message_records_state(settings, state):
    msg = tv.build_tv_message(settings, state, source=source())
    assert "给您挑了几部好剧" in msg.markdown and "《高分悬疑剧》" in msg.markdown
    assert "打开「奇异果TV」，搜「GFXYJ」" in msg.markdown  # fixture drama is on 爱奇艺
    assert "下架平台" not in msg.markdown
    assert set(state.recent(tv.STATE_KEY)) == {"1", "4"}


def test_anti_bot_redirect_is_an_error_not_empty_data():
    import pytest
    import requests
    from tests.conftest import FakeSession
    session = FakeSession({
        "/rexxar/api/v2/tv/": [FakeResponse({}, status=403)],
        "/subject/": [FakeResponse(text="<html>验证</html>", url="https://sec.douban.com/c?r=x")],
    })
    with pytest.raises(requests.HTTPError, match="anti-bot"):
        tv.DoubanTvSource(session=session, delay_s=0).enrich(tv.Drama("1", "x", 9.1, "u"))


# ---------------------------------------------------------------- no repeats

def big_source(n=40):
    """n distinct dramas, ratings 9.5 down; every detail fetch fails (we only care about IDs)."""
    subjects = [{"id": str(i), "title": f"剧{i}", "rate": f"{9.5 - i * 0.04:.1f}", "url": f"u{i}"}
                for i in range(n)]
    session = FakeSession({
        "/j/search_subjects": [FakeResponse({"subjects": []})],  # no trending → all from classics
        "/j/new_search_subjects": [FakeResponse({"data": subjects})],
        "/rexxar/api/v2/tv/": [FakeResponse({}, status=403)],
        "/subject/": [FakeResponse({}, status=403)],
    })
    return tv.DoubanTvSource(session=session, delay_s=0)


def test_repeated_requests_never_repeat_until_pool_is_exhausted(settings, state):
    import dataclasses
    import random
    s = dataclasses.replace(settings, tv_min_rating=7.5, tv_picks=3)
    src, shown = big_source(40), []
    for seed in range(13):  # 13 × 3 = 39 picks from 40 candidates
        picks = tv.recommend(src, ("历史",), (), 7.5, 3, state.recent(tv.STATE_KEY), rng=random.Random(seed))
        state.remember(tv.STATE_KEY, [d.subject_id for d in picks], keep=tv.RECENT_KEEP)
        shown += [d.subject_id for d in picks]
    assert len(shown) == len(set(shown)) == 39


def test_exhausted_pool_reallows_oldest_half(state):
    import random
    src = big_source(6)
    state.remember(tv.STATE_KEY, [str(i) for i in range(6)], keep=tv.RECENT_KEEP)  # all shown, 0 oldest
    picks = tv.recommend(src, ("历史",), (), 7.5, 3, state.recent(tv.STATE_KEY), rng=random.Random(1))
    assert {d.subject_id for d in picks} == {"0", "1", "2"}


def test_selection_varies_between_runs():
    import random
    src = big_source(40)
    runs = {tuple(sorted(d.subject_id for d in tv.recommend(src, ("历史",), (), 7.5, 3, [], rng=random.Random(s))))
            for s in range(5)}
    assert len(runs) > 1


def test_on_demand_tv_is_recorded_only_when_really_sending(monkeypatch, settings, state):
    import dataclasses
    import main
    from bot import Command
    captured = []
    monkeypatch.setattr(main, "build_tv_message", lambda st, s2, record, name, **kw: captured.append(record))
    for dry in (True, False):
        sv = main.Services(dataclasses.replace(settings, dry_run=dry), object(), None, state)
        main.build_on_demand_message(sv, Command.TV, "爸爸", record=not dry)
    assert captured == [False, True]


# ---------------------------------------------------------------- senior-friendly selection

def test_genre_list_request_targets_genre_mainland_by_score():
    session = FakeSession({"/j/new_search_subjects": [FakeResponse(fx.DOUBAN_GENRE_LIST)]})
    tv.DoubanTvSource(session=session, delay_s=0).list_by_genre("战争", 8.0, start=20)
    params = session.calls[0][2]["params"]
    assert params == {"sort": "S", "range": "8,10", "tags": "电视剧,战争", "start": 20, "countries": "中国大陆"}


THIS_YEAR = 2026


@pytest.mark.parametrize("rating,votes,year,ok", [
    (7.5, 100, "1994", True),      # classic at the threshold
    (7.3, 30_000, "2026", True),   # new and clearly popular (e.g. 我不是大师)
    (7.0, 20_000, "2025", True),   # last year counts as new
    (7.3, 5_000, "2026", False),   # new but not popular enough
    (7.3, 100_000, "2019", False), # popular but not new → needs 7.5
    (6.6, 88_000, "2026", False),  # very popular but weak (e.g. 早春晴朗)
    (0.0, 0, "2026", False),       # unrated, just started airing
])
def test_quality_gate(rating, votes, year, ok):
    d = tv.Drama("1", "x", rating, "u", year=year, votes=votes)
    assert tv.passes_quality(d, 7.5, THIS_YEAR) is ok


@pytest.mark.parametrize("genres,ok", [
    (("剧情", "科幻"), True),         # light sci-fi is fine now
    (("剧情", "爱情"), True),         # popular contemporary romance is fine now
    (("剧情", "悬疑", "犯罪"), True),
    (("古装", "奇幻"), True),
    (("动画",), False),
    (("真人秀",), False),
    (("剧情", "恐怖"), False),
])
def test_only_clearly_unsuitable_genres_are_excluded(genres, ok):
    d = tv.Drama("1", "x", 9.0, "u", genres=genres)
    assert tv.suitable(d, ("动画", "恐怖", "真人秀", "脱口秀", "儿童")) is ok


class StubSource:
    """Trending list + genre lists + per-id details, without HTTP."""

    def __init__(self, trending, classics, details):
        self.trending, self.classics, self.details = trending, classics, details
        self.genre_calls = []

    def list_candidates(self, tag, sort="recommend", limit=100):
        return [tv.Drama(i, self.details[i].title, self.details[i].rating, "u") if i in self.details
                else tv.Drama(i, i, 0.0, "u") for i in self.trending]

    def list_by_genre(self, genre, min_rating=7.5, start=0):
        self.genre_calls.append(genre)
        return [tv.Drama(i, self.details[i].title, self.details[i].rating, "u", genres=(genre,)) for i in self.classics]

    def enrich(self, d):
        return self.details.get(d.subject_id, d)


def D(i, rating, year, genres, votes=50_000, platforms=()):
    return tv.Drama(i, f"剧{i}", rating, "u", year=year, genres=genres, votes=votes, platforms=platforms)


DETAILS = {
    "n1": D("n1", 7.7, "2026", ("剧情", "犯罪")),           # 余红旧事-like
    "n2": D("n2", 7.3, "2026", ("剧情", "悬疑"), 30_000),   # popular, below 7.5
    "n3": D("n3", 7.6, "2026", ("剧情", "犯罪"), 66_000),   # same genre as n1
    "n4": D("n4", 6.6, "2026", ("剧情", "爱情"), 88_000),   # weak
    "old_hot": D("old_hot", 7.2, "2015", ("剧情",), 90_000),  # on trending list but old & <7.5
    "c1": D("c1", 9.8, "2007", ("剧情", "历史")),
    "c2": D("c2", 9.4, "2014", ("爱情", "家庭")),
    "c3": D("c3", 9.5, "2006", ("战争",)),
}


def test_mix_is_two_new_hits_then_one_classic_with_genre_variety():
    import random
    src = StubSource(["n1", "n2", "n3", "n4", "old_hot", "unrated"], ["c1", "c2", "c3"], DETAILS)
    picks = tv.recommend(src, ("历史", "家庭", "战争", "悬疑"), ("历史",), 7.5, 3, [],
                         excluded_genres=("动画",), new_picks=2, this_year=THIS_YEAR, rng=random.Random(3))
    ids = [d.subject_id for d in picks]
    assert len(ids) == 3 and set(ids[:2]) <= {"n1", "n2", "n3"} and ids[2] in {"c1", "c2", "c3"}
    assert {"n1", "n3"} - set(ids[:2]), "two 犯罪 shows chosen although a 悬疑 one was available"
    assert "n4" not in ids and "old_hot" not in ids and "unrated" not in ids
    assert len(src.genre_calls) == 3  # only a few genre lists per run


def test_shortfall_of_new_hits_is_filled_with_classics():
    import random
    src = StubSource([], ["c1", "c2", "c3"], DETAILS)
    picks = tv.recommend(src, ("历史",), (), 7.5, 3, [], new_picks=2, this_year=THIS_YEAR, rng=random.Random(0))
    assert sorted(d.subject_id for d in picks) == ["c1", "c2", "c3"]


def test_new_hits_are_marked_in_message_and_cards():
    msg = tv.format_tv_message([DETAILS["n1"], DETAILS["c1"]], "爸爸", this_year=THIS_YEAR)
    assert "🔥新剧 《剧n1》" in msg.markdown and "🔥新剧 《剧c1》" not in msg.markdown
    assert msg.articles[0].title.startswith("1.🔥新剧 《剧n1》") and "🔥" not in msg.articles[1].title
    assert "热播的新剧" in msg.markdown


def test_iqiyi_dramas_rank_higher_and_get_tv_app_note():
    on = tv.Drama("1", "大明王朝1566", 9.0, "u", genres=("历史",), platforms=("爱奇艺（VIP免费观看）",))
    off = tv.Drama("2", "亮剑", 9.4, "u", genres=("战争",), platforms=("腾讯视频（VIP免费观看）",))
    assert tv.score(on, ("历史", "战争"), "爱奇艺") > tv.score(off, ("历史", "战争"), "爱奇艺")
    assert tv.watch_note(on, "爱奇艺", "奇异果TV") == "在电视上打开「奇异果TV」，搜「DMWC」就能找到（VIP免费观看），大屏看更舒服"
    assert tv.watch_note(off, "爱奇艺", "奇异果TV") == "奇异果TV上暂时没有，可以在腾讯视频（VIP免费观看）看"


def test_unsuitable_enriched_dramas_are_skipped():
    import random
    details = {"a": D("a", 9.5, "2010", ("动画",)), "b": D("b", 9.0, "2010", ("历史",))}
    src = StubSource([], ["a", "b"], details)
    picks = tv.recommend(src, ("历史",), (), 7.5, 3, [], excluded_genres=("动画",), this_year=THIS_YEAR,
                         rng=random.Random(0))
    assert [d.subject_id for d in picks] == ["b"]


def test_search_initials():
    assert tv.search_initials("父母爱情") == "FMAQ"
    assert tv.search_initials("长安十二时辰") == "CASESC"
