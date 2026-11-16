from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Search:
    name: str
    keywords: list[str]
    languages: list[str] = field(default_factory=list)


@dataclass
class TrendTopic:
    name: str
    seeds: list[str]
    keywords: list[str] = field(default_factory=list)      # thêm tay, ngoài autocomplete
    volume_hint: int | None = None                           # search volume/tháng của seeds[0] (Keyword Planner)
    volume_hint_geo: str = "US"
    geo_keywords: dict[str, list[str]] = field(default_factory=dict)  # keyword bản địa theo nước


@dataclass
class TrendsConfig:
    provider: str = "serpapi"
    csv_dir: Path = Path("data/trends_csv")
    timeframe: str = "today 5-y"
    geos: list[str] = field(default_factory=lambda: ["US"])
    autocomplete: bool = True
    alphabet: bool = False
    max_keywords: int = 12
    max_keywords_other_geos: int = 5
    cache_days: int = 6
    sleep_seconds: float = 1.0
    topics: list[TrendTopic] = field(default_factory=list)


@dataclass
class LinkConfig:
    top_n: int = 20                 # số store (theo điểm ad) được nối với Trends
    only_scaling: bool = False      # true: chỉ store có nhãn SCALE
    min_ad_score: float = 50.0
    max_keywords: int = 4           # keyword sản phẩm tối đa / store
    autocomplete: bool = False      # mở rộng keyword sản phẩm bằng Autocomplete (tốn request)
    overrides: dict[str, list[str]] = field(default_factory=dict)  # domain → keyword đặt tay


@dataclass
class Config:
    database: Path
    report_dir: Path
    api_version: str
    countries: list[str]
    lookback_days: int
    max_pages_per_query: int
    request_sleep_seconds: float
    searches: list[Search]
    exclude_domains: set[str]
    velocity_weeks: int
    min_total_ads: int
    min_active_weeks: int
    min_growth_ratio: float
    new_store_days: int
    enrich_top_n: int
    enrich_sleep_seconds: float
    weights: dict[str, float]
    trends: TrendsConfig = field(default_factory=TrendsConfig)
    link: LinkConfig = field(default_factory=LinkConfig)


DEFAULT_WEIGHTS = {"velocity": 0.35, "consistency": 0.15, "reach": 0.2, "newness": 0.15, "spread": 0.15}


def load_config(path: str | Path) -> Config:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    al = raw.get("ad_library", {})
    vel = raw.get("velocity", {})
    enrich = raw.get("enrich", {})
    searches = [
        Search(name=s["name"], keywords=list(s["keywords"]), languages=list(s.get("languages") or []))
        for s in raw.get("searches", [])
    ]
    return Config(
        database=base / raw.get("database", "data/winfinder.db"),
        report_dir=base / raw.get("report_dir", "reports"),
        api_version=al.get("api_version", "v23.0"),
        countries=[c.upper() for c in al.get("countries", ["IE"])],
        lookback_days=int(al.get("lookback_days", 70)),
        max_pages_per_query=int(al.get("max_pages_per_query", 20)),
        request_sleep_seconds=float(al.get("request_sleep_seconds", 1.0)),
        searches=searches,
        exclude_domains={d.lower() for d in raw.get("exclude_domains", [])},
        velocity_weeks=int(vel.get("weeks", 6)),
        min_total_ads=int(vel.get("min_total_ads", 5)),
        min_active_weeks=int(vel.get("min_active_weeks", 3)),
        min_growth_ratio=float(vel.get("min_growth_ratio", 1.5)),
        new_store_days=int(raw.get("new_store_days", 90)),
        enrich_top_n=int(enrich.get("top_n", 30)),
        enrich_sleep_seconds=float(enrich.get("request_sleep_seconds", 1.0)),
        weights={**DEFAULT_WEIGHTS, **(raw.get("weights") or {})},
        trends=_load_trends(raw.get("trends") or {}, base, searches),
        link=_load_link(raw.get("link") or {}),
    )


def _load_link(x: dict) -> LinkConfig:
    return LinkConfig(
        top_n=int(x.get("top_n", 20)),
        only_scaling=bool(x.get("only_scaling", False)),
        min_ad_score=float(x.get("min_ad_score", 50)),
        max_keywords=int(x.get("max_keywords", 4)),
        autocomplete=bool(x.get("autocomplete", False)),
        overrides={str(k).lower(): [str(v) for v in (vs if isinstance(vs, list) else [vs])]
                   for k, vs in (x.get("overrides") or {}).items()},
    )


def _load_trends(t: dict, base: Path, searches: list[Search]) -> TrendsConfig:
    topics = [
        TrendTopic(
            name=x["name"],
            seeds=list(x.get("seeds") or x.get("keywords", [])[:1]),
            keywords=list(x.get("keywords") or []),
            volume_hint=x.get("volume_hint"),
            volume_hint_geo=str(x.get("volume_hint_geo", "US")).upper(),
            geo_keywords={k.upper(): list(v) for k, v in (x.get("geo_keywords") or {}).items()},
        )
        for x in t.get("topics") or []
    ]
    if not topics:  # mặc định: mỗi search của Ad Library là một chủ đề trends
        topics = [TrendTopic(name=s.name, seeds=s.keywords) for s in searches]
    exp = t.get("expand") or {}
    return TrendsConfig(
        provider=t.get("provider", "serpapi"),
        csv_dir=base / t.get("csv_dir", "data/trends_csv"),
        timeframe=t.get("timeframe", "today 5-y"),
        geos=[g.upper() for g in t.get("geos", ["US"])],
        autocomplete=bool(exp.get("autocomplete", True)),
        alphabet=bool(exp.get("alphabet", False)),
        max_keywords=int(exp.get("max_keywords", 12)),
        max_keywords_other_geos=int(exp.get("max_keywords_other_geos", 5)),
        cache_days=int(t.get("cache_days", 6)),
        sleep_seconds=float(t.get("request_sleep_seconds", 1.0)),
        topics=topics,
    )
