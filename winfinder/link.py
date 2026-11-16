"""Nối Ad Library ↔ Trends: store đang scale → hiểu sản phẩm → nhu cầu thị trường → kết luận.

Hiểu sản phẩm (theo thứ tự ưu tiên):
  1. config `link.overrides` (đặt tay)
  2. Claude đọc ad + trang sản phẩm → tên gọi chung, keyword tìm kiếm, keyword nhu cầu (cần ANTHROPIC_API_KEY)
  3. heuristic n-gram trên ad + trang sản phẩm (analysis/keywords.py)

Trang sản phẩm: hero product từ Shopify /products.json (mô tả, product_type, tags), không có thì trang chủ.
Các store bán cùng một sản phẩm (cùng keyword neo) dùng chung một lần gọi Trends.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Callable

from winfinder.analysis import keywords as kw
from winfinder.analysis.product_llm import ProductContext, ProductExtractor, ProductInsight
from winfinder.analysis.scoring import StoreResult
from winfinder.config import Config, TrendTopic
from winfinder.http import make_session
from winfinder.sources.product_page import PageInfo, choose_hero, fetch_page, page_from_shopify
from winfinder.sources.trends import TrendsProvider
from winfinder.trends_pipeline import TopicResult, run_trends

log = logging.getLogger(__name__)

NEUTRAL_TREND_SCORE = 40.0  # khi không có dữ liệu Trends (sản phẩm ít người tìm)
USABLE = {"rising", "breakout", "seasonal", "stable", "declining", "spike"}

VERDICTS = {
    "strong": "Ứng viên mạnh — store đang scale và nhu cầu thị trường đang lên",
    "watch": "Nhu cầu đang lên — store chưa scale rõ, theo dõi thêm",
    "seasonal_now": "Mùa vụ, đang vào mùa — cơ hội ngắn hạn, chuẩn bị thoát trước khi hết mùa",
    "seasonal_off": "Mùa vụ, ngoài mùa — chưa phải lúc vào",
    "ad_driven": "Ít người tìm kiếm — nhu cầu do ads tạo ra (sản phẩm impulse); dựa vào ad growth và angle",
    "saturated": "Nhu cầu ổn định / bão hòa — chỉ vào nếu có angle khác biệt",
    "risky": "Rủi ro — nhu cầu đang giảm hoặc đã qua đỉnh",
    "no_keyword": "Không xác định được sản phẩm — cần xem tay",
}
VERDICT_ORDER = ["strong", "seasonal_now", "watch", "ad_driven", "saturated", "seasonal_off", "risky", "no_keyword"]

PageFetcher = Callable[[str], "PageInfo | None"]


@dataclass
class LinkedStore:
    key: str
    domain: str | None
    page_names: list[str]
    scaling: bool
    ad_score: float
    ad_history: list[int]
    countries: list[str]
    product: str | None                 # keyword neo (tìm kiếm)
    keywords: list[str]
    keyword_source: str                 # config | claude | page+ads | shopify+ads | ads
    competitors: int                    # số store khác có ad nhắc tới keyword sản phẩm
    trend: dict | None                  # stats tổng hợp ở thị trường gốc
    trend_score: float | None
    trend_series: list[float] = field(default_factory=list)  # khử mùa vụ, 2 năm gần nhất
    spread: dict = field(default_factory=dict)
    generic_product: str | None = None  # Claude: sản phẩm thực sự là gì
    problem_keywords: list[str] = field(default_factory=list)
    persona: str | None = None
    confidence: str | None = None
    page_url: str | None = None
    need_trend: dict | None = None      # Trends của keyword nhu cầu (eye bags...)
    need_score: float | None = None
    verdict: str = "no_keyword"
    verdict_text: str = VERDICTS["no_keyword"]
    verdict_basis: str = "product"      # product | need — kết luận dựa trên trend nào
    win_score: float = 0.0


def verdict(scaling: bool, trend: dict | None) -> str:
    if trend is None or trend["label"] not in USABLE:
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


# ------------------------------------------------------------------ dữ liệu của store
def store_texts(conn: sqlite3.Connection, s: StoreResult) -> tuple[list[str], list[str], list[dict], list[str]]:
    """(tiêu đề ad, nội dung ad, sản phẩm Shopify, keyword ngách) của một store."""
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
    products: list[dict] = []
    if s.domain:
        d = conn.execute("SELECT products FROM domains WHERE domain = ?", (s.domain,)).fetchone()
        if d and d["products"]:
            products = json.loads(d["products"])
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


def product_page(
    conn: sqlite3.Connection, cfg: Config, s: StoreResult, products: list[dict], ad_texts: list[str],
    fetcher: PageFetcher | None, today: date,
) -> PageInfo | None:
    """Trang sản phẩm hero (cache trong DB `product_pages`)."""
    if not s.domain:
        return None
    hero = choose_hero(products, ad_texts)
    if hero and (hero.get("description") or hero.get("product_type")):
        return page_from_shopify(s.domain, hero)  # đủ thông tin, không cần tải HTML
    if not cfg.link.fetch_pages or fetcher is None:
        return page_from_shopify(s.domain, hero) if hero else None
    url = f"https://{s.domain}/products/{hero['handle']}" if hero and hero.get("handle") else f"https://{s.domain}/"
    row = conn.execute("SELECT info, fetched_on FROM product_pages WHERE url = ?", (url,)).fetchone()
    if row and (today - date.fromisoformat(row["fetched_on"])).days < cfg.link.page_refresh_days:
        return PageInfo.from_dict(json.loads(row["info"]))
    page = fetcher(url)
    if page:
        conn.execute(
            "INSERT OR REPLACE INTO product_pages VALUES (?,?,?,?)",
            (url, s.domain, json.dumps(page.to_dict(), ensure_ascii=False), today.isoformat()),
        )
        conn.commit()
    return page


def understand_product(
    conn: sqlite3.Connection, extractor: ProductExtractor, ctx: ProductContext, key: str, today: date,
) -> ProductInsight | None:
    """Gọi Claude; kết quả cache theo dấu vân tay nội dung — ad/trang không đổi thì không gọi lại."""
    fp = ctx.fingerprint()
    row = conn.execute("SELECT fingerprint, insight FROM product_insights WHERE store_key = ?", (key,)).fetchone()
    if row and row["fingerprint"] == fp:
        return ProductInsight.from_dict(json.loads(row["insight"]))
    insight = extractor.extract(ctx)
    if insight:
        conn.execute(
            "INSERT OR REPLACE INTO product_insights VALUES (?,?,?,?,?)",
            (key, fp, json.dumps(insight.to_dict(), ensure_ascii=False), insight.model, today.isoformat()),
        )
        conn.commit()
    return insight


def count_competitors(conn: sqlite3.Connection, phrases: list[str], exclude_key: str) -> int:
    keys: set[str] = set()
    for phrase in phrases:
        like = f"%{phrase.lower()}%"
        for r in conn.execute(
            "SELECT DISTINCT COALESCE(domain, 'page:' || page_id) AS k FROM ads "
            "WHERE lower(titles) LIKE ? OR lower(bodies) LIKE ?",
            (like, like),
        ):
            keys.add(r["k"])
    keys.discard(exclude_key)
    return len(keys)


def select_stores(stores: list[StoreResult], cfg: Config) -> list[StoreResult]:
    lc = cfg.link
    picked = [s for s in stores if (s.scaling or not lc.only_scaling) and s.score >= lc.min_ad_score]
    return picked[: lc.top_n]


@dataclass
class _Understood:
    pk: kw.ProductKeywords
    insight: ProductInsight | None
    page: PageInfo | None


def link_stores(
    conn: sqlite3.Connection, cfg: Config, stores: list[StoreResult], provider: TrendsProvider, today: date,
    *, extractor: ProductExtractor | None = None, page_fetcher: PageFetcher | None = None,
) -> list[LinkedStore]:
    lc = cfg.link
    chosen = select_stores(stores, cfg)
    if page_fetcher is None and lc.fetch_pages:
        session = make_session()
        page_fetcher = lambda url: fetch_page(session, url)  # noqa: E731

    understood: dict[str, _Understood] = {}
    for s in chosen:
        titles, bodies, products, ctx = store_texts(conn, s)
        override = lc.overrides.get(s.domain or "") or lc.overrides.get(s.key)
        page = None if override else product_page(conn, cfg, s, products, titles + bodies, page_fetcher, today)
        insight = None
        if override:
            pk = kw.ProductKeywords(override[0], list(override), "config", [])
        else:
            if extractor:
                insight = understand_product(
                    conn, extractor,
                    ProductContext(s.domain, s.page_names, titles, bodies, page.to_dict() if page else None, ctx),
                    s.key, today,
                )
            if insight and insight.search_keywords:
                pk = kw.ProductKeywords(
                    insight.search_keywords[0], insight.search_keywords[: lc.max_keywords], "claude", []
                )
            else:
                pk = kw.extract(
                    titles, bodies, [p.get("title") or "" for p in products], domain=s.domain,
                    page_names=s.page_names, context_keywords=ctx, page=page.to_dict() if page else None,
                    max_keywords=lc.max_keywords,
                )
        understood[s.key] = _Understood(pk, insight, page)

    # chủ đề Trends: 1 cho mỗi sản phẩm (keyword neo) + 1 cho mỗi nhu cầu (keyword vấn đề)
    topics: dict[str, TrendTopic] = {}

    def add_topic(name: str, kws: list[str]) -> None:
        t = topics.setdefault(name, TrendTopic(name=name, seeds=[kws[0]]))
        t.keywords = list(dict.fromkeys([*t.keywords, *(k for k in kws[1:] if k != kws[0])]))

    for u in understood.values():
        if u.pk.seed:
            add_topic(f"product:{u.pk.seed}", u.pk.keywords or [u.pk.seed])
        if lc.need_trends and u.insight and u.insight.problem_keywords:
            add_topic(f"need:{u.insight.problem_keywords[0]}", u.insight.problem_keywords)

    trend_cfg = replace(cfg, trends=replace(cfg.trends, autocomplete=lc.autocomplete, max_keywords=lc.max_keywords + 2))
    results: dict[str, TopicResult] = {}
    if topics:
        for tr in run_trends(conn, trend_cfg, provider, today, topics=list(topics.values())):
            results[tr.name] = tr

    linked = []
    for s in chosen:
        u = understood[s.key]
        pk, ins = u.pk, u.insight
        tr = results.get(f"product:{pk.seed}") if pk.seed else None
        need_tr = results.get(f"need:{ins.problem_keywords[0]}") if ins and ins.problem_keywords else None
        home, need_home = (tr.home if tr else None), (need_tr.home if need_tr else None)
        trend, need = (home.stats if home else None), (need_home.stats if need_home else None)

        # sản phẩm quá mới/ít người gọi tên → dựa vào nhu cầu gốc (vd 'eye bags')
        basis, basis_trend, basis_score = "product", trend, (home.score if home else None)
        if (trend is None or trend["label"] not in USABLE) and need and need["label"] in USABLE:
            basis, basis_trend, basis_score = "need", need, need_home.score
        v = verdict(s.scaling, basis_trend) if pk.seed else "no_keyword"
        linked.append(
            LinkedStore(
                key=s.key, domain=s.domain, page_names=s.page_names, scaling=s.scaling,
                ad_score=s.score, ad_history=s.history, countries=s.countries,
                product=pk.seed, keywords=pk.keywords, keyword_source=pk.source,
                competitors=count_competitors(conn, pk.keywords[:3] or [pk.seed], s.key) if pk.seed else 0,
                trend=trend, trend_score=home.score if home else None,
                trend_series=(home.deseasonalized[-104:] if home else []),
                spread=(need_tr.spread if basis == "need" and need_tr else (tr.spread if tr else {})),
                generic_product=ins.generic_product if ins else None,
                problem_keywords=ins.problem_keywords if ins else [],
                persona=ins.target_persona if ins else None,
                confidence=ins.confidence if ins else None,
                page_url=u.page.url if u.page else None,
                need_trend=need, need_score=need_home.score if need_home else None,
                verdict=v, verdict_text=VERDICTS[v], verdict_basis=basis,
                win_score=win_score(s.score, basis_score, v) if pk.seed else 0.0,
            )
        )
    linked.sort(key=lambda x: (0 if x.verdict == "strong" else 1, -x.win_score))
    return linked


def to_dicts(linked: list[LinkedStore]) -> list[dict]:
    return [asdict(x) for x in linked]
