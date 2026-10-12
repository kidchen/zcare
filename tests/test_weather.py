from datetime import date, datetime

import pytest
import requests

from bot import Command, parse_command
from clients.amap_client import AmapClient
from features.events import LocalNotice
from features.weather import build_weather_message, clothing_tips
from tests import fixtures as fx
from tests.conftest import FakeResponse, FakeSession

TODAY = date(2026, 10, 12)
FORECAST4 = {"status": "1", "forecasts": [{"casts": [
    {"date": "2026-10-12", "dayweather": "多云", "nightweather": "晴", "daytemp": "21", "nighttemp": "2", "daypower": "1-3"},
    {"date": "2026-10-13", "dayweather": "晴", "nightweather": "晴", "daytemp": "16", "nighttemp": "-1", "daypower": "1-3"},
    {"date": "2026-10-14", "dayweather": "小雨", "nightweather": "小雨", "daytemp": "12", "nighttemp": "3", "daypower": "4"},
    {"date": "2026-10-15", "dayweather": "晴", "nightweather": "多云", "daytemp": "19", "nighttemp": "6", "daypower": "1-3"},
]}]}


def amap(weather=None):
    return AmapClient("k", session=FakeSession({"/weather": weather or [FakeResponse(fx.LIVE), FakeResponse(FORECAST4)]}))


class Notices:
    def notices(self, today, audience):
        return [LocalNotice("天气", "寒潮蓝色预警（10月12日9时发布）：最低气温将下降8℃以上", today, "q"),
                LocalNotice("交通", "东二环封闭施工", today, "q"), LocalNotice("民生", "全市开始供暖", today, "q")]


@pytest.mark.parametrize("text", ["这周天气咋样", "明天冷不冷", "会不会下雨", "这几天温度怎么样"])
def test_weather_questions_are_their_own_command(text):
    assert parse_command(text) is Command.WEATHER


def test_traffic_words_still_mean_traffic():
    assert parse_command("今天路况和天气") is Command.COMMUTE


def test_weather_message_has_forecast_tips_and_only_weather_warnings(settings):
    msg = build_weather_message(amap(), settings, TODAY, name="妈妈", events=Notices())
    md = msg.markdown
    assert "这几天的天气" in md and "现在：晴 15°C" in md
    assert "今天（周一）：多云转晴，2~21°C" in md and "明天（周二）" in md and "周四（10月15日）" in md
    assert "寒潮蓝色预警" in md and "官方天气预警" in md
    assert "东二环" not in md and "供暖" not in md and "路况" not in md and "车程" not in md
    assert "-1°C" in md and "带把伞" in md  # clothing tips from the forecast
    assert not msg.articles  # plain message (no AMap card)


def test_weather_message_survives_amap_down(settings):
    def down(url, kw):
        raise requests.ConnectionError("down")
    msg = build_weather_message(amap(weather=[down]), settings, TODAY, name="爸爸", events=Notices())
    assert "天气暂时没查到" in msg.markdown and "寒潮蓝色预警" in msg.markdown


def test_clothing_tips_default_when_mild():
    assert clothing_tips(None, []) == ["天气还不错，适合出门走走，注意多喝水"]


def test_llm_traffic_for_weather_question_is_corrected():
    from features.assistant import FamilyAssistant

    class OneShot:
        def chat_json(self, messages, **kw):
            return {"intent": "traffic", "reply": "好的，我这就帮您查查～"}

    r = FamilyAssistant(OneShot()).respond("mom", "妈妈", "这周天气咋样", datetime(2026, 10, 12, 9))
    assert r.intent == "weather" and r.command is Command.WEATHER


def test_on_demand_weather_uses_weather_builder(monkeypatch, settings, state):
    import main
    sv = main.Services(settings, amap(), None, state, events=Notices())
    msg = main.build_on_demand_message(sv, Command.WEATHER, "妈妈")
    assert "这几天的天气" in msg.markdown and "车程" not in msg.markdown


def test_detail_page_strips_wecom_font_tags_with_quoted_attributes():
    from features.detail import markdown_to_html
    out = markdown_to_html('> 今日路况：<font color="info">整体畅通</font>')
    assert "整体畅通" in out and "font" not in out and "&lt;" not in out and "&quot;" not in out
