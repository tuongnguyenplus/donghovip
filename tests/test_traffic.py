from datetime import date, timedelta
from pathlib import Path

import pytest

from winfinder import db, demo, link
from winfinder.analysis.scoring import analyze
from winfinder.analysis.traffic import assess, brand_term, weekly_by_date
from winfinder.config import load_config
from winfinder.sources.adlibrary import parse_ad

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 16)
RISING_ADS = [0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 6, 8]
FLAT_ADS = [3] * 12


def weeks(values, end=AS_OF):
    n = len(values)
    return [(end - timedelta(weeks=n - 1 - i), float(v)) for i, v in enumerate(values)]


@pytest.mark.parametrize("domain,pages,term", [
    ("postureglow.com", ["PostureGlow"], "postureglow"),
    (None, ["Groomy Paws"], "groomy paws"),
    ("lift-eyes.co", ["Lift Eyes™"], "lift eyes"),
    ("shop.example.com", ["Totally Different Name"], "example"),
    (None, [], None),
])
def test_brand_term(domain, pages, term):
    assert brand_term(domain, pages) == term


def test_weekly_by_date_aligns_with_ad_axis():
    pts = weeks([1, 2, 3])
    assert weekly_by_date(pts, AS_OF, 4) == [0.0, 1.0, 2.0, 3.0]


def test_confirmed_when_both_rise():
    brand = weeks([0] * 46 + [5, 10, 20, 30, 40, 50])
    sig = assess("x", brand, RISING_ADS, AS_OF - timedelta(days=60), AS_OF)
    assert sig.verdict == "confirmed" and sig.factor > 1 and sig.correlation > 0.5


def test_unconfirmed_when_only_ads_rise():
    brand = weeks([0] * 40 + [20] * 12)
    assert assess("x", brand, RISING_ADS, AS_OF - timedelta(days=60), AS_OF).verdict == "unconfirmed"


def test_organic_and_declining():
    up = weeks([0] * 40 + [10] * 6 + [30] * 6)
    down = weeks([0] * 40 + [40] * 6 + [10] * 6)
    born = AS_OF - timedelta(days=90)
    assert assess("x", up, FLAT_ADS, born, AS_OF).verdict == "organic"
    assert assess("x", down, FLAT_ADS, born, AS_OF).verdict == "declining"


def test_too_small_and_no_data():
    assert assess("x", weeks([0, 1, 2] * 17), RISING_ADS, None, AS_OF).verdict == "too_small"
    assert assess("x", None, RISING_ADS, None, AS_OF).verdict == "no_data"
    assert assess(None, weeks([50] * 52), RISING_ADS, None, AS_OF).verdict == "no_data"


def test_ambiguous_common_word_had_searches_before_store_existed():
    brand = weeks([40] * 52)
    sig = assess("glow", brand, RISING_ADS, AS_OF - timedelta(days=40), AS_OF)
    assert sig.verdict == "ambiguous" and sig.factor == 1.0


def test_demo_traffic_end_to_end():
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
    v = {x.domain or x.key: x.traffic["verdict"] for x in linked}
    assert v["postureglow.com"] == "confirmed"
    assert v["spinealign.co"] == "unconfirmed"
    assert v["starrynight.shop"] == "ambiguous"
    assert v["furrygroom.com"] == "too_small"
    pg = next(x for x in linked if x.domain == "postureglow.com")
    assert sum(pg.traffic["reach_weeks"]) > 0 and len(pg.traffic["reach_weeks"]) == len(pg.ad_history)

    cfg.link.traffic = False
    off = link.link_stores(conn, cfg, analyze(conn, cfg, AS_OF), demo.DemoTrendsProvider(AS_OF), AS_OF)
    pg_off = next(x for x in off if x.domain == "postureglow.com")
    assert pg_off.traffic is None and pg_off.win_score < pg.win_score  # confirmed được cộng điểm
