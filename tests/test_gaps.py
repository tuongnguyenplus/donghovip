import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from winfinder import db, demo, link
from winfinder.analysis.angles import AdUnit, AngleDef, AngleResult, AngleStats, parse_result
from winfinder.analysis.gaps import gap_rows, verdict
from winfinder.analysis.scoring import analyze
from winfinder.angles_pipeline import run_angles
from winfinder.config import load_config
from winfinder.gaps_report import language_rows, render_gaps, render_language
from winfinder.sources.adlibrary import parse_ad
from winfinder.sources.social import SocialPost

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 18)


def post(i, text, days=10, score=5):
    return SocialPost("reddit", str(i), "", text, f"https://r/{i}", (AS_OF - timedelta(days=days)).isoformat(), score, 1, "c")


@pytest.mark.parametrize("args,expected", [
    ((0.30, 20, 2.0, 0.00, None, None, 0), "gap"),
    ((0.30, 20, 1.0, 0.15, "churn", 0.05, 0), "fumbled"),
    ((0.30, 20, 1.0, 0.15, "steady", 0.5, 3), "proven"),
    ((0.02, 2, None, 0.40, "rising", None, 0), "oversupplied"),
    ((0.15, 6, 0.5, 0.30, "declining", 0.1, 0), "oversupplied"),   # cầu đang giảm, cung lớn
    ((0.01, 1, None, 0.01, None, None, 0), "minor"),
])
def test_verdicts(args, expected):
    assert verdict(*args) == expected


def test_shared_taxonomy_parse_rejects_invented_phrases_and_unknown_refs():
    units = [AdUnit("a1", "Desk job back pain? Fix it", ["1"], ["s.com"], None, 0)]
    posts = [post(1, "my neck hurts from studying all night"), post(2, "how much")]
    data = {"angles": [{"id": "A1", "name": "Văn phòng", "persona": "p", "pain_point": "x", "promise": "y",
                        "hook_type": "problem-solution", "pain_keywords": [], "social_queries": []},
                       {"id": "A2", "name": "Học sinh", "persona": "p", "pain_point": "x", "promise": "y",
                        "hook_type": "problem-solution", "pain_keywords": [], "social_queries": []}],
            "assignments": [{"ad": "a1", "angle": "A1"}],
            "post_assignments": [{"post": "o1", "angle": "A2"}, {"post": "o2", "angle": "none"},
                                 {"post": "o9", "angle": "A1"}],
            "customer_phrases": [{"angle": "A2", "post": "o1", "phrase": "my neck hurts from studying", "kind": "pain"},
                                 {"angle": "A2", "post": "o1", "phrase": "studying ruined my spine", "kind": "pain"}],
            "untapped_angles": []}
    r = parse_result(data, units, "m", posts)
    assert r.post_assignments == {"o1": "A2"}
    assert [x["phrase"] for x in r.phrases] == ["my neck hurts from studying"]    # cụm bịa bị loại
    assert AngleResult.from_dict(json.loads(json.dumps(r.to_dict()))).phrases == r.phrases


def test_gap_rows_shares_and_organic_only_angle():
    a1 = AngleDef("A1", "Văn phòng", "p", "x", "y", "problem-solution")
    a2 = AngleDef("A2", "Học sinh", "p", "x", "y", "problem-solution")
    a3 = AngleDef("A3", "Ưu đãi", "p", "x", "y", "offer")
    posts = [post(i, "desk") for i in range(6)] + [post(10 + i, "study", days=5) for i in range(6)]
    res = AngleResult([a1, a2, a3], {}, [], "", {f"o{i}": ("A1" if i <= 6 else "A2") for i in range(1, 13)}, [])
    stats = [AngleStats(a1, "steady", "", 10, ["s"], 0, [], 0.6, 1.0, None, 0.5, 3),
             AngleStats(a3, "rising", "", 8, ["s"], 0, [], 0.4, 1.0, None)]
    rows = {r.angle_id: r for r in gap_rows(res, stats, posts, AS_OF)}
    assert rows["A2"].verdict == "gap" and rows["A2"].supply_share == 0 and rows["A2"].demand_share == 0.5
    assert rows["A1"].verdict == "proven"
    assert rows["A3"].verdict == "generic"                  # ad chỉ giảm giá không phải persona → không gọi là bão hoà
    assert rows["A2"].opportunity > rows["A1"].opportunity


def _demo_products():
    cfg = load_config(ROOT / "config.yaml")
    cfg.trends.geos = ["US"]
    cfg.link.min_ad_score = 0
    cfg.link.fetch_pages = False
    conn = db.connect(":memory:")
    for search, country, raw in demo.generate(AS_OF):
        db.upsert_ads(conn, [parse_ad(raw, cfg.exclude_domains)], country=country, search=search,
                      keyword=demo.SEARCH_KEYWORD[search], observed_on=AS_OF)
    linked = link.link_stores(conn, cfg, analyze(conn, cfg, AS_OF), demo.DemoTrendsProvider(AS_OF), AS_OF,
                              extractor=demo.DemoExtractor())
    src, an = demo.DemoSocialSource(AS_OF), demo.DemoAngleAnalyzer()
    prods = run_angles(conn, cfg, link.to_dicts(linked), an, AS_OF, sources=[src])
    return conn, cfg, linked, prods, src, an


def test_demo_gap_map_end_to_end_and_cache():
    conn, cfg, linked, prods, src, an = _demo_products()
    posture = next(p for p in prods if p.product == "posture corrector")
    v = {g["name"]: g["verdict"] for g in posture.gaps}
    assert v["Học sinh ôn thi bị gù"] == "gap" and v["Đau lưng dân văn phòng"] == "proven"
    assert v["Ưu đãi / cam kết hoàn tiền"] == "generic"
    assert posture.corpus["posts"] > 0 and posture.phrases
    assert len({(x["angle"], x["phrase"]) for x in posture.phrases}) == len(posture.phrases)   # không lặp
    calls_an, calls_src = an.calls, src.calls
    again = run_angles(conn, cfg, link.to_dicts(linked), an, AS_OF, sources=[src])
    assert an.calls == calls_an and src.calls == calls_src                                  # cache: không gọi lại
    assert [g["verdict"] for g in next(p for p in again if p.product == "posture corrector").gaps] == \
        [g["verdict"] for g in posture.gaps]


def test_reports_render():
    *_, prods, _src, _an = _demo_products()
    docs = [p.to_dict() for p in prods]
    html = render_gaps(docs, AS_OF.isoformat())
    assert "Khoảng trống" in html and "Học sinh ôn thi bị gù" in html and 'id="g-posture-corrector"' in html
    rows = language_rows(docs)
    assert rows and all(r["count"] for r in rows)
    lang = render_language(docs, AS_OF.isoformat())
    assert "Tải CSV" in lang and "my neck hurts from studying" in lang and "</script>" in lang
