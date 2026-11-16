from datetime import date, timedelta
from pathlib import Path

import pytest

from winfinder import db, demo
from winfinder.analysis.durability import AdLife, DurabilityRules, active_weekly, creative_key, measure
from winfinder.analysis.scoring import analyze
from winfinder.config import load_config
from winfinder.sources.adlibrary import parse_ad

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 16)


def ad(days_ago: int, lived: int | None = None, creative: str = "c"):
    start = AS_OF - timedelta(days=days_ago)
    stop = start + timedelta(days=lived) if lived is not None else None
    return AdLife(start, stop, creative)


def test_active_weekly_counts_alive_ads():
    ads = [ad(70), ad(40, 10), ad(5)]
    assert active_weekly(ads, AS_OF, 3) == [1, 1, 2]


def test_survival_only_counts_ads_old_enough():
    # 2 ad đủ tuổi (1 sống, 1 chết), 5 ad mới không được kéo tỉ lệ xuống
    ads = [ad(50), ad(45, 10)] + [ad(5) for _ in range(5)]
    m = measure(ads, AS_OF)
    assert m.aged_ads == 2 and m.survival_30 == 0.5
    assert m.alive_30 == 1 and m.oldest_active_days == 50


def test_scaling_requires_growth_and_durability():
    ads = [ad(80 - 6 * i) for i in range(5)] + [ad(d) for d in (12, 10, 8, 6, 5, 4, 3, 2, 1, 1)]
    m = measure(ads, AS_OF)
    assert m.archetype == "scaling" and m.active_growth >= 1.5


def test_treadmill_many_launches_short_lives():
    ads = [ad(d, 7, creative=f"c{d}") for d in range(80, 0, -2)]
    m = measure(ads, AS_OF)
    assert m.archetype == "treadmill" and m.survival_30 == 0 and m.turnover > 1
    assert m.dup_share == 0  # creative mới liên tục, không nhân bản


def test_evergreen_few_launches_long_lives():
    ads = [ad(85), ad(80), ad(75)] + [ad(d, 6) for d in (60, 40, 20, 9)]
    assert measure(ads, AS_OF).archetype == "evergreen"


def test_early_vs_testing_for_young_stores():
    alive = [ad(d) for d in (25, 20, 18, 10, 6, 3, 2, 1)]
    dying = [ad(d, 5) for d in (25, 22, 20, 18, 16)] + [ad(3), ad(2)]
    assert measure(alive, AS_OF).archetype == "early"
    assert measure(dying, AS_OF).archetype == "testing"


def test_stopped_and_declining():
    assert measure([ad(60, 10), ad(50, 8)], AS_OF).archetype == "stopped"
    old = [ad(d, 40) for d in (75, 72, 70, 68, 66, 64)] + [ad(80)]
    assert measure(old, AS_OF).archetype == "declining"


def test_duplication_needs_relaunch_on_different_day():
    same_day = [ad(10, creative="x"), ad(10, creative="x")]
    relaunched = [ad(20, creative="x"), ad(5, creative="x"), ad(3, creative="y")]
    assert measure(same_day, AS_OF).duplicated_creatives == 0
    m = measure(relaunched, AS_OF)
    assert m.duplicated_creatives == 1 and m.dup_share == pytest.approx(2 / 3, abs=0.01)


def test_creative_key_ignores_numbers_and_punctuation():
    assert creative_key("50% OFF!", "Only 3 left") == creative_key("70% off", "only 9 left.")


def test_rules_are_configurable():
    ads = [ad(25), ad(24), ad(23), ad(2), ad(1)]
    assert measure(ads, AS_OF).aged_ads == 0
    assert measure(ads, AS_OF, rules=DurabilityRules(profit_days=20)).aged_ads == 3


def test_demo_store_archetypes():
    cfg = load_config(ROOT / "config.yaml")
    conn = db.connect(":memory:")
    for search, country, raw in demo.generate(AS_OF):
        db.upsert_ads(conn, [parse_ad(raw, cfg.exclude_domains)], country=country, search=search,
                      keyword="k", observed_on=AS_OF)
    arch = {s.key: s.archetype for s in analyze(conn, cfg, AS_OF)}
    assert arch["postureglow.com"] == "scaling" and arch["lifteyes.co"] == "scaling"
    assert arch["furrygroom.com"] == "early"
    assert arch["spinealign.co"] == "treadmill"   # ra creative đều tăng nhưng chết nhanh → không được điểm cao
    assert arch["backbuddy.store"] == "evergreen"
    assert arch["starrynight.shop"] == "stopped"
    scores = {s.key: s.score for s in analyze(conn, cfg, AS_OF)}
    assert scores["postureglow.com"] > scores["backbuddy.store"] > scores["spinealign.co"]
