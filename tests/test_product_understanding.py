import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from winfinder import db, demo
from winfinder.analysis.keywords import extract, singular
from winfinder.analysis.product_llm import SCHEMA, ClaudeProductExtractor, ProductContext
from winfinder.analysis.scoring import analyze
from winfinder.config import load_config
from winfinder.link import link_stores, understand_product
from winfinder.sources.product_page import PageInfo, choose_hero, html_to_text, parse_page
from winfinder.sources.adlibrary import parse_ad

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 16)

HTML = """<html><head><title>LiftEyes™ Pro | LiftEyes</title>
<meta name="description" content="Reduce eye bags in 10 minutes a day.">
<meta property="og:title" content="LiftEyes™ Pro">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Product",
 "name":"LiftEyes Pro","category":"Red Light Therapy Glasses",
 "description":"<p>630nm red light therapy glasses for under eye bags.</p>"}]}</script>
<script>var tracking = "ignore me";</script><style>.x{color:red}</style></head>
<body><h1>LiftEyes™ Pro</h1><h2>Say goodbye to puffy eyes</h2><p>Wear for 10 minutes.</p></body></html>"""


# ------------------------------------------------------------------ đọc trang
def test_parse_page_extracts_jsonld_meta_headings():
    p = parse_page("https://lifteyes.co/products/pro", HTML)
    assert p.title.startswith("LiftEyes")
    assert p.product_name == "LiftEyes Pro"
    assert p.product_type == "Red Light Therapy Glasses"
    assert p.description == "Reduce eye bags in 10 minutes a day."
    assert "Say goodbye to puffy eyes" in p.headings
    assert "red light therapy glasses" in p.text and "ignore me" not in p.text and "color:red" not in p.text


def test_parse_page_survives_garbage():
    assert parse_page("https://x", "<html><<<>>> not really html").url == "https://x"


def test_html_to_text():
    assert html_to_text("<p>A&amp;B</p><script>x()</script><li>C</li>") == "A&B\nC"


def test_choose_hero_matches_brand_in_ads_and_skips_junk():
    products = [{"title": "Gift Card"}, {"title": "Shipping Protection"},
                {"title": "Old Thing", "created_at": "2025-01-01"},
                {"title": "LiftEyes™ Pro", "handle": "lifteyes-pro", "created_at": "2026-09-01"}]
    assert choose_hero(products, ["LiftEyes — wake up younger"])["handle"] == "lifteyes-pro"
    assert choose_hero([{"title": "Gift Card"}], ["x"]) is None


def test_choose_hero_no_guess_on_big_catalog():
    products = [{"title": f"Product {i}"} for i in range(10)]
    assert choose_hero(products, ["completely unrelated ad"]) is None


def test_pageinfo_roundtrip():
    p = PageInfo(url="u", product_type="t", tags=["a"])
    assert PageInfo.from_dict(json.loads(json.dumps(p.to_dict()))) == p


# ------------------------------------------------------------------ heuristic + trang
def test_heuristic_uses_page_when_title_is_brand_only():
    titles = ["LiftEyes™ — wake up looking 10 years younger", "LiftEyes: 10 minutes a day"]
    without = extract(titles, [], domain="lifteyes.co", page_names=["LiftEyes"])
    page = {"product_type": "Red Light Therapy Glasses", "tags": ["eye bags"],
            "text": "Red light therapy glasses that reduce eye bags."}
    with_page = extract(titles, [], domain="lifteyes.co", page_names=["LiftEyes"], page=page)
    assert without.seed is None or "light" not in without.seed
    assert "red light therapy" in with_page.keywords
    assert with_page.source == "page+ads"


def test_surface_form_keeps_plural_search_phrase():
    r = extract(["No more eye bags", "Eye bags gone"], ["fix eye bags fast"])
    assert r.seed == "eye bags"


def test_plural_only_nouns():
    assert singular("glasses") == "glasses" and singular("leggings") == "leggings"


# ------------------------------------------------------------------ Claude (client giả)
class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _fake_client(payload: dict | None, stop_reason="end_turn"):
    content = [SimpleNamespace(type="thinking", thinking=""),
               SimpleNamespace(type="text", text=json.dumps(payload) if payload else "")]
    resp = SimpleNamespace(stop_reason=stop_reason, content=content, model="claude-opus-5-5")
    msgs = FakeMessages(resp)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


CTX = ProductContext("lifteyes.co", ["LiftEyes"], ["LiftEyes — 10 minutes a day"], ["Say goodbye"],
                     PageInfo(url="https://lifteyes.co/products/pro", product_type="Red Light Therapy Glasses").to_dict(),
                     ["eye bags"])


