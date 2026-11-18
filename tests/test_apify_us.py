from datetime import date, timedelta
from pathlib import Path

import pytest

from winfinder import db
from winfinder.analysis.durability import AdLife, measure
from winfinder.analysis.scoring import analyze
from winfinder.config import load_config
from winfinder.sources import apify_fb
from winfinder.sources.apify import ApifyBudget, ApifyClient, ApifyError, BudgetExhausted

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 18)


def ts(d: date) -> int:
    import datetime as dt
    return int(dt.datetime.combine(d, dt.time(), tzinfo=dt.timezone.utc).timestamp())


def item(ad_id, page="111", name="Shop", start_days=10, active=True, link="https://shop.com/products/x",
         body="Fix your posture", title="Posture Corrector", versions=1, caption="shop.com"):
    start = TODAY - timedelta(days=start_days)
    return {"adArchiveID": ad_id, "pageID": page, "pageName": name, "isActive": active,
            "startDate": ts(start), "endDate": ts(TODAY if active else start + timedelta(days=5)),
            "collationCount": versions, "publisherPlatform": ["FACEBOOK", "INSTAGRAM"],
            "snapshot": {"linkUrl": link, "caption": caption, "title": title, "body": {"text": body}, "cards": []}}


# ------------------------------------------------------------------ đọc dữ liệu
def test_parse_item_active_ad_has_no_stop_date():
    a = apify_fb.parse_item(item("1", versions=3))
    assert (a.domain, a.start_date, a.stop_date, a.versions, a.source) == (
        "shop.com", (TODAY - timedelta(days=10)).isoformat(), None, 3, "apify_fb")
    assert a.link_url.endswith("/products/x") and a.platforms == ["facebook", "instagram"]


def test_parse_item_inactive_cards_and_excluded_link():
    it = item("2", active=False, link=None, caption="", body="")
    it["snapshot"]["cards"] = [{"linkUrl": "https://www.store2.com/products/y", "body": "card body", "title": "T"}]
    a = apify_fb.parse_item(it)
    assert a.domain == "store2.com" and a.stop_date is not None and a.bodies == ["card body"]
    b = apify_fb.parse_item(item("3", link="https://amazon.com/dp/1", caption="SHOP3.COM"), {"amazon.com"})
    assert b.domain == "shop3.com"
    assert apify_fb.parse_item({"snapshot": {}}) is None


def test_library_urls():
    assert "q=posture%20corrector" in apify_fb.library_url("US", keyword="posture corrector")
    assert "view_all_page_id=123" in apify_fb.library_url("US", page_id="123")


# ------------------------------------------------------------------ client + ngân sách (HTTP giả)
class R:
    def __init__(self, status, payload):
        self.status_code, self._p, self.text = status, payload, str(payload)

    def json(self):
        return self._p


class Sess:
    def __init__(self, replies):
        self.replies, self.calls, self.headers = list(replies), [], {}

    def request(self, method, url, timeout=None, **kw):
        self.calls.append((method, url, kw))
        return self.replies.pop(0)


def test_client_runs_polls_and_tracks_cost():
    sess = Sess([R(201, {"data": {"id": "r1", "status": "RUNNING"}}),
                 R(200, {"data": {"id": "r1", "status": "SUCCEEDED", "usageTotalUsd": 0.12, "defaultDatasetId": "d1"}}),
                 R(200, [{"a": 1}, {"error": "blocked"}])])
    b = ApifyBudget(1.0)
    out = ApifyClient(b, token="t", session=sess, poll_seconds=0).run("apify/x", {"q": 1})
    assert out == [{"a": 1}] and b.spent_usd == pytest.approx(0.12)
    assert sess.calls[0][2]["params"]["maxTotalChargeUsd"] == 1.0   # trần = min(per_run_cap, còn lại)
    assert sess.headers["Authorization"] == "Bearer t"
    assert "apify~x" in sess.calls[0][1]


def test_client_never_exceeds_budget_and_handles_minimum_cap():
    b = ApifyBudget(0.6, spent_usd=0.3)
    sess = Sess([R(400, {"error": {"type": "max-total-charge-usd-below-minimum", "message": "min 0.5"}})])
    with pytest.raises(BudgetExhausted):          # còn $0.30 < trần tối thiểu $0.50 → không chạy
        ApifyClient(b, token="t", session=sess, poll_seconds=0).run("a/b", {})
    with pytest.raises(BudgetExhausted):
        ApifyClient(ApifyBudget(0.0), token="t", session=Sess([]), poll_seconds=0).run("a/b", {})
    sess = Sess([R(401, {})])
    with pytest.raises(ApifyError, match="xác thực"):
        ApifyClient(ApifyBudget(1), token="bad", session=sess, poll_seconds=0).run("a/b", {})


# ------------------------------------------------------------------ thu thập US + suy ra ad đã tắt
class FakeClient:
    def __init__(self, by_url):
        self.by_url, self.calls = by_url, []
        self.budget = ApifyBudget(3)

    def run(self, actor, run_input, max_cost=None, label=""):
        url = run_input["startUrls"][0]["url"]
        self.calls.append(url)
        for k, v in self.by_url.items():
            if k in url:
                return v[: run_input["resultsLimit"]]
        return []


