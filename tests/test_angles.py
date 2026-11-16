import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from winfinder import db, demo, link
from winfinder.analysis.angles import (
    SCHEMA, AdUnit, AngleDef, AngleResult, ClaudeAngleAnalyzer, ad_units, angle_stats, angle_status, parse_result,
)
from winfinder.analysis.scoring import analyze
from winfinder.angles_pipeline import run_angles
from winfinder.angles_report import render
from winfinder.config import load_config
from winfinder.sources.adlibrary import parse_ad

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 16)


def _ad(i, store, body, start, title="T"):
    return parse_ad({"id": str(i), "page_id": "1", "page_name": "P", "ad_delivery_start_time": start,
                     "ad_creative_link_captions": [store.upper()], "ad_creative_link_titles": [title],
                     "ad_creative_bodies": [body], "eu_total_reach": 100})


def test_ad_units_dedupe_text_across_stores():
    conn = db.connect(":memory:")
    ads = [_ad(1, "a.com", "Gift for mom", "2026-11-01"), _ad(2, "b.com", "Gift for mom", "2026-11-03"),
           _ad(3, "a.com", "Back pain?", "2026-10-01")]
    db.upsert_ads(conn, ads, country="IE", search="s", keyword="k", observed_on=AS_OF)
    units, meta = ad_units(conn, ["a.com", "b.com"])
    assert len(units) == 2
    gift = next(u for u in units if "Gift" in u.text)
    assert gift.ad_ids == ["1", "2"] and gift.stores == ["a.com", "b.com"] and gift.first_start == "2026-11-01"
    assert meta["3"]["start"] == "2026-10-01"


def test_parse_result_drops_unknown_refs_and_angles():
    units = [AdUnit("a1", "x", ["1"], ["s"], None, 0), AdUnit("a2", "y", ["2"], ["s"], None, 0)]
    data = {"angles": [{"id": "A1", "name": "n", "persona": "p", "pain_point": "pp", "promise": "pr",
                        "hook_type": "gift"}],
            "assignments": [{"ad": "a1", "angle": "A1"}, {"ad": "a2", "angle": "A9"}, {"ad": "zz", "angle": "A1"}],
            "untapped_angles": [{"name": "u", "persona": "p", "why": "w"}]}
    r = parse_result(data, units)
    assert r.assignments == {"a1": "A1"} and r.untapped[0]["name"] == "u"


def test_angle_status():
    start = AS_OF - timedelta(weeks=10)
    recent = AS_OF - timedelta(days=10)
    assert angle_status("scaling", start, AS_OF, start, 5) == "rising"
    assert angle_status("treadmill", start, AS_OF, start, 5) == "churn"
    assert angle_status("declining", start, AS_OF, start, 2) == "declining"
    assert angle_status("steady", start, AS_OF, start, 0) == "stopped"
    assert angle_status("early", recent, AS_OF, start, 3) == "new"
    # store mới mở: angle mới nhưng sản phẩm cũng mới → không gắn 'new'
    assert angle_status("early", recent, AS_OF, recent, 3) == "rising"


def test_angle_stats_shares_sum_to_one():
    units = [AdUnit("a1", "x", ["1", "2"], ["s"], None, 10), AdUnit("a2", "y", ["3"], ["s"], None, 5)]
    meta = {"1": {"start": "2026-11-10"}, "2": {"start": "2026-11-12"}, "3": {"start": "2026-11-14"}}
    res = AngleResult([AngleDef("A1", "n1", "p", "pp", "pr", "gift"), AngleDef("A2", "n2", "p", "pp", "pr", "offer"),
                       AngleDef("A3", "unused", "p", "pp", "pr", "other")], {"a1": "A1", "a2": "A2"}, [])
    stats = angle_stats(res, units, meta, AS_OF)
    assert {s.angle.name for s in stats} == {"n1", "n2"}  # angle không có ad bị bỏ
    assert abs(sum(s.recent_share for s in stats) - 1) < 1e-6


