"""Chạy phân tích angle cho các sản phẩm trên trang Ứng viên."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from collections import Counter
from datetime import date, timedelta

from winfinder.analysis.angles import AngleAnalyzer, AngleResult, AngleStats, ad_units, angle_stats, fingerprint
from winfinder.analysis.gaps import gap_rows
from winfinder.config import Config

log = logging.getLogger(__name__)


@dataclass
class ProductAngles:
    product: str
    generic: str | None
    stores: list[str]
    units: int                     # số nội dung ad khác nhau
    assigned: int                  # số nội dung đã gán angle
    angles: list[AngleStats]
    untapped: list[dict] = field(default_factory=list)
    model: str = ""
    cached: bool = False
    gaps: list[dict] = field(default_factory=list)       # bản đồ khoảng trống (cầu organic vs cung ad)
    phrases: list[dict] = field(default_factory=list)    # customer language bank (nguyên văn, có link)
    corpus: dict = field(default_factory=dict)           # kho organic: số bài theo nguồn, truy vấn

    def to_dict(self) -> dict:
        return asdict(self)


def _group(linked: list[dict]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for x in linked:
        if not x.get("product"):
            continue
        g = groups.setdefault(x["product"], {"generic": x.get("generic_product"), "stores": [], "best": 0.0,
                                             "problems": []})
        g["stores"].append(x["key"])
        g["problems"] = list(dict.fromkeys(g["problems"] + list(x.get("problem_keywords") or [])))
        g["generic"] = g["generic"] or x.get("generic_product")
        g["best"] = max(g["best"], x.get("win_score") or 0.0)
    return dict(sorted(groups.items(), key=lambda kv: -kv[1]["best"]))


MAX_POSTS = 120     # số bài organic Claude đọc cùng với ad cho mỗi sản phẩm


def gather_corpus(conn: sqlite3.Connection, cfg: Config, product: str, generic: str | None, problems: list[str],
                  sources: list, today: date) -> tuple[list, dict]:
    """Kho organic của sản phẩm: bài + bình luận theo nỗi đau và tên gọi chung (cache theo social_cache)."""
    from winfinder.analysis.social import dedupe, sample
    from winfinder.personas_pipeline import _cached_posts

    queries = list(dict.fromkeys([*problems[:2], generic or product]))
    since = today - timedelta(days=cfg.personas.lookback_days)
    posts, errors = [], []
    for src in sources:
        for q in queries:
            try:
                posts += _cached_posts(conn, src, q, since, today, cfg.personas.refresh_days)
            except Exception as exc:  # một nguồn lỗi / hết ngân sách không làm hỏng cả bước
                errors.append(f"{src.name} '{q}': {exc}")
    posts = [p for p in dedupe(posts) if len(f"{p.title} {p.text}".split()) >= 3]
    posts = sample(sorted(posts, key=lambda p: (p.source, p.id)), MAX_POSTS)   # thứ tự ổn định → cache đúng
    return posts, {"queries": queries, "posts": len(posts), "by_source": dict(Counter(p.source for p in posts)),
                   "errors": errors[:5]}


def run_angles(
    conn: sqlite3.Connection, cfg: Config, linked: list[dict], analyzer: AngleAnalyzer | None, today: date,
    sources: list | None = None,
) -> list[ProductAngles]:
    """sources: nguồn organic (TikTok/Reddit/YouTube). Có → angle được chia CHUNG cho ad + organic → bản đồ khoảng trống."""
    ac = cfg.angles
    out = []
    for product, g in list(_group(linked).items())[: ac.max_products]:
        units, meta = ad_units(conn, g["stores"])
        if len(units) < ac.min_ads:
            log.info("[%s] chỉ %d nội dung ad — bỏ qua phân tích angle", product, len(units))
            continue
        posts, corpus = (gather_corpus(conn, cfg, product, g["generic"], g["problems"], sources, today)
                         if sources else ([], {}))
        fp = fingerprint(product, units, posts)
        row = conn.execute("SELECT fingerprint, result FROM product_angles WHERE product = ?", (product,)).fetchone()
        cached = bool(row and row["fingerprint"] == fp)
        if cached:
            result = AngleResult.from_dict(json.loads(row["result"]))
        elif analyzer is None:
            if not row:
                continue
            result = AngleResult.from_dict(json.loads(row["result"]))   # kết quả cũ (dữ liệu đã đổi)
            posts = [] if result.post_assignments else posts
        else:
            log.info("[%s] phân tích angle: %d nội dung ad + %d bài organic (%s)", product, len(units), len(posts),
                     analyzer.model)
            result = analyzer.analyze(product, g["generic"], units, posts)
            if result is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO product_angles VALUES (?,?,?,?,?)",
                (product, fp, json.dumps(result.to_dict(), ensure_ascii=False), result.model, today.isoformat()),
            )
            conn.commit()
        stats = angle_stats(result, units, meta, today, cfg.durability)
        texts = [f"{p.title} {p.text}".lower() for p in posts]
        phrases, seen = [], set()
        for x in result.phrases:  # cùng một cụm từ ở nhiều bài → giữ một dòng, tần suất đếm trên cả kho
            k = (x["angle"], x["phrase"].lower())
            if k in seen:
                continue
            seen.add(k)
            phrases.append({**x, "count": sum(1 for t in texts if x["phrase"].lower() in t) if texts else x.get("count")})
        result.phrases = phrases
        gaps = gap_rows(result, stats, posts, today) if posts and result.post_assignments else []
        out.append(ProductAngles(
            product=product, generic=g["generic"], stores=g["stores"], units=len(units),
            assigned=sum(1 for u in units if u.ref in result.assignments),
            angles=stats, untapped=result.untapped, model=result.model, cached=cached,
            gaps=[x.to_dict() for x in gaps], phrases=phrases, corpus=corpus,
        ))
    return out


def load_linked(report_dir) -> list[dict]:
    from pathlib import Path

    p = Path(report_dir) / "winners.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("stores", [])


def load_doc(report_dir) -> dict:
    from pathlib import Path

    p = Path(report_dir) / "angles.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
