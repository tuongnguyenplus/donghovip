"""Nối Ad Library ↔ Trends: store đang scale → keyword sản phẩm → nhu cầu thị trường → kết luận.

Các store bán cùng một sản phẩm (cùng keyword neo) dùng chung một lần gọi Trends.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import date

from winfinder.analysis import keywords as kw
from winfinder.analysis.scoring import StoreResult
from winfinder.config import Config, TrendTopic
from winfinder.sources.trends import TrendsProvider
from winfinder.trends_pipeline import TopicResult, run_trends

log = logging.getLogger(__name__)

NEUTRAL_TREND_SCORE = 40.0  # khi không có dữ liệu Trends (sản phẩm ít người tìm)

VERDICTS = {
    "strong": "Ứng viên mạnh — store đang scale và nhu cầu thị trường đang lên",
    "watch": "Nhu cầu đang lên — store chưa scale rõ, theo dõi thêm",
    "seasonal_now": "Mùa vụ, đang vào mùa — cơ hội ngắn hạn, chuẩn bị thoát trước khi hết mùa",
    "seasonal_off": "Mùa vụ, ngoài mùa — chưa phải lúc vào",
    "ad_driven": "Ít người tìm kiếm — nhu cầu do ads tạo ra (sản phẩm impulse); dựa vào ad growth và angle",
    "saturated": "Nhu cầu ổn định / bão hòa — chỉ vào nếu có angle khác biệt",
    "risky": "Rủi ro — nhu cầu đang giảm hoặc đã qua đỉnh",
    "no_keyword": "Không rút được keyword sản phẩm — cần xem tay",
}
VERDICT_ORDER = ["strong", "seasonal_now", "watch", "ad_driven", "saturated", "seasonal_off", "risky", "no_keyword"]


@dataclass
class LinkedStore:
    key: str
    domain: str | None
    page_names: list[str]
    scaling: bool
    ad_score: float
    ad_history: list[int]
    countries: list[str]
    product: str | None
    keywords: list[str]
    keyword_source: str
    competitors: int                    # số store khác có ad nhắc tới keyword sản phẩm
    trend: dict | None                  # stats tổng hợp ở thị trường gốc
    trend_score: float | None
    trend_series: list[float] = field(default_factory=list)  # khử mùa vụ, 2 năm gần nhất
    spread: dict = field(default_factory=dict)
    verdict: str = "no_keyword"
    verdict_text: str = VERDICTS["no_keyword"]
    win_score: float = 0.0


def verdict(scaling: bool, trend: dict | None) -> str:
    if trend is None or trend["label"] == "insufficient":
        return "ad_driven"
    label = trend["label"]
    if label in ("rising", "breakout"):
        return "strong" if scaling else "watch"
    if label == "seasonal":
        return "seasonal_now" if trend.get("in_season") else "seasonal_off"
    if label in ("declining", "spike"):
        return "risky"
    return "saturated"


def win_score(ad_score: float, trend_score: float | None, v: str) -> float:
    ts = NEUTRAL_TREND_SCORE if trend_score is None or v == "ad_driven" else trend_score
    score = 0.55 * ad_score + 0.45 * ts
    if v == "risky":
        score *= 0.6
    elif v == "seasonal_off":
        score *= 0.8
    return round(score, 1)


def store_texts(conn: sqlite3.Connection, s: StoreResult) -> tuple[list[str], list[str], list[str], list[str]]:
    """(tiêu đề ad, nội dung ad, tên sản phẩm Shopify, keyword ngách) của một store."""
    if s.domain:
        rows = conn.execute("SELECT ad_id, titles, bodies FROM ads WHERE domain = ?", (s.domain,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT ad_id, titles, bodies FROM ads WHERE domain IS NULL AND page_id = ?", (s.key.split(":", 1)[1],)
        ).fetchall()
    titles, bodies = [], []
    for r in rows:
        titles += json.loads(r["titles"] or "[]")
        bodies += json.loads(r["bodies"] or "[]")
    products = []
    if s.domain:
        d = conn.execute("SELECT products FROM domains WHERE domain = ?", (s.domain,)).fetchone()
        if d and d["products"]:
            products = [p.get("title") or "" for p in json.loads(d["products"])]
    ids = [r["ad_id"] for r in rows]
    ctx: list[str] = []
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        ctx += [
            r[0] for r in conn.execute(
                f"SELECT DISTINCT keyword FROM ad_searches WHERE ad_id IN ({','.join('?' * len(chunk))})", chunk
            )
        ]
    return list(dict.fromkeys(titles)), list(dict.fromkeys(bodies)), products, sorted(set(ctx))


def count_competitors(conn: sqlite3.Connection, phrase: str, exclude_key: str) -> int:
    like = f"%{phrase.lower()}%"
    n = 0
    for r in conn.execute(
        "SELECT DISTINCT COALESCE(domain, 'page:' || page_id) AS k FROM ads "
        "WHERE lower(titles) LIKE ? OR lower(bodies) LIKE ?",
        (like, like),
    ):
        if r["k"] != exclude_key:
            n += 1
    return n


def select_stores(stores: list[StoreResult], cfg: Config) -> list[StoreResult]:
    lc = cfg.link
    picked = [s for s in stores if (s.scaling or not lc.only_scaling) and s.score >= lc.min_ad_score]
    return picked[: lc.top_n]


def link_stores(
    conn: sqlite3.Connection, cfg: Config, stores: list[StoreResult], provider: TrendsProvider, today: date,
) -> list[LinkedStore]:
    lc = cfg.link
    chosen = select_stores(stores, cfg)

    extracted: dict[str, kw.ProductKeywords] = {}
    for s in chosen:
        override = lc.overrides.get(s.domain or "") or lc.overrides.get(s.key)
        if override:
            extracted[s.key] = kw.ProductKeywords(override[0], list(override), "config", [])
            continue
        titles, bodies, products, ctx = store_texts(conn, s)
        extracted[s.key] = kw.extract(
            titles, bodies, products, domain=s.domain, page_names=s.page_names,
            context_keywords=ctx, max_keywords=lc.max_keywords,
        )

    # một chủ đề Trends cho mỗi keyword neo; gộp keyword phụ của mọi store cùng sản phẩm
    topics: dict[str, TrendTopic] = {}
    for pk in extracted.values():
        if not pk.seed:
            continue
        t = topics.setdefault(pk.seed, TrendTopic(name=f"product:{pk.seed}", seeds=[pk.seed]))
        t.keywords = list(dict.fromkeys([*t.keywords, *(k for k in pk.keywords if k != pk.seed)]))

    trend_cfg = replace(cfg, trends=replace(cfg.trends, autocomplete=lc.autocomplete, max_keywords=lc.max_keywords + 2))
    results: dict[str, TopicResult] = {}
    if topics:
        for tr in run_trends(conn, trend_cfg, provider, today, topics=list(topics.values())):
            results[tr.name.split(":", 1)[1]] = tr

    linked = []
    for s in chosen:
        pk = extracted[s.key]
        tr = results.get(pk.seed or "")
        home = tr.home if tr else None
        trend = home.stats if home else None
        v = verdict(s.scaling, trend) if pk.seed else "no_keyword"
        t_score = home.score if home else None
        linked.append(
            LinkedStore(
                key=s.key,
                domain=s.domain,
                page_names=s.page_names,
                scaling=s.scaling,
                ad_score=s.score,
                ad_history=s.history,
                countries=s.countries,
                product=pk.seed,
                keywords=pk.keywords,
                keyword_source=pk.source,
                competitors=count_competitors(conn, pk.seed, s.key) if pk.seed else 0,
                trend=trend,
                trend_score=t_score,
                trend_series=(home.deseasonalized[-104:] if home else []),
                spread=tr.spread if tr else {},
                verdict=v,
                verdict_text=VERDICTS[v],
                win_score=win_score(s.score, t_score, v) if pk.seed else 0.0,
            )
        )
    linked.sort(key=lambda x: (VERDICT_ORDER.index(x.verdict) if x.verdict == "strong" else 1, -x.win_score))
    return linked


def to_dicts(linked: list[LinkedStore]) -> list[dict]:
    return [asdict(x) for x in linked]
