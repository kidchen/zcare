import dataclasses
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from features.activity import ActivityLog
from features.report import build_daily_report

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 12, 21, 0, tzinfo=TZ)
T = NOW.timestamp()
NAMES = {"father": "爸爸", "mother": "妈妈", "admin": "你"}


def entries():
    h = 3600
    return [
        {"ts": T - 13.5 * h, "type": "sent", "job": "commute", "to": "father", "ok": True},
        {"ts": T - 12 * h, "type": "sent", "job": "outing", "to": "mother", "ok": True, "fallback": True},
        {"ts": T - 10 * h, "type": "chat", "role": "mother", "intent": "outing", "source": "llm", "text": "今天有什么活动？"},
        {"ts": T - 9 * h, "type": "chat", "role": "mother", "intent": "feedback", "source": "llm", "text": "这个太远了"},
        {"ts": T - 9 * h, "type": "feedback", "role": "mother", "target": "venue", "value": "内蒙古体育馆", "reason": "too_far"},
        {"ts": T - 8 * h, "type": "chat", "role": "father", "intent": "weather", "source": "llm", "text": "明天冷不冷"},
        {"ts": T - 7 * h, "type": "chat", "role": "mother", "intent": "urgent", "source": "llm", "urgent": True, "text": "有点胸闷"},
        {"ts": T - 6 * h, "type": "on_demand", "command": "outing", "role": "mother", "ok": False, "error": "Timeout"},
        {"ts": T - 14 * h, "type": "extract", "feed_articles": 60, "new_relevant": 9, "processed": 9, "failed_batches": 0,
         "cached": 40, "events": 4, "notices": 1},
        {"ts": T - 3 * h, "type": "extract", "feed_articles": 64, "new_relevant": 2, "processed": 0, "failed_batches": 1,
         "cached": 40, "events": 3, "notices": 2},
        {"ts": T - 10 * h, "type": "llm", "purpose": "chat", "ok": True, "prompt": 700, "completion": 40, "latency": 1.2},
        {"ts": T - 9 * h, "type": "llm", "purpose": "chat", "ok": True, "prompt": 720, "completion": 50, "latency": 1.6},
        {"ts": T - 14 * h, "type": "llm", "purpose": "extract", "ok": True, "prompt": 2300, "completion": 500, "latency": 12.0},
        {"ts": T - 3 * h, "type": "llm", "purpose": "extract", "ok": False, "latency": 120.0},
        {"ts": T - 17.5 * h, "type": "cleanup", "deleted": 37},
        {"ts": T - 5 * h, "type": "wemp_notice", "title": "WeRss授权过期", "qr": True},
        {"ts": T - 12 * h, "type": "error", "what": "outing（已发送备用问候）", "error": "RuntimeError('boom')"},
    ]


def test_report_summarizes_every_section():
    short, full = build_daily_report(entries(), NOW, NAMES)
    assert short.startswith("📊 zcare 日报 · 10月12日 21:00")
    assert "定时消息：成功 2，备用问候 1" in short
    assert "家人对话：妈妈 3 次；爸爸 1 次，⚠️紧急 1 次" in short
    assert "新相关 11 篇，已提取 9 篇，失败批次 1；当前有效活动 3、提醒 2" in short
    assert "GLM：对话 2 次，文章提取 2 次（失败 1）" in short and "授权提醒 1 次（二维码已发送）" in short
    assert len(short.encode()) < 1500  # fits a WeCom text with the link
    for part in ("07:30 爸爸早间路况 → 爸爸：✓", "09:00 妈妈出行推荐 → 妈妈：备用问候", "妈妈：3 次", "「这个太远了」→ 反馈",
                 "⚠️ 紧急消息", "「有点胸闷」", "场馆「内蒙古体育馆」— 太远", "按需请求失败", "订阅源里最近的文章：64 篇",
                 "对话：2 次（成功 2，失败 0），输入 1420 / 输出 90 tokens，平均 1.4 秒", "清理旧文章：37 篇", "RuntimeError"):
        assert part in full, part


def test_report_for_a_quiet_day():
    short, full = build_daily_report([], NOW, NAMES)
    assert "家人对话：无" in short and "GLM：无调用" in short and "- 无" in full


def test_activity_log_window_prune_and_torn_lines(tmp_path):
    now = [T]
    log = ActivityLog(tmp_path / "activity.jsonl", clock=lambda: now[0])
    log.record("chat", role="mother", text="你好")
    now[0] += 2 * 86400
    log.record("sent", job="outing")
    with (tmp_path / "activity.jsonl").open("a") as f:
        f.write('{"ts": 1, "type": "trunc')  # crash mid-write
    assert [e["type"] for e in log.since(86400)] == ["sent"]
    now[0] += 7 * 86400
    log.prune()
    lines = (tmp_path / "activity.jsonl").read_text().splitlines()
    assert [json.loads(x)["type"] for x in lines] == ["sent"]  # 9-day-old chat dropped, 7-day-old send kept


