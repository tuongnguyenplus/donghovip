"""Pipeline Trends: mở rộng keyword → lấy chuỗi theo từng thị trường (có cache) → phân tích chủ đề."""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from winfinder import db
from winfinder.analysis.trends import Series, TrendStats, analyze_series, sum_series, trend_score
from winfinder.config import Config, TrendTopic
from winfinder.http import make_session
from winfinder.sources.trends import (
    CsvTrends,
    DirectTrends,
    SerpApiTrends,
    TrendsError,
    TrendsProvider,
    expand_keywords,
    fetch_keyword_set,
)

log = logging.getLogger(__name__)

RISING_LABELS = {"rising", "breakout"}


def make_provider(cfg: Config) -> TrendsProvider:
    t = cfg.trends
    if t.provider == "serpapi":
        return SerpApiTrends(os.environ.get("SERPAPI_API_KEY", ""), sleep_seconds=t.sleep_seconds)
    if t.provider == "direct":
        return DirectTrends(sleep_seconds=max(t.sleep_seconds, 3.0))
    if t.provider == "csv":
        return CsvTrends(t.csv_dir)
    raise TrendsError(f"Provider trends không hợp lệ: {t.provider} (serpapi | direct | csv)")


@dataclass
class KeywordResult:
    keyword: str
    stats: dict
    score: float
    share: float                 # tỉ trọng trong bộ keyword (12 tháng gần nhất)
    est_monthly_volume: int | None


@dataclass
class GeoResult:
    geo: str
    stats: dict
    score: float
    weeks: list[str]
    values: list[float]
    deseasonalized: list[float]
    keywords: list[KeywordResult]
    est_monthly_volume: int | None = None


@dataclass
class TopicResult:
    name: str
    keywords: list[str]
    geos: list[GeoResult]
    spread: dict = field(default_factory=dict)

    @property
    def home(self) -> GeoResult | None:
        return self.geos[0] if self.geos else None


def _last_mean(s: Series, n: int = 52) -> float:
    vals = [v for _, v in s[-n:]]
    return sum(vals) / len(vals) if vals else 0.0


def topic_keywords(conn: sqlite3.Connection, cfg: Config, topic: TrendTopic, today: date) -> list[str]:
    t = cfg.trends
    base = list(dict.fromkeys([*(s.lower() for s in topic.seeds), *(k.lower() for k in topic.keywords)]))
    if not t.autocomplete:  # không gọi mạng → không cần cache, luôn theo keyword hiện tại
        return base[: t.max_keywords]
    cached, fetched = db.load_keyword_set(conn, topic.name)
    if cached and fetched and (today - fetched).days < 30:
        return cached
    if t.autocomplete:
        try:
            kws = expand_keywords(make_session(), base, max_keywords=t.max_keywords, alphabet=t.alphabet)
        except Exception as exc:
            log.warning("Mở rộng keyword '%s' lỗi: %s", topic.name, exc)
            kws = base[: t.max_keywords]
    else:
        kws = base[: t.max_keywords]
    db.save_keyword_set(conn, topic.name, kws, today)
    return kws


def fetch_topic_geo(
    conn: sqlite3.Connection, cfg: Config, provider: TrendsProvider, topic: str,
    keywords: list[str], geo: str, today: date, *, force: bool = False,
) -> dict[str, Series]:
    t = cfg.trends
    cached, fetched = db.load_trend_series(conn, topic, geo, t.timeframe)
    if (
        not force and fetched and (today - fetched).days < t.cache_days
        and set(keywords) <= set(cached)
    ):
        return cached
    series = fetch_keyword_set(provider, keywords, geo, t.timeframe)
    if series:
        db.save_trend_series(conn, topic, geo, t.timeframe, series, today)
    return series


def analyze_topic_geo(
    geo: str, series: dict[str, Series], keyword_order: list[str],
    volume_hint: int | None = None, volume_anchor: str | None = None,
) -> GeoResult | None:
    present = [k for k in keyword_order if series.get(k)]
    if not present:
        return None
    total = sum_series([series[k] for k in present])
    st: TrendStats = analyze_series(total)
    means = {k: _last_mean(series[k]) for k in present}
    total_mean = sum(means.values()) or 1.0
    anchor_mean = means.get(volume_anchor or "", 0.0)

    def vol(m: float) -> int | None:
        return int(round(volume_hint * m / anchor_mean)) if volume_hint and anchor_mean else None

    kw_results = []
    for k in present:
        ks = analyze_series(series[k])
        kw_results.append(
            KeywordResult(k, ks.to_dict(), trend_score(ks), round(means[k] / total_mean, 3), vol(means[k]))
        )
    kw_results.sort(key=lambda r: r.share, reverse=True)
    return GeoResult(
        geo=geo,
        stats=st.to_dict(),
        score=trend_score(st),
        weeks=[d.isoformat() for d, _ in total],
        values=[round(v, 2) for _, v in total],
        deseasonalized=st.deseasonalized,
        keywords=kw_results,
        est_monthly_volume=vol(total_mean) if volume_hint else None,
    )


def spread_summary(geos: list[GeoResult]) -> dict:
    """Thị trường đầu tiên (thường US) là 'home'; xem các thị trường khác đã tăng theo chưa."""
    if not geos:
        return {}
    home, others = geos[0], geos[1:]
    rising_others = [g.geo for g in others if g.stats["label"] in RISING_LABELS]
    home_rising = home.stats["label"] in RISING_LABELS
    if home_rising and not rising_others:
        stage = "Mới tăng ở thị trường gốc — chưa lan sang nước khác (cơ hội đi trước)"
    elif home_rising and rising_others:
        stage = "Đang lan rộng sang: " + ", ".join(rising_others)
    elif not home_rising and rising_others:
        stage = "Thị trường gốc chững, đang tăng ở: " + ", ".join(rising_others)
    else:
        stage = "Chưa có dấu hiệu tăng ở thị trường nào"
    return {"home": home.geo, "home_rising": home_rising, "rising_elsewhere": rising_others, "stage": stage}


def run_trends(
    conn: sqlite3.Connection, cfg: Config, provider: TrendsProvider, today: date,
    *, topics: list[TrendTopic] | None = None, force: bool = False,
) -> list[TopicResult]:
    t = cfg.trends
    results = []
    for topic in topics or t.topics:
        kws = topic_keywords(conn, cfg, topic, today)
        geo_results = []
        for i, geo in enumerate(t.geos):
            geo_kws = topic.geo_keywords.get(geo) or (kws if i == 0 else kws[: t.max_keywords_other_geos])
            try:
                series = fetch_topic_geo(conn, cfg, provider, topic.name, geo_kws, geo, today, force=force)
            except Exception as exc:
                log.error("Trends '%s' @ %s: %s", topic.name, geo, exc)
                continue
            hint = topic.volume_hint if geo == topic.volume_hint_geo else None
            gr = analyze_topic_geo(geo, series, geo_kws, hint, geo_kws[0] if geo_kws else None)
            if gr:
                geo_results.append(gr)
                log.info("[%s] %s: %s (điểm %.0f)", topic.name, geo, gr.stats["label_vi"], gr.score)
        results.append(TopicResult(topic.name, kws, geo_results, spread_summary(geo_results)))
    results.sort(key=lambda r: r.home.score if r.home else -1, reverse=True)
    return results