def test_claude_extractor_request_and_parsing():
    payload = {"generic_product": "Red Light Therapy Glasses", "category": "beauty",
               "search_keywords": ["Red light therapy glasses", "red light eye mask", "red light eye mask"],
               "problem_keywords": ["eye bags"], "target_persona": "women 35+", "confidence": "high"}
    client, msgs = _fake_client(payload)
    ins = ClaudeProductExtractor(client=client).extract(CTX)
    assert ins.generic_product == "red light therapy glasses"
    assert ins.search_keywords == ["red light therapy glasses", "red light eye mask"]  # chuẩn hoá + bỏ trùng
    k = msgs.kwargs
    assert k["model"] == "claude-opus-5-5"
    assert k["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert k["output_config"]["effort"] == "low"
    assert k["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in k["betas"]
    assert "Red Light Therapy Glasses" in k["messages"][0]["content"]
    assert "thinking" not in k  # Opus 5.5: không gửi thinking disabled


def test_claude_extractor_refusal_returns_none():
    client, _ = _fake_client(None, stop_reason="refusal")
    assert ClaudeProductExtractor(client=client).extract(CTX) is None


def test_schema_is_strict():
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])


def test_insight_cache_by_fingerprint():
    conn = db.connect(":memory:")
    ex = demo.DemoExtractor()
    understand_product(conn, ex, CTX, "lifteyes.co", AS_OF)
    understand_product(conn, ex, CTX, "lifteyes.co", AS_OF)
    assert ex.calls == 1
    changed = ProductContext(**{**CTX.__dict__, "ad_titles": ["New ad"]})
    understand_product(conn, ex, changed, "lifteyes.co", AS_OF)
    assert ex.calls == 2


# ------------------------------------------------------------------ end-to-end demo
def _demo():
    cfg = load_config(ROOT / "config.yaml")
    cfg.trends.geos = ["US"]
    cfg.link.min_ad_score = 0
    cfg.link.fetch_pages = False  # test nào cần đọc trang thì tự truyền page_fetcher
    conn = db.connect(":memory:")
    for search, country, raw in demo.generate(AS_OF):
        db.upsert_ads(conn, [parse_ad(raw, cfg.exclude_domains)], country=country, search=search,
                      keyword=demo.SEARCH_KEYWORD[search], observed_on=AS_OF)
    for d, products in demo.shopify_products().items():
        db.save_domain(conn, d, registered_at=None, first_cert_at=None, is_shopify=True,
                       product_count=len(products), products=products, checked_on=AS_OF)
    return cfg, conn


def test_brand_only_store_understood_via_claude_and_need_trend():
    cfg, conn = _demo()
    linked = link_stores(conn, cfg, analyze(conn, cfg, AS_OF), demo.DemoTrendsProvider(AS_OF), AS_OF,
                         extractor=demo.DemoExtractor(), page_fetcher=lambda u: None)
    le = next(x for x in linked if x.domain == "lifteyes.co")
    assert le.product == "red light therapy glasses" and le.keyword_source == "claude"
    assert le.page_url == "https://lifteyes.co/products/lifteyes-pro"
    assert le.problem_keywords[0] == "eye bags"
    # tên sản phẩm chưa ai tìm → kết luận dựa trên nhu cầu 'eye bags' đang tăng
    assert le.trend["label"] == "insufficient" and le.need_trend["label"] == "rising"
    assert (le.verdict, le.verdict_basis) == ("strong", "need")


def test_without_llm_falls_back_to_heuristic_with_page():
    cfg, conn = _demo()
    linked = link_stores(conn, cfg, analyze(conn, cfg, AS_OF), demo.DemoTrendsProvider(AS_OF), AS_OF,
                         extractor=None, page_fetcher=lambda u: None)
    le = next(x for x in linked if x.domain == "lifteyes.co")
    assert le.keyword_source == "page+ads" and "light therapy" in le.product


def test_homepage_fetched_and_cached_when_no_shopify():
    cfg, conn = _demo()
    cfg.link.fetch_pages = True
    conn.execute("DELETE FROM domains WHERE domain = 'lifteyes.co'")
    fetched = []

    def fetcher(url):
        fetched.append(url)
        return parse_page(url, HTML)

    stores = analyze(conn, cfg, AS_OF)
    for _ in range(2):
        linked = link_stores(conn, cfg, stores, demo.DemoTrendsProvider(AS_OF), AS_OF, page_fetcher=fetcher)
    assert fetched.count("https://lifteyes.co/") == 1
    le = next(x for x in linked if x.domain == "lifteyes.co")
    assert "light therapy" in le.product