def test_daily_report_job_sends_summary_with_link(monkeypatch, tmp_path, settings, state):
    import main
    from features.detail import DetailStore

    class Rec:
        texts = []

        def send_text(self, users, text):
            Rec.texts.append((users, text))

    act = ActivityLog(tmp_path / "a.jsonl", clock=lambda: T)
    for e in entries():
        act.record(e.pop("type"), **{k: v for k, v in e.items() if k != "ts"})
    s = dataclasses.replace(settings, dry_run=False, admin_userid="me")
    sv = main.Services(s, object(), Rec(), state, details=DetailStore(tmp_path / "d", "http://srv"), activity=act)
    sv.now = lambda: NOW
    monkeypatch.setattr(main, "get_services", lambda: sv)
    main.run_daily_report()
    ((users, text),) = Rec.texts
    assert users == ["me"] and "📊 zcare 日报" in text and "完整报告：http://srv/detail/" in text


def test_report_job_is_scheduled(settings):
    import main
    sched = main.build_scheduler(dataclasses.replace(settings, admin_userid="me", daily_report_cron="0 21 * * *"))
    job = {j.id: j for j in sched.get_jobs()}["daily_report"]
    assert "hour='21'" in repr(job.trigger)
    off = main.build_scheduler(dataclasses.replace(settings, admin_userid="me", daily_report_cron=""))
    assert "daily_report" not in {j.id for j in off.get_jobs()}


def test_activity_is_recorded_from_jobs_chat_llm_and_extraction(monkeypatch, tmp_path, settings, state):
    import main
    from features.assistant import FamilyAssistant
    from bot import ChatBot

    act = ActivityLog(tmp_path / "a.jsonl")

    class Rec:
        def send_message(self, *a):
            pass

        def send_text(self, *a):
            pass

    s = dataclasses.replace(settings, dry_run=False, father=dataclasses.replace(settings.father, wecom_userid="dad"),
                            mother=dataclasses.replace(settings.mother, wecom_userid="mom"))
    sv = main.Services(s, object(), Rec(), state, activity=act)
    monkeypatch.setattr(main, "get_services", lambda: sv)
    monkeypatch.setitem(main.JOBS, "tv", dataclasses.replace(
        main.JOBS["tv"], build=lambda sv_, record: main.Message("t", "md")))
    main.run_job("tv")

    class OneShot:
        def chat_json(self, messages, **kw):
            return {"intent": "weather", "reply": "好的～"}
    monkeypatch.setattr(main, "get_assistant", lambda: FamilyAssistant(OneShot()))
    monkeypatch.setattr(main, "get_bot", lambda b=ChatBot(["dad", "mom"], llm_enabled=True): b)
    monkeypatch.setattr(main, "run_on_demand", lambda cmd, user: None)
    main.run_llm_chat("mom", "明天冷不冷")

    from clients.llm_client import LLMClient
    from tests.conftest import FakeResponse, FakeSession
    llm = LLMClient("k", session=FakeSession({"/chat/completions": [FakeResponse({"choices": [
        {"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})]}))
    llm.on_call = lambda **k: act.record("llm", **k)
    llm.chat_json([], purpose="extract")

    kinds = [(e["type"], e.get("job") or e.get("intent") or e.get("purpose")) for e in act.since(3600)]
    assert ("sent", "tv") in kinds and ("chat", "weather") in kinds and ("llm", "extract") in kinds
    chat = next(e for e in act.since(3600) if e["type"] == "chat")
    assert chat["role"] == "mother" and chat["text"] == "明天冷不冷"


def test_extraction_stats_are_reported_on_warm_up(state):
    from clients.wemp_rss import Article
    from features import events as ev
    stats = []

    class LLM:
        def chat_json(self, messages, **kw):
            return {"events": [], "notices": []}

    class Feed:
        def articles(self):
            return [Article("乌兰恰特大剧院演出", "10月17日 二人台", date(2026, 10, 12), "a")]

    f = ev.EventFinder(LLM(), state, articles=Feed())
    f.on_stats = lambda **k: stats.append(k)
    f.refresh(date(2026, 10, 13), report=True)
    f.refresh(date(2026, 10, 13))            # nothing new and not a warm-up → no entry
    f.refresh(date(2026, 10, 13), report=True)
    assert len(stats) == 2 and stats[0]["new_relevant"] == 1 and stats[0]["processed"] == 1
    assert stats[1]["new_relevant"] == 0 and stats[1]["feed_articles"] == 1
