from datetime import date

from clients.amap_client import AmapClient
from features import area, outing, reach
from tests import fixtures as fx
from tests.conftest import FakeResponse, FakeSession
from tests.test_parks import around

TUESDAY = date(2026, 10, 13)
NO_TRANSIT = {"status": "1", "route": {"transits": []}}


def test_verified_place_must_be_in_the_message():
    assert area.verified_place("大黑河", "大黑河那边有什么逛的") == "大黑河"
    assert area.verified_place("大黑河那边", "大黑河那边有什么逛的") == "大黑河"
    assert area.verified_place("大召附近", "大召附近好玩吗") == "大召"
    assert area.verified_place("大召寺", "大召附近好玩吗") == ""        # not literally written → ignored
    assert area.verified_place("", "随便") == "" and area.verified_place(None, "x") == ""


NAMED = {"status": "1", "pois": [
    {"name": n, "address": "", "location": f"111.8{i},40.7{i}", "type": t} for i, (n, t) in enumerate([
        ("大黑河郊野公园", "风景名胜;公园广场;公园"), ("大黑河郊野公园-梵草映月", "风景名胜;风景名胜;风景名胜"),
        ("格桑花海(大黑河郊野公园)", "风景名胜;风景名胜;风景名胜"), ("大黑河军事文化乐园", "风景名胜;公园广场;公园")])]}


def test_named_places_first_and_sub_attractions_grouped(settings):
    client = AmapClient("k", session=FakeSession({
        "/place/text": [FakeResponse(NAMED)],
        "/place/around": [FakeResponse(around(("万象城", "购物服务;商场;购物中心", 1800)))],
        "/direction/transit": [FakeResponse(fx.TRANSIT)],
    }))
    anchor, spots = area.find_spots(client, settings, "大黑河")
    assert anchor.name == "大黑河郊野公园"
    assert [p.name for p in spots] == ["大黑河郊野公园", "大黑河军事文化乐园", "万象城"]


def amap_area(transit=fx.TRANSIT, around_resp=None, text_hits=True):
    spots = around_resp or around(("大黑河郊野公园", "风景名胜;公园广场;公园", 300), ("XX小区花园", "风景名胜;公园广场;公园", 100),
                                  ("大黑河郊野公园东门", "风景名胜;公园广场;公园", 350), ("万象城", "购物服务;商场;购物中心", 1800))
    return AmapClient("k", session=FakeSession({
        "/place/text": [FakeResponse(NAMED if text_hits else {"status": "1", "pois": []})],
        "/place/around": [FakeResponse(spots)],
        "/direction/transit": [FakeResponse(transit)],
        "/direction/driving": [FakeResponse(fx.DRIVING)],
    }))


def test_area_message_lists_spots_around_the_named_place(settings):
    msg = area.build_area_message(amap_area(), settings, "大黑河", settings.mother_home, "妈妈")
    assert "**妈妈你好 🌷 大黑河那边可以逛逛这些**" in msg.markdown
    assert "1. 大黑河郊野公园" in msg.markdown and "2. 大黑河军事文化乐园" in msg.markdown and "3. 万象城" in msg.markdown
    assert "小区" not in msg.markdown and "东门" not in msg.markdown and "梵草映月" not in msg.markdown
    assert "公交怎么走" in msg.markdown and len(msg.item_images) == 3


def test_hard_to_reach_spots_stay_with_route_note_and_driving_time(settings):
    msg = area.build_area_message(amap_area(transit=NO_TRANSIT), settings, "大黑河", settings.mother_home, "妈妈")
    assert "公交不太方便" in msg.markdown and "开车约 22 分钟" in msg.markdown and "mode=car" in msg.markdown


def test_unknown_place_asks_back(settings):
    msg = area.build_area_message(amap_area(text_hits=False), settings, "火星", settings.mother_home, "妈妈")
    assert "没找到「火星」" in msg.markdown and "具体一点" in msg.markdown


def test_reach_lines():
    from clients.amap_client import parse_transit
    plans = [parse_transit(t) for t in fx.TRANSIT["route"]["transits"]]
    r = reach.Reach(best=max(plans, key=lambda p: p.transfers), drive_minutes=25)
    line = reach.hard_route_line(r)
    assert line.startswith("公交不太方便：坐 ") and "→" in line and "（换乘 " in line and "(" not in line, line
    assert reach.drive_line(r).startswith("开车约 25 分钟")
    assert reach.hard_route_line(reach.Reach()) == "公交不太方便：附近没有合适的公交"


def test_daily_outing_suggests_a_hard_to_reach_place_instead_of_nothing(settings, state):
    client = AmapClient("k", session=FakeSession({
        "/weather": [FakeResponse(fx.LIVE), FakeResponse(fx.FORECAST)],
        "/place/text": [fx.poi_echo()],
        "/place/around": [FakeResponse(around())],
        "/direction/transit": [FakeResponse(NO_TRANSIT)],
        "/direction/driving": [FakeResponse(fx.DRIVING)],
    }))
    msg = outing.build_outing_message(client, settings, state, TUESDAY, event_sources=[])
    assert "推荐：" in msg.markdown and "公交不太方便" in msg.markdown and "开车约" in msg.markdown
    assert msg.headline.endswith("公交不太方便")


def test_outing_place_from_chat_reaches_the_area_search(monkeypatch, settings, state):
    from features.assistant import FamilyAssistant

    class LLM:
        def chat_json(self, messages, **kw):
            return {"intent": "outing", "reply": "好的，我帮您看看～", "place": "大黑河"}

    r = FamilyAssistant(LLM()).respond("u", "妈妈", "大黑河那边有什么逛的", __import__("datetime").datetime(2026, 11, 2, 9))
    assert r.intent == "outing" and r.place == "大黑河"
