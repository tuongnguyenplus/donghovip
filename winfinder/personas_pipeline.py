"""Persona: nhu cầu riêng của từng angle (Trends theo nỗi đau) + xác minh trên mạng xã hội.

Với mỗi angle của các sản phẩm ứng viên:
  1. Trends cho `pain_keywords` của angle → nhu cầu của PERSONA đó có đang tăng không (khác nhu cầu sản phẩm).
  2. Tìm bài viết thật (Reddit, YouTube…) theo `social_queries` → khối lượng, đà, cộng đồng.
  3. Claude đọc mẫu bài → persona có thật không, trích dẫn, ngôn ngữ của khách.
Kết quả: reports/personas.json — trang Cơ hội angle đọc và chấm theo hồ sơ angle.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta

from winfinder.analysis.social import (
    VERDICT_VI, SocialStats, Verification, Verifier, dedupe, fingerprint, heuristic_verdict, sample, social_score,
    stats,
)
from winfinder.config import Config, TrendTopic
from winfinder.sources.social import SocialPost, SocialSource
from winfinder.sources.trends import TrendsProvider
from winfinder.trends_pipeline import run_trends

log = logging.getLogger(__name__)
STATUS_ORDER = {"rising": 0, "new": 1, "steady": 2, "churn": 3, "declining": 4, "stopped": 5}


@dataclass
class PersonaResult:
    key: str                         # "<sản phẩm>|<id angle>"
    product: str
    angle: str
    persona: str
    pain_keywords: list[str]
    social_queries: list[str]
    need: dict | None = None         # thống kê Trends của nhu cầu persona
    need_score: float | None = None
    social: dict | None = None       # SocialStats
    verification: dict | None = None
    social_score: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _cached_posts(conn: sqlite3.Connection, src: SocialSource, query: str, since: date, today: date,
                  refresh_days: int) -> list[SocialPost]:
    row = conn.execute("SELECT posts, fetched_on FROM social_cache WHERE source = ? AND query = ?",
                       (src.name, query)).fetchone()
    if row and (today - date.fromisoformat(row["fetched_on"])).days < refresh_days:
        return [SocialPost(**p) for p in json.loads(row["posts"])]
    posts = src.search(query, since)
    conn.execute("INSERT OR REPLACE INTO social_cache VALUES (?,?,?,?)",
                 (src.name, query, today.isoformat(), json.dumps([p.to_dict() for p in posts], ensure_ascii=False)))
    conn.commit()
    return posts


def select_angles(angles_doc: dict, cfg: Config) -> list[tuple[str, str, dict]]:
    """(sản phẩm, tên chung, angle) — ưu tiên angle đang lên/mới; bỏ angle không có nỗi đau (vd ưu đãi)."""
    pc = cfg.personas
    out = []
    for prod in (angles_doc.get("products") or [])[: pc.max_products]:
        ranked = sorted(prod.get("angles") or [], key=lambda a: (STATUS_ORDER.get(a["status"], 9), -a["recent_share"]))
        picked = [a for a in ranked if a["angle"].get("pain_keywords") or a["angle"].get("social_queries")]
        out += [(prod["product"], prod.get("generic") or prod["product"], a) for a in picked[: pc.max_angles]]
    return out


def run_personas(
    conn: sqlite3.Connection, cfg: Config, angles_doc: dict, today: date, *,
    provider: TrendsProvider | None = None, sources: list[SocialSource] | None = None,
    verifier: Verifier | None = None, skipped_sources: list[str] | None = None,
) -> list[PersonaResult]:
    pc = cfg.personas
    chosen = select_angles(angles_doc, cfg)
    sources = sources or []
    results: list[PersonaResult] = []

    # 1) Trends cho nhu cầu của từng persona (gom một lần, các angle trùng keyword dùng chung)
    need: dict[str, tuple[dict, float]] = {}
    if provider and pc.trends:
        topics = {}
        for _p, _g, a in chosen:
            kws = a["angle"].get("pain_keywords") or []
            if kws:
                topics.setdefault(kws[0], TrendTopic(name=f"persona:{kws[0]}", seeds=kws[:1], keywords=kws[1:3]))
        if topics:
            pcfg = replace(cfg, trends=replace(cfg.trends, autocomplete=False))
            for tr in run_trends(conn, pcfg, provider, today, topics=list(topics.values())):
                if tr.home:
                    need[tr.name.split(":", 1)[1]] = (tr.home.stats, tr.home.score)

    since = today - timedelta(days=pc.lookback_days)
    for product, generic, a in chosen:
        ang = a["angle"]
        r = PersonaResult(f"{product}|{ang['id']}", product, ang["name"], ang["persona"],
                          ang.get("pain_keywords") or [], ang.get("social_queries") or [])
        kw0 = (ang.get("pain_keywords") or [None])[0]
        if kw0 in need:
            r.need, r.need_score = need[kw0]

        # 2) bài viết thật
        posts: list[SocialPost] = []
        queries = list(dict.fromkeys((ang.get("social_queries") or [])[:2] + (ang.get("pain_keywords") or [])[:1]))
        for src in sources:
            for q in queries:
                try:
                    posts += _cached_posts(conn, src, q, since, today, pc.refresh_days)
                except Exception as exc:  # một nguồn lỗi không làm hỏng cả lần chạy
                    log.warning("%s '%s': %s", src.name, q, exc)
                    r.notes.append(f"{src.name}: lỗi khi tìm “{q}”")
        posts = dedupe(posts)
        if not sources:
            r.notes.append("chưa có nguồn mạng xã hội nào (cần REDDIT_CLIENT_ID/SECRET hoặc YOUTUBE_API_KEY)")
            r.notes += skipped_sources or []
            r.verification = Verification("unverified", VERDICT_VI["unverified"], "low", None, method="none").to_dict()
            results.append(r)
            continue
        st: SocialStats = stats(posts, (ang.get("pain_keywords") or []) + (ang.get("social_queries") or []), today)
        r.social = asdict(st)

        # 3) xác minh — cache theo nội dung (angle + tập bài), không gọi lại nếu không có bài mới
        ver: Verification | None = None
        smp = sample(posts, pc.sample_posts)
        if verifier and smp and pc.verify:
            fp = fingerprint(ang, smp)
            row = conn.execute("SELECT fingerprint, result FROM social_verdicts WHERE angle_key = ?",
                               (r.key,)).fetchone()
            if row and row["fingerprint"] == fp:
                ver = Verification(**json.loads(row["result"]))
            else:
                ver = verifier.verify(generic, ang, smp)
                if ver:
                    conn.execute("INSERT OR REPLACE INTO social_verdicts VALUES (?,?,?,?)",
                                 (r.key, fp, json.dumps(ver.to_dict(), ensure_ascii=False), today.isoformat()))
                    conn.commit()
        if ver is None:
            ver = heuristic_verdict(st)
            if verifier is None:
                r.notes.append("chưa có ANTHROPIC_API_KEY — kết luận chỉ dựa trên đếm từ khoá")
        r.verification = ver.to_dict()
        r.social_score = social_score(st, ver)
        r.notes += skipped_sources or []
        results.append(r)
        log.info("[%s] %s: %d bài, %s", generic, ang["name"], st.posts, ver.verdict)
    return results


def write(results: list[PersonaResult], report_dir, today: date) -> None:
    from pathlib import Path

    p = Path(report_dir)
    p.mkdir(parents=True, exist_ok=True)
    (p / "personas.json").write_text(json.dumps(
        {"as_of": today.isoformat(), "personas": [r.to_dict() for r in results]}, ensure_ascii=False, indent=2),
        encoding="utf-8")
