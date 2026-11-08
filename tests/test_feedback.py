import dataclasses

import pytest

from features import feedback as fb


@pytest.mark.parametrize("text, raw, want", [   # the live GLM probes of 2026-11-02
    ("今天的路况不准，明明很堵", {"kind": "content", "topic": "traffic", "adjust": {"knob": "digest_items", "direction": "more"}},
     ("content", "traffic", None)),                                        # wrong knob for the topic → dropped
    ("新闻的链接打不开", {"kind": "content", "topic": "other"}, ("bug", "news", None)),
    ("能不能每天告诉我菜价", {"kind": "content", "topic": "other"}, ("wish", "other", None)),
    ("新闻太多了，看不过来", {"kind": "content", "topic": "news", "adjust": {"knob": "digest_items", "direction": "less"}},
     ("content", "news", {"knob": "digest_items", "direction": "less"})),
    ("新闻不错", {"kind": "content", "topic": "news", "adjust": {"knob": "digest_items", "direction": "less"}},
     ("praise", "news", None)),                                            # never said "less" → no adjustment
    ("那个剧真好看", {"kind": "content", "topic": "tv"}, ("praise", "tv", None)),
    ("这个剧不好看", {"kind": "content", "topic": "tv"}, ("content", "tv", None)),
])
def test_feedback_is_verified_against_the_parents_words(text, raw, want):
    assert fb.parse_feedback(raw, text) == want


def test_parse_feedback_validates_everything():
    assert fb.parse_feedback({"kind": "content", "topic": "news", "adjust": {"knob": "digest_items", "direction": "less"}}) \
        == ("content", "news", {"knob": "digest_items", "direction": "less"})
    assert fb.parse_feedback({"kind": "nonsense", "topic": "x", "adjust": {"knob": "rm -rf", "direction": "less"}}) \
        == ("content", "other", None)
    assert fb.parse_feedback({"reason": "too_far"})[0] == "place"     # old-style place feedback
    assert fb.parse_feedback(None) == ("content", "other", None)


@pytest.mark.parametrize("text, cmd", [
    ("反馈", fb.AdminCommand("list")),
    ("同意 3", fb.AdminCommand("approve", 3)),
    ("改好了 #12 现在新闻只发3条", fb.AdminCommand("fixed", 12, "现在新闻只发3条")),
    ("不改 4", fb.AdminCommand("dismiss", 4)),
    ("反馈一下今天的新闻太长了", None), ("同意", None), ("今天天气怎么样", None),
])
def test_admin_commands(text, cmd):
    assert fb.parse_admin_command(text) == cmd


def test_proposals_stay_in_bounds_and_apply_as_overrides(settings, state):
    store = fb.FeedbackStore(state)
    s = dataclasses.replace(settings, digest_max_items=3)
    low = store.add("mother", "新闻太多了", "content", "news", {"knob": "digest_items", "direction": "less"}, s)
    assert low.proposal is None                                     # already at the minimum (3)
    more = store.add("mother", "新闻太少了", "content", "news", {"knob": "digest_items", "direction": "more"}, s)
    assert more.proposal == {"knob": "digest_items", "from": 3, "to": 4}
    done = store.apply(more)
    assert done.status == "applied" and done.note == "晚间新闻条数从 3条 改成 4条"
    assert store.tuned(s).digest_max_items == 4 and store.tuned(s).tv_picks == s.tv_picks


def test_items_remember_what_the_parent_last_received(state):
    now = [1_000_000.0]
    store = fb.FeedbackStore(state, clock=lambda: now[0])
    store.remember_delivery("mother", "晚间本地新闻 · 今晚5条本地新闻")
    assert store.last_delivery("mother").startswith("晚间本地新闻")
    now[0] += 2 * 86400
    assert store.last_delivery("mother") == ""                     # too old to be "这个"


class Push:
    def __init__(self):
        self.sent = []

    def send(self, title, html, to=()):
        self.sent.append((title, html, tuple(to)))


def _sv(settings, state, auto=False):  # auto=False: the approval path (FEEDBACK_AUTO_APPLY=false)
    import main
    s = dataclasses.replace(settings, dry_run=False, feedback_auto_apply=auto, admin_userid="",
                            mother=dataclasses.replace(settings.mother, pushplus_to="momtoken"))
    return main.Services(s, object(), None, state, pushplus=Push(), feedback=fb.FeedbackStore(state))


