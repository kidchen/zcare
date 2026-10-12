DRIVING = {"status": "1", "route": {"paths": [{
    "distance": "14300", "duration": "1320", "traffic_lights": "12",
    "steps": [
        {"road": "新华大街", "distance": "6000", "tmcs": [
            {"status": "畅通", "distance": "5000"}, {"status": "缓行", "distance": "800"}]},
        {"road": [], "distance": "200", "tmcs": []},
        {"road": "兴安北路", "distance": "5000", "tmcs": [
            {"status": "拥堵", "distance": "300"}, {"status": "缓行", "distance": "200"}]},
        {"road": "成吉思汗大街", "distance": "3100", "tmcs": [{"status": "畅通", "distance": "3100"}]},
    ]}]}}

LIVE = {"status": "1", "lives": [{"weather": "晴", "temperature": "15", "winddirection": "西北",
        "windpower": "≤3", "humidity": "30", "reporttime": "2026-10-11 07:00:00"}]}
LIVE_STORM = {"status": "1", "lives": [{"weather": "沙尘暴", "temperature": "10", "winddirection": "西北",
        "windpower": "8", "humidity": "20", "reporttime": "x"}]}
FORECAST = {"status": "1", "forecasts": [{"casts": [{"date": "2026-10-11", "dayweather": "晴",
        "nightweather": "多云", "daytemp": "18", "nighttemp": "6", "daypower": "≤3"}]}]}

POI = {"status": "1", "pois": [{"name": "内蒙古博物院", "address": "新华东街27号",
        "location": "111.700000,40.830000", "type": "科教文化服务;博物馆"}]}


def poi_echo(photo=""):
    """Fake AMap POI search that returns a POI named exactly as the searched keyword."""
    from tests.conftest import FakeResponse

    def respond(url, kw):
        name = kw["params"]["keywords"]
        poi = {"name": name, "address": "某街1号", "location": "111.700000,40.830000", "type": "科教文化服务;博物馆"}
        if photo:
            poi["photos"] = [{"url": photo}]
        return FakeResponse({"status": "1", "pois": [poi]})
    return respond

# One direct bus (walk 450m) and one 2-transfer plan that should be rejected.
TRANSIT = {"status": "1", "route": {"transits": [
    {"cost": "2", "duration": "2100", "walking_distance": "450", "segments": [
        {"walking": {"distance": "200", "duration": "180"},
         "bus": {"buslines": [{"name": "12路(火车站--东客站)", "type": "普通公交线路",
                 "departure_stop": {"name": "家门口站"}, "arrival_stop": {"name": "博物院站"},
                 "via_num": "5", "distance": "5000", "duration": "1500"}]},
         "railway": []},
        {"walking": {"distance": "250", "duration": "200"}, "bus": {"buslines": []}, "railway": []},
    ]},
    {"cost": "4", "duration": "1500", "walking_distance": "300", "segments": [
        {"walking": [], "bus": {"buslines": [{"name": "1路", "type": "普通公交线路",
            "departure_stop": {"name": "A"}, "arrival_stop": {"name": "B"}, "via_num": "1"}]}},
        {"walking": [], "bus": {"buslines": [{"name": "地铁1号线", "type": "地铁线路",
            "departure_stop": {"name": "B"}, "arrival_stop": {"name": "C"}, "via_num": "1"}]}},
        {"walking": [], "bus": {"buslines": [{"name": "3路", "type": "普通公交线路",
            "departure_stop": {"name": "C"}, "arrival_stop": {"name": "D"}, "via_num": "1"}]}},
    ]},
]}}

DOUBAN_LIST = {"subjects": [
    {"id": "1", "title": "高分悬疑剧", "rate": "9.1", "url": "https://movie.douban.com/subject/1/"},
    {"id": "2", "title": "低分剧", "rate": "6.0", "url": "u2"},
    {"id": "3", "title": "暂无评分", "rate": "", "url": "u3"},
    {"id": "4", "title": "年代剧", "rate": "8.6", "url": "u4"},
]}

DOUBAN_GENRE_LIST = {"data": DOUBAN_LIST["subjects"]}  # /j/new_search_subjects shape

DOUBAN_MOBILE = {
    "title": "高分悬疑剧", "year": "2026", "genres": ["剧情", "悬疑"],
    "intro": "完整的\n剧情简介。", "rating": {"value": 9.2, "count": 1000},
    "vendors": [
        {"title": "腾讯视频", "accessible": True, "payment_desc": "VIP免费"},
        {"title": "爱奇艺", "accessible": True},
        {"title": "下架平台", "accessible": False},
    ],
}

DOUBAN_DETAIL = """
<html><body>
<span class="year">(2025)</span>
<span property="v:genre">剧情</span><span property="v:genre">悬疑</span>
<strong class="ll rating_num" property="v:average">9.2</strong>
<span property="v:summary">短简介</span>
<span class="all hidden">  完整的
  剧情简介。 </span>
<ul class="bs"><li><a class="playBtn" data-cn="腾讯视频" href="#">腾讯视频</a></li>
<li><a class="playBtn" data-cn="爱奇艺" href="#">爱奇艺</a></li></ul>
</body></html>
"""