def test_schema_strict_everywhere():
    def check(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for v in node["properties"].values():
                check(v)
        if node.get("type") == "array":
            check(node["items"])
    check(SCHEMA)


# ------------------------------------------------------------------ Claude (client giả)
class FakeStream:
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.resp


def test_claude_angle_request():
    payload = {"angles": [{"id": "A1", "name": "Quà", "persona": "p", "pain_point": "x", "promise": "y",
                           "hook_type": "gift"}],
               "assignments": [{"ad": "a1", "angle": "A1"}], "untapped_angles": []}
    resp = SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5",
                           content=[SimpleNamespace(type="text", text=json.dumps(payload))])
    captured = {}

    def stream(**kw):
        captured.update(kw)
        return FakeStream(resp)

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    units = [AdUnit("a1", "Gift for mom | Shop now", ["1"], ["s"], None, 0)]
    r = ClaudeAngleAnalyzer(client=client).analyze("posture corrector", "posture corrector", units)
    assert r.assignments == {"a1": "A1"}
    assert captured["output_config"]["format"]["schema"] == SCHEMA
    assert captured["output_config"]["effort"] == "medium"
    assert captured["fallbacks"] == "default"
    assert "a1: Gift for mom" in captured["messages"][0]["content"]


def test_claude_angle_truncated_output_is_rejected():
    resp = SimpleNamespace(stop_reason="max_tokens", model="m", content=[SimpleNamespace(type="text", text="{")])
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream(resp))))
    assert ClaudeAngleAnalyzer(client=client).analyze("p", None, []) is None


# ------------------------------------------------------------------ end-to-end demo
def _linked():
    cfg = load_config(ROOT / "config.yaml")
    cfg.trends.geos = ["US"]
    cfg.link.min_ad_score = 0
    cfg.link.fetch_pages = False
    conn = db.connect(":memory:")
    for search, country, raw in demo.generate(AS_OF):
        db.upsert_ads(conn, [parse_ad(raw, cfg.exclude_domains)], country=country, search=search,
                      keyword=demo.SEARCH_KEYWORD[search], observed_on=AS_OF)
    for d, products in demo.shopify_products().items():
        db.save_domain(conn, d, registered_at=None, first_cert_at=None, is_shopify=True,
                       product_count=len(products), products=products, checked_on=AS_OF)
    linked = link.link_stores(conn, cfg, analyze(conn, cfg, AS_OF), demo.DemoTrendsProvider(AS_OF), AS_OF,
                              extractor=demo.DemoExtractor())
    return cfg, conn, link.to_dicts(linked)


def test_demo_angles_end_to_end_and_cache():
    cfg, conn, linked = _linked()
    an = demo.DemoAngleAnalyzer()
    products = run_angles(conn, cfg, linked, an, AS_OF)
    by = {p.product: p for p in products}
    posture = by["posture corrector"]
    assert sorted(posture.stores) == ["postureglow.com", "spinealign.co"]  # gộp 2 store cùng sản phẩm
    st = {a.angle.name: a.status for a in posture.angles}
    assert st["Quà tặng cho mẹ"] == "rising"
    eye = by["red light therapy glasses"]
    st = {a.angle.name: a.status for a in eye.angles}
    assert st["Ca đêm vẫn tươi tỉnh"] == "new"
    assert eye.untapped
    calls = an.calls
    again = run_angles(conn, cfg, linked, an, AS_OF)  # nội dung không đổi → dùng cache
    assert an.calls == calls and all(p.cached for p in again)
    # không có analyzer vẫn đọc được kết quả đã lưu
    assert len(run_angles(conn, cfg, linked, None, AS_OF)) == len(products)
    assert "Quà tặng cho mẹ" in render(products, AS_OF)


def test_skip_products_with_too_few_ads():
    cfg, conn, linked = _linked()
    cfg.angles.min_ads = 1000
    assert run_angles(conn, cfg, linked, demo.DemoAngleAnalyzer(), AS_OF) == []