def _chat(monkeypatch, sv, text, glm, role="mother"):
    import main
    from features.assistant import FamilyAssistant

    class LLM:
        def chat_json(self, messages, **kw):
            return glm

    monkeypatch.setattr(main, "get_services", lambda: sv)
    monkeypatch.setattr(main, "get_assistant", lambda: FamilyAssistant(LLM()))
    monkeypatch.setattr(main, "clawbot_store", lambda: type("S", (), {"all": lambda self: []})())
    out = []
    chatter = main.Chatter(f"claw:{role}", role, out.append, lambda m: out.append(m.markdown), "clawbot")
    main.run_llm_chat(chatter.key, text, chatter)
    return out


def test_content_feedback_is_captured_acknowledged_and_closed_with_a_message(monkeypatch, settings, state):
    import main
    sv = _sv(settings, state)
    sv.feedback.remember_delivery("mother", "晚间本地新闻 · 今晚5条本地新闻")
    out = _chat(monkeypatch, sv, "新闻太多了，看不过来", {"intent": "feedback", "reply": "以后少给您推荐这类",
                "feedback": {"kind": "content", "topic": "news", "adjust": {"knob": "digest_items", "direction": "less"}}})
    assert out == [fb.ACK["content"]]                               # fixed, honest ack (not the place wording)
    (item,) = sv.feedback.open_items()
    assert item.about.startswith("晚间本地新闻") and item.proposal["to"] == item.proposal["from"] - 1
    assert sv.settings.digest_max_items == settings.digest_max_items   # nothing applied without approval

    assert "已应用" in main.handle_admin_feedback(sv, fb.AdminCommand("approve", item.id))
    assert main.tuned(sv).settings.digest_max_items == item.proposal["to"]
    reply = main.handle_admin_feedback(sv, fb.AdminCommand("fixed", item.id))
    assert "已告诉妈妈" in reply
    (title, page, to), = sv.pushplus.sent
    assert to == ("momtoken",) and "新闻太多了" in page and "晚间新闻条数从" in page
    assert sv.feedback.open_items() == [] and "没有待处理" in main.handle_admin_feedback(sv, fb.AdminCommand("list"))


def test_auto_apply_changes_at_once_and_reports_later(monkeypatch, settings, state):
    import main
    sv = _sv(settings, state, auto=True)
    alerts = []
    monkeypatch.setattr(main, "alarm_notify", lambda sv_, title, lines: alerts.append((title, lines)))
    monkeypatch.setattr(main, "get_services", lambda: sv)
    out = _chat(monkeypatch, sv, "推荐的剧太少了", {"intent": "feedback", "reply": "好的",
                "feedback": {"kind": "content", "topic": "tv", "adjust": {"knob": "tv_picks", "direction": "more"}}})
    assert out[0].startswith("好的，已经帮您调好了：好剧推荐部数")
    assert main.tuned(sv).settings.tv_picks == settings.tv_picks + 1 and alerts == []   # nothing pinged right away
    main.run_feedback_digest()
    (title, lines), = alerts
    assert title.startswith("家人反馈日报：新 1 条，自动调整 1 项") and any("好剧推荐部数从" in x for x in lines)
    main.run_feedback_digest()
    assert len(alerts) == 1                                          # nothing new → no second summary
    item = sv.feedback.items()[-1]
    main.handle_admin_feedback(sv, fb.AdminCommand("dismiss", item.id))   # undo
    assert main.tuned(sv).settings.tv_picks == settings.tv_picks


def test_bugs_reach_you_right_away_and_place_feedback_is_unchanged(monkeypatch, settings, state):
    import main
    sv = _sv(settings, state)
    alerts = []
    monkeypatch.setattr(main, "alarm_notify", lambda sv_, title, lines: alerts.append(title))
    out = _chat(monkeypatch, sv, "新闻的链接打不开", {"intent": "feedback", "reply": "x",
                "feedback": {"kind": "bug", "topic": "news"}})
    assert out == [fb.ACK["bug"]] and alerts == []                  # reported in the daily summary instead
    assert any("需要你改" in x for x in sv.feedback.digest_lines())
    _chat(monkeypatch, sv, "这个太远了", {"intent": "feedback", "reply": "好的，记下了，以后少给您推荐这类",
          "feedback": {"kind": "place", "reason": "too_far"}})
    assert [i.kind for i in sv.feedback.items()] == ["bug"]       # place → preferences.py, not the inbox


def test_report_section(state):
    store = fb.FeedbackStore(state)
    store.add("father", "路况不准", "content", "traffic", None, None)
    short, md = fb.report_lines(store)
    assert short.startswith("家人反馈：新 1 条，待处理 1 条") and any("路况不准" in x for x in md)
