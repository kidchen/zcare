import pytest

from clients.amap_client import AmapClient, AmapError
from tests import fixtures as fx
from tests.conftest import FakeResponse, FakeSession


def client(routes):
    return AmapClient("k", session=FakeSession(routes))


def test_driving_route_aggregates_congestion_and_handles_empty_road():
    route = client({"/direction/driving": [FakeResponse(fx.DRIVING)]}).get_driving_route("a", "b")
    assert route.duration_s == 1320 and route.distance_m == 14300
    assert route.main_roads == ("新华大街", "兴安北路", "成吉思汗大街")
    assert route.congested[0].road == "兴安北路" and route.congested[0].status == "拥堵"
    assert route.congested[0].distance_m == 500
    assert route.worst_severity == 2


def test_transit_parsing_counts_transfers_and_tolerates_list_fields():
    plans = client({"/direction/transit": [FakeResponse(fx.TRANSIT)]}).get_transit_routes("a", "b", "呼和浩特")
    direct, complex_ = plans
    assert direct.transfers == 0 and direct.walking_m == 450
    assert [leg.mode for leg in direct.legs] == ["walk", "bus", "walk"]
    assert direct.legs[1].stop_count == 6
    assert complex_.transfers == 2 and complex_.legs[1].mode == "subway"


def test_error_status_raises():
    with pytest.raises(AmapError, match="INVALID_USER_KEY"):
        client({"/weather": [FakeResponse({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})]}) \
            .get_live_weather("150100")


def test_api_key_is_sent():
    session = FakeSession({"/weather": [FakeResponse(fx.LIVE)]})
    AmapClient("secret-key", session=session).get_live_weather("150100")
    assert session.calls[0][2]["params"]["key"] == "secret-key"


def test_calls_are_paced_and_a_qps_error_is_retried_once():
    import clients.amap_client as ac
    from tests import fixtures as fx
    from tests.conftest import FakeResponse, FakeSession
    now, slept = [100.0], []
    def sleep(s):
        slept.append(round(s, 2)); now[0] += s
    qps = FakeResponse({"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT", "infocode": "10021"})
    session = FakeSession({"/weather": [qps, FakeResponse(fx.LIVE), FakeResponse(fx.LIVE)]})
    ac.AmapClient._last_call = 0.0
    c = ac.AmapClient("k", session=session, min_interval_s=0.34, sleep=sleep, clock=lambda: now[0])
    assert c.get_live_weather("150100").weather == "晴"      # retried after the QPS error
    c.get_live_weather("150100")                              # back-to-back call waits for the gap
    assert len(session.calls) == 3 and 0.34 in slept
    bad = FakeSession({"/weather": [qps]})
    with pytest.raises(ac.AmapError):
        ac.AmapClient("k", session=bad, min_interval_s=0, sleep=sleep).get_live_weather("150100")  # 2nd failure raises
    assert len(bad.calls) == 2
