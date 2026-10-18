from datetime import date

from clients.amap_client import AmapClient
from features import outing
from features import parks as pk
from features.push import render_push_html
from messages import Message
from tests import fixtures as fx
from tests.conftest import FakeResponse, FakeSession

TUESDAY = date(2026, 10, 13)


def around(*pois):
    return {"status": "1", "pois": [
        {"name": n, "address": "", "location": f"111.6{i},40.8{i}", "type": t, "distance": str(d),
         "photos": [{"url": f"http://store.is.autonavi.com/showpic/{i}"}]}
        for i, (n, t, d) in enumerate(pois)]}


AROUND = around(("满都海公园", "风景名胜;公园广场;公园", 650), ("XX小区中心花园", "风景名胜;公园广场;公园", 120),
                ("如意广场", "风景名胜;公园广场;城市广场", 1200), ("青城公园", "风景名胜;公园广场;公园", 1900),
                ("大青山野生动物园", "风景名胜;公园广场;动物园", 9000), ("满都海公园B区", "风景名胜;公园广场;公园", 700),
                ("小草公园沙坑", "风景名胜;公园广场;公园", 800))


def amap(live=fx.LIVE, around_resp=AROUND):
    return AmapClient("k", session=FakeSession({
        "/weather": [FakeResponse(live), FakeResponse(fx.FORECAST)],
        "/place/around": [FakeResponse(around_resp)],
        "/place/text": [fx.poi_echo()],
        "/direction/transit": [FakeResponse(fx.TRANSIT)],
    }))


def test_nearby_parks_filters_private_and_far_places_and_upgrades_photos(settings, state):
    parks = pk.nearby_parks(amap(), settings.mother_home, state)
    assert [p.name for p in parks] == ["满都海公园", "如意广场", "青城公园"]   # no 小区 garden, nothing > 2 km
    assert parks[0].photo.startswith("https://") and parks[0].kind == "公园" and parks[1].kind == "广场"
    assert parks[0].walk_minutes == round(650 * 1.3 / 60)


def test_parks_are_cached_and_one_amap_call_is_enough(settings, state):
    client = amap()
    pk.nearby_parks(client, settings.mother_home, state)
    pk.nearby_parks(client, settings.mother_home, state)
    calls = [c for c in client._session.calls if "/place/around" in c[1]]
    assert len(calls) == 1
    later = lambda: 31 * 86400 + 1e10  # noqa: E731 — cache expired
    pk.nearby_parks(client, settings.mother_home, state, clock=later)
    assert len([c for c in client._session.calls if "/place/around" in c[1]]) == 2


def test_rotation_preferences_and_exclusions(state):
    parks = [pk.Park(f"公园{i}", f"1,{i}", 100 * (i + 1)) for i in range(7)]
    day1, day2 = pk.pick_parks(parks, TUESDAY), pk.pick_parks(parks, date(2026, 10, 14))
    assert len(day1) == 2 and day1 != day2 and all(p in parks[:5] for p in day1 + day2)
    from features.preferences import Feedback, PreferenceStore
    prefs = PreferenceStore(state)
    prefs.record(Feedback("venue", "公园0", "visited"))
    picks = [pk.pick_parks(parks, date(2026, 10, d), prefs, exclude=("公园1",)) for d in range(1, 20)]
    assert not any(p.name in ("公园0", "公园1") for day in picks for p in day)


def test_outing_message_gets_a_park_section_with_cards(settings, state):
    msg = outing.build_outing_message(amap(), settings, state, TUESDAY, event_sources=[])
    assert "**🌳 家附近走走**" in msg.markdown and "走路约" in msg.markdown
    assert "uri.amap.com/navigation" in msg.markdown and "mode=walk" in msg.markdown
    page = render_push_html(msg)
    assert page.count('border-radius:18px') == 2 and "store.is.autonavi.com/showpic/" in page   # 2 park cards


def test_no_parks_on_cold_or_stormy_days(settings, state):
    cold = {"status": "1", "lives": [{**fx.LIVE["lives"][0], "temperature": "-3", "weather": "小雪"}]}
    assert "家附近走走" not in outing.build_outing_message(amap(cold), settings, state, TUESDAY, event_sources=[]).markdown
    assert "家附近走走" not in outing.build_outing_message(amap(fx.LIVE_STORM), settings, state, TUESDAY,
                                                            event_sources=[]).markdown


def test_park_lookup_failure_keeps_the_outing_message(settings, state):
    msg = outing.build_outing_message(amap(around_resp={"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT",
                                                        "infocode": "10021"}),
                                      settings, state, TUESDAY, event_sources=[])
    assert "妈妈早上好" in msg.markdown and "家附近走走" not in msg.markdown


def test_park_numbers_continue_after_the_message_items():
    m = Message("t", "**活动**\n\n**1. 甲**\n> a\n\n**2. 乙**\n> b", item_images=("https://x/1", ""))
    out = pk.add_parks(m, [pk.Park("满都海公园", "1,2", 650, "https://p/3")], settings_home())
    assert "**3. 满都海公园**" in out.markdown and out.item_images == ("https://x/1", "", "https://p/3")


def settings_home():
    from config import Location
    return Location.parse("MOTHER_HOME", "家", "111.70,40.82")


def test_park_section_names_merge_into_the_park():
    assert pk._base_name("丝绸之路公园B区") == "丝绸之路公园" and pk._base_name("青城公园(东门)") == "青城公园"
    assert pk._base_name("如意广场") == "如意广场" and pk._base_name("满都海公园") == "满都海公园"


def test_parks_on_a_cold_morning_that_warms_up(settings, state):
    cold_morning = {"status": "1", "lives": [{**fx.LIVE["lives"][0], "temperature": "0"}]}
    msg = outing.build_outing_message(amap(cold_morning), settings, state, TUESDAY, event_sources=[])
    assert "家附近走走" in msg.markdown and "中午暖和了" in msg.markdown   # forecast high is 18°C


def test_park_weather_tip_rules():
    from clients.amap_client import DailyForecast, LiveWeather
    live = lambda w="晴", t=12, p="≤3": LiveWeather(w, t, "西北", p, 30, "")  # noqa: E731
    day = lambda w="晴", hi=15: DailyForecast("2026-10-13", w, w, hi, 0, "≤3")  # noqa: E731
    assert outing.park_weather_tip(live(), day()).startswith("去公园走一圈")
    assert outing.park_weather_tip(live(t=0), day(hi=14)).startswith("早上还冷")
    assert outing.park_weather_tip(live(t=0), day(hi=5)) is None          # cold all day
    assert outing.park_weather_tip(live("小雨"), day()) is None
    assert outing.park_weather_tip(live(p="5"), day()) is None
    assert outing.park_weather_tip(None, None) is None
