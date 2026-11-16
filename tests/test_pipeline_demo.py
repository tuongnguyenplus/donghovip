from datetime import date
from pathlib import Path

from winfinder import db, demo
from winfinder.analysis.scoring import analyze
from winfinder.config import load_config
from winfinder.report import render_html
from winfinder.sources.adlibrary import parse_ad

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 16)


def _load():
    cfg = load_config(ROOT / "config.yaml")
    conn = db.connect(":memory:")
    for search, country, raw in demo.generate(AS_OF):
        db.upsert_ads(conn, [parse_ad(raw, cfg.exclude_domains)], country=country, search=search,
                      keyword="demo", observed_on=AS_OF)
    return cfg, conn


def test_demo_ranking():
    cfg, conn = _load()
    results = analyze(conn, cfg, AS_OF)
    by_key = {r.key: r for r in results}
    assert by_key["postureglow.com"].scaling
    assert by_key["postureglow.com"].countries == ["DE", "IE", "NL"]
    assert not by_key["backbuddy.store"].scaling       # evergreen, không tăng
    assert not by_key["quickfixposture.shop"].scaling  # test fail
    assert results[0].scaling
    assert results[-1].key == "quickfixposture.shop"


def test_filter_by_search():
    cfg, conn = _load()
    keys = {r.key for r in analyze(conn, cfg, AS_OF, search="pet-grooming")}
    assert "furrygroom.com" in keys and "postureglow.com" not in keys


def test_domain_age_prefers_rdap():
    cfg, conn = _load()
    db.save_domain(conn, "postureglow.com", registered_at="2026-09-01", first_cert_at="2026-09-03",
                   is_shopify=True, product_count=3, products=[{"title": "x"}], checked_on=AS_OF)
    r = next(r for r in analyze(conn, cfg, AS_OF) if r.key == "postureglow.com")
    assert (r.age_source, r.age_days) == ("rdap", 76)
    assert r.shopify["product_count"] == 3


def test_html_renders_and_escapes():
    cfg, conn = _load()
    results = analyze(conn, cfg, AS_OF)
    results[0].page_names = ["<script>x</script>"]
    out = render_html(results, AS_OF)
    assert "<title>WinFinder</title>" in out and "<script>x" not in out
