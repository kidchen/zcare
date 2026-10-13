import dataclasses
from datetime import date, datetime, timedelta

import pytest

from bot import Command, parse_command
from clients.llm_client import LLMError
from clients.wemp_rss import Article
from features import digest as dg

TODAY = date(2026, 10, 13)


def A(title, content, published=TODAY, url=None, account="新闻天天看"):
    return Article(title, content, published, url or f"https://mp.weixin.qq.com/s/{abs(hash(title)) % 10**8}", account)


ARTICLES = [
    A("呼和浩特10月15日起全面供暖", "全市大部分供热系统将于10月15日全面送暖，居民家中温度可达18℃以上。请注意检查暖气。"),
    A("呼市10月15日全面供暖！", "10月15日全面送暖。", account="青橙融媒"),          # same story, shorter
    A("快卸载！快卸载！快卸载！", "这几款App存在风险，请尽快卸载。公安提醒注意防范。", account="青橙融媒"),
    A("地铁3号线开通时间确定", "地铁3号线将于12月28日开通运营，设站18座。"),
    A("大召广场周末非遗展演", "本周六大召广场举办非遗展演，免费观看。"),
    A("老新闻", "上周的事情。", published=TODAY - timedelta(days=3)),
]


class ScriptedLLM:
    def __init__(self, rank=None, summary=None, fail=False):
        self.rank, self.summary, self.fail, self.calls = rank, summary, fail, []

    def chat_json(self, messages, **kw):
        self.calls.append((messages, kw))
        if self.fail:
            raise LLMError("1305 overloaded")
        return self.rank if messages[0]["content"] == dg.RANK_PROMPT else self.summary


def test_candidates_are_recent_new_and_one_per_story():
    pool = dg.candidates(ARTICLES, TODAY, sent=set())
    titles = [a.title for a in pool]
    assert "老新闻" not in titles  # older than yesterday
    assert sum("供暖" in t for t in titles) == 1 and "呼和浩特10月15日起全面供暖" in titles  # longest text kept
    assert "快卸载！快卸载！快卸载！" in titles  # no topic filtering
    sent = {dg.article_key(ARTICLES[3])}
    assert "地铁3号线开通时间确定" not in [a.title for a in dg.candidates(ARTICLES, TODAY, sent)]


def test_ranked_items_use_verified_summaries_and_fall_back_to_article_words(state):
    pool = dg.candidates(ARTICLES, TODAY, set())
    ids = {a.title: i + 1 for i, a in enumerate(pool)}
    rank = {"picks": [{"id": ids["地铁3号线开通时间确定"], "score": 5}, {"id": ids["呼和浩特10月15日起全面供暖"], "score": 5},
                      {"id": ids["大召广场周末非遗展演"], "score": 3}]}
    # summary ids refer to the summarize-call order (= ranked order)
    summary = {"items": [
        {"id": 1, "title": "地铁3号线年底开通", "summary": "地铁3号线12月28日开通，设站18座。"},
        {"id": 2, "title": "15号全城供暖", "summary": "10月15日全面供暖，屋里能到25℃。"},  # 25 is invented
        {"id": 3, "title": "周末大召广场看非遗", "summary": "本周六大召广场有免费非遗展演。"},
    ]}
    items = dg.DigestBuilder(ScriptedLLM(rank, summary), state).build(ARTICLES, TODAY)
    assert [i.title for i in items[:3]] == ["地铁3号线年底开通", "呼和浩特10月15日起全面供暖", "周末大召广场看非遗"]
    assert items[1].summary.startswith("全市大部分供热系统将于10月15日全面送暖")  # invented number → article's own words
    assert len(items) <= 5 and all(i.url.startswith("https://mp.weixin.qq.com/") for i in items)


def test_glm_down_still_produces_a_digest_from_article_leads(state):
    items = dg.DigestBuilder(ScriptedLLM(fail=True), state).build(ARTICLES, TODAY)
    assert items and all(i.summary for i in items)
    assert items[0].title == dg.candidates(ARTICLES, TODAY, set())[0].title  # newest first


def test_at_most_max_items_and_no_repeats_after_sending(state):
    many = [A(f"第{i}条本地新闻：社区{i}号活动", f"第{i}条内容。说明{i}。") for i in range(12)]
    b = dg.DigestBuilder(None, state, max_items=5)
    first = b.build(many, TODAY)
    assert len(first) == 5
    b.remember(first)
    second = b.build(many, TODAY)
    assert not {i.key for i in first} & {i.key for i in second}


def test_message_cards_open_original_articles():
    items = [dg.DigestItem("地铁3号线年底开通", "地铁3号线12月28日开通。", "https://mp.weixin.qq.com/s/abc", "新闻天天看", "k")]
    msg = dg.format_digest_message(items)
    (card,) = msg.articles
    assert card.url == "https://mp.weixin.qq.com/s/abc" and "来源：新闻天天看" in card.description
    assert "爸爸妈妈晚上好" in msg.markdown and "[阅读原文](https://mp.weixin.qq.com/s/abc)" in msg.markdown


def test_lead_sentence():
    assert dg.lead_sentence("全市10月15日全面送暖。请注意检查。") == "全市10月15日全面送暖。"
    assert dg.lead_sentence("没有句号的一段很长的文字" * 10).endswith("…")


@pytest.mark.parametrize("text", ["有什么新闻", "最近呼和浩特有啥新鲜事", "今天发生了什么"])
def test_news_command(text):
    assert parse_command(text) is Command.NEWS


def test_digest_job_sends_to_both_parents_and_skips_empty_days(monkeypatch, settings, state):
    import main

    class Feed:
        def __init__(self, items):
            self.items = items

        def recent_articles(self):
            return self.items

    s = dataclasses.replace(settings, father=dataclasses.replace(settings.father, wecom_userid="dad"),
                            mother=dataclasses.replace(settings.mother, wecom_userid="mom"))
    assert main.JOBS["digest"].recipients(s) == ["dad", "mom"]
    same = dataclasses.replace(s, mother=dataclasses.replace(s.mother, wecom_userid="dad"))
    assert main.JOBS["digest"].recipients(same) == ["dad"]  # one person in both roles gets it once
    monkeypatch.setattr(main, "get_llm", lambda: None)
    sv = main.Services(s, object(), None, state, events=Feed(ARTICLES))
    sv.now = lambda: datetime(2026, 10, 13, 19, 30)
    msg = main.build_digest(sv, record=True)
    assert msg is not None and len(msg.articles) == 4 and "今天呼和浩特的新鲜事" in msg.markdown
    assert main.build_digest(sv, record=True) is None  # everything already sent → skip, no empty message
    assert main.build_digest(main.Services(s, object(), None, state, events=Feed([])), record=True) is None


def test_digest_scheduled_at_1930_and_can_be_turned_off(settings):
    import main
    on = {j.id: j for j in main.build_scheduler(dataclasses.replace(settings, digest_cron="30 19 * * *")).get_jobs()}
    assert "hour='19'" in repr(on["digest"].trigger) and "minute='30'" in repr(on["digest"].trigger)
    off = {j.id for j in main.build_scheduler(dataclasses.replace(settings, digest_cron="")).get_jobs()}
    assert "digest" not in off
