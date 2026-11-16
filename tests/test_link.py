from datetime import date
from pathlib import Path

import pytest

from winfinder import db, demo
from winfinder.analysis.keywords import extract, singular
from winfinder.analysis.scoring import analyze
from winfinder.config import load_config
from winfinder.link import link_stores, verdict, win_score
from winfinder.link_report import render
from winfinder.sources.adlibrary import parse_ad

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 16)


# ------------------------------------------------------------------ rút keyword
@pytest.mark.parametrize(
    "titles,products,kwargs,seed",
    [
        (["PostureGlow™ Smart Posture Corrector", "Posture Corrector — 50% OFF today"],
         ["PostureGlow™ Smart Posture Corrector 2.0", "Gift Card"],
         {"domain": "postureglow.com", "page_names": ["PostureGlow"]}, "posture corrector"),
        (["Cat Hair Remover — reusable", "Cat Hair Remover Roller"], [],
         {"domain": "furrygroom.com", "context_keywords": ["cat hair remover"]}, "cat hair remover"),
        (["Heated Dog Vest", "Heated dog vest for winter walks"], ["Heated Dog Vest - USB"],
         {"domain": "petlint.io"}, "heated dog vest"),
        (["Galaxy Star Projector", "Star Projector for bedroom"], [], {}, "star projector"),
    ],
)
def test_extract_seed(titles, products, kwargs, seed):
    assert extract(titles, [], products, **kwargs).seed == seed


def test_extract_drops_bound_subphrases_and_unrelated():
    r = extract(["PostureGlow Smart Posture Corrector", "Posture Corrector sale"], [], domain="postureglow.com")
    assert "smart posture" not in r.keywords
    r = extract(["Heated Dog Vest", "Heated dog vest for winter walks"], [])
    assert "winter walk" not in r.keywords


def test_extract_keeps_shared_head():
    r = extract(["Heated Dog Vest", "Waterproof Dog Vest", "dog vest"], [])
    assert r.seed == "dog vest" and {"heated dog vest", "waterproof dog vest"} <= set(r.keywords)


def test_brand_and_marketing_removed():
    r = extract(["SpineAlign Posture Corrector — Free Shipping", "SpineAlign Posture Corrector"], [],
                domain="spinealign.co", page_names=["SpineAlign"])
    assert all("spinealign" not in k and "shipping" not in k for k in r.keywords)


def test_shopify_hero_only_when_matching_ads():
    r = extract(["Cat Hair Remover"], [], ["Cat Hair Remover Roller", "Gift Card"])
    assert r.source == "shopify+ads"
    assert extract(["Cat Hair Remover"], [], ["Gift Card"]).source == "ads"


def test_singular():
    assert [singular(w) for w in ["correctors", "brushes", "batteries", "glass", "vest"]] == \
        ["corrector", "brush", "battery", "glass", "vest"]


def test_empty_text():
    assert extract([], []).seed is None


# ------------------------------------------------------------------ kết luận
def test_verdicts():
    assert verdict(True, {"label": "rising"}) == "strong"
    assert verdict(False, {"label": "breakout"}) == "watch"
    assert verdict(True, {"label": "seasonal", "in_season": True}) == "seasonal_now"
    assert verdict(True, {"label": "seasonal", "in_season": False}) == "seasonal_off"
    assert verdict(True, {"label": "spike"}) == "risky"
    assert verdict(True, {"label": "stable"}) == "saturated"
    assert verdict(True, None) == "ad_driven"
    assert verdict(True, {"label": "insufficient"}) == "ad_driven"


def test_win_score_penalizes_risky():
    assert win_score(80, 80, "risky") < win_score(80, 80, "saturated")
    assert win_score(80, None, "ad_driven") == win_score(80, 0, "ad_driven")


# ------------------------------------------------------------------ end-to-end trên demo
def _demo():
    cfg = load_config(ROOT / "config.yaml")
    conn = db.connect(":memory:")
    for search, country, raw in demo.generate(AS_OF):
        db.upsert_ads(conn, [parse_ad(raw, cfg.exclude_domains)], country=country, search=search,
                      keyword=demo.SEARCH_KEYWORD[search], observed_on=AS_OF)
    for d, products in demo.shopify_products().items():
        db.save_domain(conn, d, registered_at=None, first_cert_at=None, is_shopify=True,
                       product_count=len(products), products=products, checked_on=AS_OF)
    cfg.link.min_ad_score = 0
    cfg.link.fetch_pages = False  # không gọi mạng trong test
    return cfg, conn


class CountingProvider(demo.DemoTrendsProvider):
    def __init__(self):
        super().__init__(AS_OF)
        self.calls = []

    def interest_over_time(self, keywords, geo, timeframe):
        self.calls.append((tuple(keywords), geo))
        return super().interest_over_time(keywords, geo, timeframe)


def test_link_demo_end_to_end():
    cfg, conn = _demo()
    cfg.trends.geos = ["US"]
    p = CountingProvider()
    linked = link_stores(conn, cfg, analyze(conn, cfg, AS_OF), p, AS_OF)
    by = {x.domain or x.key: x for x in linked}
    assert by["furrygroom.com"].verdict == "strong"
    assert by["postureglow.com"].verdict == "strong"
    assert by["starrynight.shop"].verdict == "risky"
    assert by["backbuddy.store"].verdict == "saturated"
    assert by["petlint.io"].product == "heated dog vest"
    no_link = next(x for x in linked if x.domain is None)
    assert no_link.verdict == "ad_driven"
    # 2 store cùng bán posture corrector → chỉ 1 lần gọi Trends cho sản phẩm đó
    assert sum(1 for kws, _ in p.calls if kws[0] == "posture corrector") == 1
    assert by["postureglow.com"].competitors >= 1
    assert linked[0].verdict == "strong"


def test_link_overrides_and_only_scaling():
    cfg, conn = _demo()
    cfg.trends.geos = ["US"]
    cfg.link.only_scaling = True
    cfg.link.overrides = {"postureglow.com": ["posture brace"]}
    linked = link_stores(conn, cfg, analyze(conn, cfg, AS_OF), CountingProvider(), AS_OF)
    assert all(x.scaling for x in linked)
    pg = next(x for x in linked if x.domain == "postureglow.com")
    assert (pg.product, pg.keyword_source) == ("posture brace", "config")


def test_render_html():
    cfg, conn = _demo()
    cfg.trends.geos = ["US"]
    linked = link_stores(conn, cfg, analyze(conn, cfg, AS_OF), CountingProvider(), AS_OF)
    html = render(linked, AS_OF, "demo")
    assert "Ứng viên mạnh" in html and "heated dog vest" in html
