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
