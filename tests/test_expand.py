import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from winfinder import db, demo
from winfinder.analysis.scoring import analyze
from winfinder.config import load_config
from winfinder.expand import expand_stores
from winfinder.sources.adlibrary import AdLibraryClient, parse_ad
from winfinder.sources.landing import analyse_html, classify_url

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 16)


# ------------------------------------------------------------------ phân loại landing
@pytest.mark.parametrize("url,store,kind", [
    ("https://shop.com/products/glow-pro?v=1", "shop.com", "pdp"),
    ("shop.com/collections/all", "shop.com", "collection"),
    ("https://shop.com/", "shop.com", "home"),
    ("https://shop.com/pages/why-it-works", "shop.com", "advertorial"),
    ("https://shop.com/blogs/news/top-5-reasons-to-try", "shop.com", "listicle"),
    ("https://shop.com/pages/quiz", "shop.com", "quiz"),
    ("https://healthnews.com/story/1", "shop.com", "presell"),
    ("https://healthnews.com/7-reasons-women-love-it", "shop.com", "listicle"),
    (None, "shop.com", None),
])
def test_classify_url(url, store, kind):
    assert classify_url(url, store) == kind


ADVERTORIAL = """<html><head><title>Why women over 40 are switching</title></head><body>
<h1>I tried it for 30 days</h1><p>Story...</p>
<a href="https://lifteyes.co/products/pro">Check availability</a>
<a href="https://lifteyes.co/products/pro?ref=a">Get 50% off today</a>
<a href="https://facebook.com/share">Share</a><a href="/privacy">Privacy</a></body></html>"""


def test_analyse_html_resolves_advertorial_to_store():
    r = analyse_html("https://beautyinsiderdaily.com/", ADVERTORIAL)
    assert (r.kind, r.resolves_to, r.landing_type) == ("presell", "lifteyes.co", "advertorial")


def test_analyse_html_listicle_and_store_and_unknown():
    listicle = ADVERTORIAL.replace("<title>Why women over 40 are switching</title>", "<title>Top 5 eye gadgets</title>")
    assert analyse_html("https://x.com/", listicle).landing_type == "listicle"
    assert analyse_html("https://s.com/", '<form action="/cart/add"><button>Add to cart</button></form>').kind == "store"
    one_link = '<a href="https://other.com/">other</a>'
    assert analyse_html("https://x.com/", one_link).kind == "unknown"  # 1 link lạc không đủ kết luận


# ------------------------------------------------------------------ Ad Library: tìm theo page
class FakeResp:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.params = []

    def get(self, url, params=None, timeout=None):
        self.params.append(params)
        return FakeResp({"data": [{"id": "1"}]})


def test_search_pages_params():
    sess = FakeSession()
    c = AdLibraryClient("t", session=sess, sleep_seconds=0)
    assert [a["id"] for a in c.search_pages(["123", "456"], "IE", AS_OF)] == ["1"]
    p = sess.params[0]
    assert p["search_page_ids"] == "[123, 456]" and "search_terms" not in p
    with pytest.raises(ValueError):
        list(c.search_pages([str(i) for i in range(11)], "IE", AS_OF))


# ------------------------------------------------------------------ DB cũ được nâng cấp
def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE ads (ad_id TEXT PRIMARY KEY, page_id TEXT NOT NULL, page_name TEXT, domain TEXT,
        created_at TEXT, start_date TEXT, stop_date TEXT, bodies TEXT, titles TEXT, captions TEXT, snapshot_url TEXT,
        platforms TEXT, languages TEXT, eu_total_reach INTEGER, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)""")
    old.execute("INSERT INTO ads (ad_id, page_id, domain, first_seen, last_seen) VALUES ('1','p','a.com','x','x')")
    old.commit()
    old.close()
    conn = db.connect(path)
    row = conn.execute("SELECT * FROM ads").fetchone()
    assert row["landing_domain"] == "a.com" and row["via_domain"] is None


# ------------------------------------------------------------------ end-to-end trên demo
def _setup():
    cfg = load_config(ROOT / "config.yaml")
    cfg.expand.countries = ["IE"]
    conn = db.connect(":memory:")
    for search, country, raw in demo.generate(AS_OF):
        db.upsert_ads(conn, [parse_ad(raw, cfg.exclude_domains)], country=country, search=search,
                      keyword="k", observed_on=AS_OF)
    return cfg, conn


def test_expand_finds_second_page_and_satellite_advertorial():
    cfg, conn = _setup()
    before = {s.key: s for s in analyze(conn, cfg, AS_OF)}
    client = demo.DemoAdLibraryClient(AS_OF)
    st = expand_stores(conn, cfg, client, list(before.values()), AS_OF, resolver=demo.demo_resolver)
    after = {s.key: s for s in analyze(conn, cfg, AS_OF)}

    pg = after["postureglow.com"]
    assert pg.pages == 2 and pg.durability["active_now"] > before["postureglow.com"].durability["active_now"]

    le = after["lifteyes.co"]
    assert le.pages == 2 and le.via_domains == ["beautyinsiderdaily.com"]
    assert le.landing_mix.get("advertorial", 0) > 0
    assert le.direct_share < pg.direct_share            # chạy qua advertorial → kém ưu tiên hơn chạy thẳng
    assert "beautyinsiderdaily.com" not in after        # trang trung gian không phải store
    assert st.presell_resolved == 1 and st.ads_relinked > 0
    assert any(c[0] == "pages" for c in client.calls)

    # chạy lại: page vừa quét không quét lại, ad đã nối về store không bị tách ra
    client2 = demo.DemoAdLibraryClient(AS_OF)
    expand_stores(conn, cfg, client2, list(after.values()), AS_OF, resolver=demo.demo_resolver)
    assert not any(c[0] == "pages" for c in client2.calls)
    for _s, country, raw in demo.generate(AS_OF):  # thu thập lại theo keyword như tuần sau
        db.upsert_ads(conn, [parse_ad(raw, cfg.exclude_domains)], country=country, search="x", keyword="k",
                      observed_on=AS_OF)
    for country, raw in demo.hidden_raws(AS_OF):
        db.upsert_ads(conn, [parse_ad(raw, cfg.exclude_domains)], country=country, search="x", keyword="k",
                      observed_on=AS_OF)
    again = {s.key: s for s in analyze(conn, cfg, AS_OF)}
    assert again["lifteyes.co"].via_domains == ["beautyinsiderdaily.com"]
    assert "beautyinsiderdaily.com" not in again


def test_expand_respects_min_score_and_stopped():
    cfg, conn = _setup()
    cfg.expand.min_score = 1000
    st = expand_stores(conn, cfg, demo.DemoAdLibraryClient(AS_OF), analyze(conn, cfg, AS_OF), AS_OF)
    assert st.stores == 0 and st.new_ads == 0


def test_search_filter_keeps_all_ads_of_matching_store():
    cfg, conn = _setup()
    expand_stores(conn, cfg, demo.DemoAdLibraryClient(AS_OF), analyze(conn, cfg, AS_OF), AS_OF,
                  resolver=demo.demo_resolver)
    full = {s.key: s.ad_count for s in analyze(conn, cfg, AS_OF)}
    eye = {s.key: s.ad_count for s in analyze(conn, cfg, AS_OF, search="eye-care")}
    assert eye["lifteyes.co"] == full["lifteyes.co"]   # gồm cả ad từ page vệ tinh
