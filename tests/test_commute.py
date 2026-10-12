from clients.amap_client import AmapClient
from features.commute import build_commute_message
from tests.conftest import FakeResponse, FakeSession
from tests import fixtures as fx


def test_commute_message(settings):
    amap = AmapClient("k", session=FakeSession({
        "/direction/driving": [FakeResponse(fx.DRIVING)],
        "/weather": [FakeResponse(fx.LIVE), FakeResponse(fx.FORECAST)],
    }))
    msg = build_commute_message(amap, settings)
    assert "爸爸早上好" in msg.markdown
    assert "**22 分钟**" in msg.markdown
    assert "局部缓行" in msg.markdown
    assert "兴安北路：拥堵约 0.5 公里" in msg.markdown
    assert "新华大街：缓行约 0.8 公里" in msg.markdown
    assert "今天 6~18°C" in msg.markdown
    assert msg.url.startswith("https://uri.amap.com/navigation")
