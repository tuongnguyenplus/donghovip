"""Gom ad theo store và chấm điểm 0–100."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date

from winfinder.analysis.velocity import Growth, growth, is_scaling, weekly_counts
from winfinder.config import Config

HISTORY_WEEKS = 12  # số tuần hiển thị trên sparkline


@dataclass
class StoreResult:
    key: str                     # domain, hoặc "page:<id>" nếu ad không có domain
    domain: str | None
    page_names: list[str]
    searches: list[str]
    countries: list[str]
    ad_count: int
    live_ads: int
    eu_reach: int
    first_ad_date: str | None
    age_days: int | None
    age_source: str | None       # rdap | crt.sh | first_ad
    history: list[int]           # creative mới / tuần, HISTORY_WEEKS tuần
    window: dict                 # chỉ số Growth trong cửa sổ velocity
    scaling: bool
    components: dict[str, float]
    score: float
    shopify: dict | None = None
    sample_ads: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _newness(age_days: int | None, new_store_days: int) -> float:
    """1.0 nếu ≤ new_store_days, giảm tuyến tính về 0 ở 2× new_store_days."""
    if age_days is None:
        return 0.5
    if age_days <= new_store_days:
        return 1.0
    return _clamp(1 - (age_days - new_store_days) / new_store_days)


def components(g: Growth, eu_reach: int, n_countries: int, age_days: int | None, cfg: Config) -> dict[str, float]:
    return {
        "velocity": _clamp(g.slope_norm / 0.25) if g.slope > 0 else 0.0,
        "consistency": g.active_weeks / len(g.counts) if g.counts else 0.0,
        "reach": _clamp(math.log10(eu_reach + 1) / 6),  # 1M reach → 1.0
        "newness": _newness(age_days, cfg.new_store_days),
        "spread": _clamp(n_countries / 3),
    }


def weighted_score(comps: dict[str, float], weights: dict[str, float]) -> float:
    total_w = sum(weights.get(k, 0) for k in comps)
    if not total_w:
        return 0.0
    return round(100 * sum(v * weights.get(k, 0) for k, v in comps.items()) / total_w, 1)


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def analyze(conn: sqlite3.Connection, cfg: Config, as_of: date, search: str | None = None) -> list[StoreResult]:
    sql = "SELECT a.* FROM ads a"
    params: tuple = ()
    if search:
        sql += " WHERE a.ad_id IN (SELECT ad_id FROM ad_searches WHERE search = ?)"
        params = (search,)
    stores: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(sql, params):
        stores[row["domain"] or f"page:{row['page_id']}"].append(row)

    countries_by_ad: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT ad_id, country FROM ad_countries"):
        countries_by_ad[r["ad_id"]].add(r["country"])
    searches_by_ad: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT ad_id, search FROM ad_searches"):
        searches_by_ad[r["ad_id"]].add(r["search"])
    domains = {r["domain"]: r for r in conn.execute("SELECT * FROM domains")}

    results = []
    for key, rows in stores.items():
        starts = [d for d in (_d(r["start_date"]) for r in rows) if d]
        if not starts:
            continue
        domain = rows[0]["domain"]
        g = growth(weekly_counts(starts, as_of, cfg.velocity_weeks))
        history = weekly_counts(starts, as_of, HISTORY_WEEKS)
        countries = sorted(set().union(*(countries_by_ad[r["ad_id"]] for r in rows)))
        eu_reach = sum(r["eu_total_reach"] or 0 for r in rows)
        first_ad = min(starts)

        dom = domains.get(domain) if domain else None
        age_source, born = "first_ad", first_ad
        if dom is not None and dom["registered_at"]:
            age_source, born = "rdap", date.fromisoformat(dom["registered_at"])
        elif dom is not None and dom["first_cert_at"]:
            age_source, born = "crt.sh", date.fromisoformat(dom["first_cert_at"])
        age_days = (as_of - born).days

        comps = components(g, eu_reach, len(countries), age_days, cfg)
        top = sorted(rows, key=lambda r: r["eu_total_reach"] or 0, reverse=True)[:3]
        results.append(
            StoreResult(
                key=key,
                domain=domain,
                page_names=sorted({r["page_name"] for r in rows if r["page_name"]}),
                searches=sorted(set().union(*(searches_by_ad[r["ad_id"]] for r in rows))),
                countries=countries,
                ad_count=len(rows),
                live_ads=sum(1 for r in rows if not r["stop_date"]),
                eu_reach=eu_reach,
                first_ad_date=first_ad.isoformat(),
                age_days=age_days,
                age_source=age_source,
                history=history,
                window={
                    "counts": g.counts,
                    "active_weeks": g.active_weeks,
                    "slope": round(g.slope, 2),
                    "growth_ratio": round(g.growth_ratio, 2),
                },
                scaling=is_scaling(
                    g,
                    min_total_ads=cfg.min_total_ads,
                    min_active_weeks=cfg.min_active_weeks,
                    min_growth_ratio=cfg.min_growth_ratio,
                ),
                components={k: round(v, 3) for k, v in comps.items()},
                score=weighted_score(comps, cfg.weights),
                shopify=(
                    {"product_count": dom["product_count"], "products": json.loads(dom["products"] or "[]")[:5]}
                    if dom is not None and dom["is_shopify"]
                    else None
                ),
                sample_ads=[
                    {
                        "title": (json.loads(r["titles"] or "[]") or [""])[0],
                        "body": ((json.loads(r["bodies"] or "[]") or [""])[0])[:280],
                        "start_date": r["start_date"],
                        "eu_total_reach": r["eu_total_reach"],
                        "snapshot_url": r["snapshot_url"],
                    }
                    for r in top
                ],
            )
        )
    # Store đang scale lên trước, sau đó theo điểm
    results.sort(key=lambda s: (s.scaling, s.score), reverse=True)
    return results