def _cfg(tmp_path):
    cfg = load_config(ROOT / "config.yaml")
    cfg.database = tmp_path / "w.db"
    cfg.searches = cfg.searches[:1]
    cfg.searches[0].keywords = ["posture corrector"]
    cfg.expand.min_score = 0
    return cfg


def test_collect_and_sweep_infer_stopped_ads(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.database)
    day1 = TODAY - timedelta(days=7)
    c1 = FakeClient({"q=posture": [item("1"), item("2")], "view_all_page_id=111": [item("1"), item("2"), item("3")]})
    apify_fb.collect(conn, cfg, c1, day1)
    apify_fb.sweep_store_pages(conn, cfg, c1, analyze(conn, cfg, day1), day1)
    assert conn.execute("SELECT COUNT(*) FROM ads WHERE stop_date IS NULL").fetchone()[0] == 3
    # tuần sau: quét trọn page, ad 2 không còn → đã tắt vào khoảng lần thấy cuối
    c2 = FakeClient({"q=posture": [item("1")], "view_all_page_id=111": [item("1"), item("3")]})
    apify_fb.collect(conn, cfg, c2, TODAY)
    st = apify_fb.sweep_store_pages(conn, cfg, c2, analyze(conn, cfg, TODAY), TODAY)
    assert st.stopped_inferred == 1
    assert conn.execute("SELECT stop_date FROM ads WHERE ad_id = '2'").fetchone()[0] == day1.isoformat()


def test_incomplete_sweep_does_not_infer_stops(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.apify.max_ads_per_page = 2
    conn = db.connect(cfg.database)
    day1 = TODAY - timedelta(days=7)
    c1 = FakeClient({"q=posture": [item("1"), item("2")], "view_all_page_id=111": [item("1"), item("2")]})
    apify_fb.collect(conn, cfg, c1, day1)
    c2 = FakeClient({"q=posture": [], "view_all_page_id=111": [item("1"), item("3"), item("4")]})
    st = apify_fb.sweep_store_pages(conn, cfg, c2, analyze(conn, cfg, TODAY), TODAY)
    assert st.stopped_inferred == 0       # chạm giới hạn 2 ad → chưa lấy trọn → không kết luận ad 2 đã tắt


def test_reactivated_ad_clears_stop(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.database)
    db.upsert_ads(conn, [apify_fb.parse_item(item("1"))], country="US", search="s", keyword="k", observed_on=TODAY)
    conn.execute("UPDATE ads SET stop_date = '2026-11-01' WHERE ad_id = '1'")
    db.upsert_ads(conn, [apify_fb.parse_item(item("1"))], country="US", search="s", keyword="k", observed_on=TODAY)
    assert conn.execute("SELECT stop_date FROM ads WHERE ad_id = '1'").fetchone()[0] is None


# ------------------------------------------------------------------ chống thiên lệch người sống sót
def test_unobservable_old_ads_do_not_count_toward_survival():
    old_alive = [AdLife(TODAY - timedelta(days=d), None, f"c{d}", observable=False) for d in (90, 80, 70, 60)]
    m = measure(old_alive, TODAY)
    assert m.survival_30 is None and m.alive_60 == 4      # ad ≥60 ngày vẫn là sự thật quan sát được
    assert any("chỉ thấy khi đang chạy" in n for n in m.notes)


def test_first_scan_momentum_is_unknown_not_inflated():
    # lần quét đầu: chỉ thấy ad còn sống → đồ thị luôn trông như tăng
    ads = [AdLife(TODAY - timedelta(days=d), None, "c", observable=False) for d in (40, 20, 10, 6, 5, 3, 2, 1)]
    m = measure(ads, TODAY, tracked_days=0)
    assert m.active_growth is None and m.archetype == "observing"
    evergreen = ads + [AdLife(TODAY - timedelta(days=d), None, "e", observable=False) for d in (100, 90)]
    assert measure(evergreen, TODAY, tracked_days=0).archetype == "evergreen"
    assert measure(ads, TODAY, tracked_days=30).active_growth is not None   # đủ thời gian theo dõi → đo được


def test_versions_count_as_duplication():
    m = measure([AdLife(TODAY - timedelta(days=5), None, "x", versions=3), AdLife(TODAY, None, "y")], TODAY)
    assert m.duplicated_creatives == 1


def test_us_store_scores_from_first_scan(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.database)
    ads = [apify_fb.parse_item(item(str(i), start_days=d)) for i, d in enumerate((400, 200, 90, 65, 20, 5, 2))]
    db.upsert_ads(conn, ads, country="US", search="s", keyword="k", observed_on=TODAY)
    s = analyze(conn, cfg, TODAY)[0]
    assert s.archetype == "evergreen" and s.durability["survival_30"] is None
    assert s.durability["active_growth"] is None and s.landing_mix == {"pdp": 7}
