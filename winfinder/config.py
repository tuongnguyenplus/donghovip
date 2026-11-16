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
    )
