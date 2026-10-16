import time
from datetime import date

from features import detail as dt
from messages import Article, Message

NAV = "https://uri.amap.com/navigation?from=111.7,40.8,%E5%AE%B6&to=111.68,40.81,X&mode=bus"


def outing_message():
    md = "\n".join([
        "**妈妈早上好 🌷 今天出去走走？**",
        "> 📍 推荐：**内蒙古图书馆**（图书馆 · 室内）",
        "",
        "**🚌 怎么去**（约 59 分钟 · 步行约 600 米 · 2 元）",
        "1. 步行约 400 米",
        "2. 乘坐 **K6路**：「内蒙古检察院」上车 →「学府康都」下车（11 站）",
        "",
        "**💡 小贴士**",
        "- 9:30 以后再出门，错开早高峰，车上更容易有座位",
        "- 走累了就找地方坐一坐，不赶时间，慢慢逛",
        f"[📍 打开高德地图导航]({NAV})",
    ])
    return Message("今日出行建议 · 内蒙古图书馆", md, url=NAV,
                   articles=(Article("妈妈早上好，今天去内蒙古图书馆走走？", "图书馆 · 室内\n点开看完整路线和出行贴士 ›",
                                     NAV, "https://store.is.autonavi.com/showpic/x"),))


def test_page_shows_full_text_big_button_and_senior_styles():
    page = dt.render_page("t", outing_message().markdown)
    assert "9:30 以后再出门，错开早高峰，车上更容易有座位" in page  # untruncated
    assert f'<a class="btn" href="{NAV.replace("&", "&amp;")}" rel="noreferrer">📍 打开高德地图导航</a>' in page
    assert "<h1>" in page and "<ol>" in page and "<ul>" in page and 'class="note"' in page
    assert "font: 21px/1.75" in page and "#FAF7F0" in page and "#2F2F2F" in page
    assert 'name="viewport"' in page and 'content="no-referrer"' in page and "noindex" in page


def test_page_escapes_everything_and_only_allows_http_links():
    md = '<script>alert(1)</script>\n**<img src=x onerror=alert(1)>**\n[点我](javascript:alert(1))\n<font color="info">畅通</font>'
    page = dt.markdown_to_html(md)
    assert "<script>" not in page and "<img" not in page and "<font" not in page and "畅通" in page
    assert 'href="javascript' not in page  # non-http "links" stay plain text


def test_event_headings_get_anchors_for_multi_card_links():
    html_ = dt.markdown_to_html("**1. 民歌音乐会**\n> 🗓 10月17日\n**2. 🆕 非遗展演**")
    assert '<h2 id="item-1">' in html_ and '<h2 id="item-2">' in html_


def test_publish_swaps_amap_links_for_detail_page(tmp_path):
    store = dt.DetailStore(tmp_path, "http://203.0.113.10:8000/")
    out = store.publish(outing_message())
    (card,) = out.articles
    assert card.url.startswith("http://203.0.113.10:8000/detail/") and "#" not in card.url
    assert card.picurl == "https://store.is.autonavi.com/showpic/x" and card.title.startswith("妈妈早上好")
    report_id = card.url.rsplit("/", 1)[1]
    assert len(report_id) >= 20 and store.load(report_id)["markdown"] == outing_message().markdown
    assert NAV in store.load(report_id)["markdown"]  # the AMap link lives on as the page's button


def test_multi_card_links_point_to_item_anchors_and_non_nav_cards_untouched(tmp_path):
    store = dt.DetailStore(tmp_path, "http://srv")
    events = Message("近期活动", "**1. A**\n**2. B**", articles=(Article("A", "a", NAV), Article("B", "b", NAV)))
    out = store.publish(events)
    assert [a.url.rsplit("#", 1)[1] for a in out.articles] == ["item-1", "item-2"]
    tv = Message("剧", "x", articles=(Article("剧", "d", "https://m.douban.com/movie/subject/1/"),))
    assert store.publish(tv) is tv  # dramas keep their Douban links
    assert dt.DetailStore(tmp_path, "").publish(outing_message()).articles[0].url == NAV  # no PUBLIC_BASE_URL


def test_load_rejects_bad_ids_and_expired_pages(tmp_path):
    now = [time.time()]
    store = dt.DetailStore(tmp_path, "http://srv", clock=lambda: now[0])
    report_id = store.publish(outing_message()).articles[0].url.rsplit("/", 1)[1]
    assert store.load("../state") is None and store.load("short") is None
    now[0] += 15 * 86400
    assert store.load(report_id) is None


def test_detail_route_serves_page_and_404s(monkeypatch, tmp_path, settings, state):
    from fastapi.testclient import TestClient

    import main
    store = dt.DetailStore(tmp_path, "http://srv")
    report_id = store.publish(outing_message()).articles[0].url.rsplit("/", 1)[1]
    monkeypatch.setattr(main, "get_services", lambda: main.Services(settings, object(), None, state, details=store))
    with TestClient(main.app) as c:
        r = c.get(f"/detail/{report_id}")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "错开早高峰" in r.text and r.headers["cache-control"] == "private, no-store"
        assert r.headers["referrer-policy"] == "no-referrer"
        missing = c.get("/detail/" + "A" * 22)
        assert missing.status_code == 404 and "已经过期" in missing.text
        assert c.get("/detail/..%2Fstate").status_code == 404


def test_deliver_publishes_only_for_card_formats(monkeypatch, tmp_path, settings, state):
    import dataclasses

    import main

    class Rec:
        def __init__(self):
            self.sent = []

        def send_message(self, users, message, fmt):
            self.sent.append((fmt, message.articles[0].url))

    for fmt, expect_detail in (("news", True), ("textcard", True), ("text", False)):
        rec = Rec()
        sv = main.Services(dataclasses.replace(settings, wecom_message_type=fmt), object(), rec, state,
                           details=dt.DetailStore(tmp_path, "http://srv"))
        main.deliver(sv, ["mom"], outing_message())
        assert rec.sent[0][1].startswith("http://srv/detail/") is expect_detail, fmt


def test_cards_have_concise_untruncated_descriptions(settings, state):
    from clients.amap_client import AmapClient
    from features import outing
    from features.commute import build_commute_message
    from tests import fixtures as fx
    from tests.conftest import FakeResponse, FakeSession

    def amap():
        return AmapClient("k", session=FakeSession({
            "/direction/driving": [FakeResponse(fx.DRIVING)],
            "/weather": [FakeResponse(fx.LIVE), FakeResponse(fx.FORECAST)],
            "/place/text": [fx.poi_echo(photo="https://store.is.autonavi.com/showpic/p")],
            "/direction/transit": [FakeResponse(fx.TRANSIT)],
        }))
    o = outing.build_outing_message(amap(), settings, state, date(2026, 10, 13), event_sources=[])
    c = build_commute_message(amap(), settings, today_date=date(2026, 10, 13))
    for msg in (o, c):
        (card,) = msg.articles
        assert len(card.description.encode()) <= 512, "would be truncated by WeCom"
        assert card.description.endswith("›") and card.url.startswith("https://uri.amap.com")
        assert "[📍 打开高德地图导航](" in msg.markdown
    assert o.articles[0].title.startswith("妈妈早上好，今天去") and o.articles[0].picurl
    assert "坐 12路，不用换乘" in o.articles[0].description
