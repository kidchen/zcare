from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from clients.wemp_rss import Article
from features import alarms as al

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=TZ).timestamp()


def arts(n, text="正文" * 20, day=date(2026, 11, 2)):
    return [Article(f"标题{i}", text, day, f"https://mp.weixin.qq.com/s/{i}", "号") for i in range(n)]


def test_headline_only_feed_fires_the_no_text_alarm():   # the 2026-11-02 incident
    a = {x.key: x for x in al.feed_alarms([], arts(30, text=""), NOW)}
    assert a["feed:no_text"].firing and "30 篇里 30 篇只有标题" in a["feed:no_text"].desc
    assert not a["feed:down"].firing and not a["feed:stale"].firing
    healthy = {x.key: x.firing for x in al.feed_alarms([], arts(30), NOW)}
    assert healthy == {"feed:down": False, "feed:stale": False, "feed:no_text": False}


def test_feed_down_and_stale():
    a = {x.key: x.firing for x in al.feed_alarms([{"type": "extract", "feed_error": True, "ts": NOW}], arts(3), NOW)}
    assert a["feed:down"]
    stale = {x.key: x.firing for x in al.feed_alarms([], arts(10, day=date(2026, 10, 30)), NOW)}
    assert stale["feed:stale"] and not {x.key: x.firing for x in al.feed_alarms([], arts(10, day=date(2026, 11, 1)), NOW)}["feed:stale"]


def test_job_failed_fallback_and_missed():
    entries = [{"type": "sent", "job": "commute", "ok": True, "ts": NOW - 7200},
               {"type": "sent", "job": "outing", "ok": True, "fallback": True, "ts": NOW - 7000}]
    j = {x.key: x.firing for x in al.job_alarms(entries, ["commute", "outing", "tv"])}
    assert j == {"job:commute": False, "job:outing": True, "job:tv": False}
    sched = {"commute": CronTrigger.from_crontab("0 7 * * *", timezone=TZ),
             "digest": CronTrigger.from_crontab("30 19 * * *", timezone=TZ)}
    started = datetime(2026, 11, 1, 20, 0, tzinfo=TZ).timestamp()   # running since last night
    m = {x.key: x.firing for x in al.missed_alarms([], sched, started, NOW)}
    assert m == {"missed:commute": True, "missed:digest": False}     # 07:00 passed with no run; 19:30 not yet due


def test_delivery_llm_and_host():
    errs = [{"type": "error", "what": "晚间本地新闻·PushPlus", "error": "rejected: code 999", "ts": NOW - 60}]
    d = {x.key: x.firing for x in al.delivery_alarms(errs, NOW)}
    assert d == {"delivery:pushplus": True, "chat:clawbot": False}
    calls = [{"type": "llm", "ok": i == 0, "ts": NOW - 60} for i in range(4)]
    assert al.llm_alarm(calls, NOW).firing and not al.llm_alarm(calls[:2], NOW).firing


def test_engine_notifies_only_on_change_and_recovery(state):
    sent = []
    clock = [NOW]
    eng = al.AlarmEngine(state, lambda t, lines: sent.append((t, lines)), clock=lambda: clock[0])
    fire = al.Alarm("feed:no_text", True, "公众号文章没有正文")
    eng.apply([fire, al.Alarm("host:disk", False, "disk")])
    eng.apply([fire])                                   # still firing → no new message
    clock[0] += 3 * 3600
    eng.apply([al.Alarm("feed:no_text", False, "x")])
    assert [t for t, _ in sent] == ["zcare 告警：公众号文章没有正文", "zcare 已恢复：公众号文章没有正文（持续了 3 小时）"]
    assert eng.firing() == {} and eng.remind() == []


def test_escalation_and_daily_reminder(state):
    sent = []
    eng = al.AlarmEngine(state, lambda t, lines: sent.append((t, lines)), clock=lambda: NOW)
    eng.apply([al.Alarm("llm:failing", True, "GLM 失败", al.WARNING)])
    eng.apply([al.Alarm("llm:failing", True, "GLM 失败", al.CRITICAL)])
    assert sent[0][0].startswith("zcare 提醒") and sent[1][1][0].startswith("⏫")
    assert eng.remind()[0].startswith("🚨 GLM 失败")


def test_a_broken_notifier_never_raises(state):
    eng = al.AlarmEngine(state, lambda t, lines: 1 / 0, clock=lambda: NOW)
    assert eng.apply([al.Alarm("k", True, "d")])


def test_zcare_collects_every_alarm_kind(monkeypatch, settings, state, tmp_path):
    import dataclasses

    import main
    from features.activity import ActivityLog

    class Feed:
        def recent_articles(self):
            return arts(20, text="")

    s = dataclasses.replace(settings, state_file=tmp_path / "state.json", wemp_rss_url="http://we-mp-rss:8001")
    sv = main.Services(s, object(), None, state, events=Feed(), activity=ActivityLog(tmp_path / "a.jsonl"))
    keys = {a.key: a.firing for a in main.collect_alarms(sv)}
    assert keys["feed:no_text"] is True
    assert {"job:commute", "missed:digest", "delivery:pushplus", "llm:failing", "backup:old", "host:disk"} <= set(keys)


def test_turning_a_rule_off_clears_it_silently(state):
    sent = []
    eng = al.AlarmEngine(state, lambda t, lines: sent.append(t), clock=lambda: NOW)
    eng.apply([al.Alarm("feed:no_text", True, "没有正文")])
    eng.clear(["feed:no_text"])
    assert eng.firing() == {} and eng.remind() == [] and len(sent) == 1   # no "recovered" message


def test_feed_text_alarm_can_be_disabled(settings, state, tmp_path):
    import dataclasses

    import main
    from features.activity import ActivityLog

    class Feed:
        def recent_articles(self):
            return arts(20, text="")

    s = dataclasses.replace(settings, state_file=tmp_path / "state.json", wemp_rss_url="http://x", alarm_feed_text=False)
    sv = main.Services(s, object(), None, state, events=Feed(), activity=ActivityLog(tmp_path / "a.jsonl"))
    keys = {a.key for a in main.collect_alarms(sv)}
    assert "feed:no_text" not in keys and "feed:stale" in keys
