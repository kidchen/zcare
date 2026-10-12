from datetime import date

from clients.amap_client import AmapClient
from features import outing
from tests.conftest import FakeResponse, FakeSession
from tests import fixtures as fx

MONDAY, TUESDAY = date(2026, 10, 12), date(2026, 10, 13)


def amap(live=fx.LIVE):
    return AmapClient("k", session=FakeSession({
        "/weather": [FakeResponse(live), FakeResponse(fx.FORECAST)],
        "/place/text": [fx.poi_echo()],
        "/direction/transit": [FakeResponse(fx.TRANSIT)],
    }))


def test_picks_direct_bus_and_formats_gentle_itinerary(settings, state):
    msg = outing.build_outing_message(amap(), settings, state, TUESDAY, event_sources=[])
    assert "妈妈早上好" in msg.markdown
    assert "**12路**" in msg.markdown and "(火车站" not in msg.markdown
    assert "「家门口站」上车 →「博物院站」下车（6 站）" in msg.markdown
    assert "回程在对面站台坐同一路车" in msg.markdown
    assert len(state.recent(outing.STATE_KEY)) == 1


def test_dry_run_does_not_record(settings, state):
    outing.build_outing_message(amap(), settings, state, TUESDAY, event_sources=[], record=False)
    assert state.recent(outing.STATE_KEY) == []


def test_stay_home_on_sandstorm(settings, state):
    msg = outing.build_outing_message(amap(fx.LIVE_STORM), settings, state, TUESDAY, event_sources=[])
    assert "在家歇一歇" in msg.markdown


def test_candidate_ordering_respects_closed_days_indoor_and_recent():
    museum = outing.CURATED_PLACES[0]
    assert museum not in outing.order_candidates(outing.CURATED_PLACES, MONDAY, "any", [])
    indoor = outing.order_candidates(outing.CURATED_PLACES, TUESDAY, "indoor", [])
    assert indoor and all(p.indoor for p in indoor)
    fresh = outing.order_candidates(outing.CURATED_PLACES, TUESDAY, "any", ["青城公园"])
    assert all(p.name != "青城公园" for p in fresh)


def test_events_come_first():
    html = '<ul class="ex"><li><a>草原丝绸之路特展</a></li></ul>'
    events = outing.parse_event_list(html, "ul.ex li a", "内蒙古博物院")
    ordered = outing.order_candidates(events + list(outing.CURATED_PLACES), TUESDAY, "any", [])
    assert ordered[0].source == "event" and "草原丝绸之路特展" in ordered[0].description


def test_rejects_routes_with_too_much_walking():
    from clients.amap_client import parse_transit
    plans = [parse_transit(t) for t in fx.TRANSIT["route"]["transits"]]
    assert outing.pick_gentlest_plan(plans, max_transfers=1, max_walk_m=400) is None
    assert outing.pick_gentlest_plan(plans, max_transfers=1, max_walk_m=800).transfers == 0
