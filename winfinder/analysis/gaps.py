"""Bản đồ khoảng trống: CẦU (khách nói gì — organic) so với CUNG (đối thủ bán gì — ad), trên cùng bộ angle.

Cầu  = tỉ trọng thảo luận organic của angle (có trọng số tương tác) + đà tăng (90 ngày gần / 90 ngày trước).
Cung = tỉ trọng ad ĐANG SỐNG của angle + số store + độ bền ad (sống sót 30 ngày, ad ≥ 30 ngày).

Lưu ý khi đọc: tỉ trọng cầu là tỉ trọng THẢO LUẬN của người lên mạng nói (khác nhau theo nền tảng, theo từ khoá
tìm), không phải tỉ trọng người mua — dùng để so tương đối giữa các angle và theo thời gian.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

DEMAND_MIN = 0.10      # angle chiếm ≥ 10% thảo luận = có cầu đáng kể
DEMAND_LOW = 0.05
SUPPLY_LOW = 0.05      # < 5% ad đang sống = gần như chưa ai bán bằng angle này
SUPPLY_HIGH = 0.20
MIN_POSTS = 3          # ít hơn → chưa đủ để nói về cầu

VERDICTS = {
    "gap": "Khoảng trống — khách nói nhiều, gần như chưa ai chạy ad",
    "proven": "Đã kiểm chứng — có cầu, đối thủ chạy và ad sống tốt; vào được nếu làm tốt hơn",
    "fumbled": "Tranh nhau sai cách — có cầu, nhiều ad nhưng chết nhanh; cơ hội cho creative tốt hơn",
    "oversupplied": "Bão hoà — nhiều ad nhưng khách ít nói / đang nói ít đi",
    "balanced": "Cân bằng — cung và cầu tương xứng",
    "minor": "Nhỏ — ít dữ liệu cả hai phía",
    "generic": "Ad chung chung (chỉ giảm giá / cam kết) — không nhắm persona nào",
}
ORDER = ["gap", "fumbled", "proven", "balanced", "oversupplied", "minor", "generic"]


@dataclass
class GapRow:
    angle_id: str
    name: str
    persona: str
    pain: str
    promise: str
    hook: str
    demand_share: float
    demand_posts: int
    demand_growth: float | None
    demand_sources: dict
    supply_share: float
    supply_ads: int
    supply_stores: int
    supply_status: str | None
    supply_survival: float | None
    supply_alive_30: int
    verdict: str
    verdict_vi: str
    opportunity: float              # 0–100: cầu lớn, cung nhỏ, cầu đang tăng → cao
    phrases: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _weight(p) -> float:
    return 1.0 + math.log1p(max(0, p.score) + max(0, p.comments))


def verdict(demand: float, posts: int, growth: float | None, supply: float, status: str | None,
            survival: float | None, alive30: int) -> str:
    if posts < MIN_POSTS and supply < SUPPLY_LOW:
        return "minor"
    if demand >= DEMAND_MIN and supply < SUPPLY_LOW:
        return "gap"
    if demand >= DEMAND_MIN and status == "churn":
        return "fumbled"
    if demand >= DEMAND_MIN and (alive30 >= 2 or (survival or 0) >= 0.3 or status in ("rising", "steady", "new")):
        return "proven"
    if supply >= SUPPLY_HIGH and (demand < DEMAND_LOW or (growth is not None and growth < 0.7)):
        return "oversupplied"
    return "balanced"


def opportunity(demand: float, supply: float, growth: float | None, v: str) -> float:
    ratio = (demand + 0.01) / (supply + 0.05)
    score = min(1.0, math.log2(ratio + 1) / 3)
    if growth is not None:
        score *= 1.2 if growth >= 1.3 else (0.8 if growth < 0.7 else 1.0)
    if v == "fumbled":
        score = max(score, 0.5)
    if v in ("oversupplied", "minor", "generic"):
        score *= 0.5
    return round(100 * min(1.0, score), 1)


def gap_rows(result, stats: list, posts: list, today: date) -> list[GapRow]:
    """result: AngleResult (có post_assignments), stats: list[AngleStats], posts: kho organic theo thứ tự o1, o2…"""
    by_ref = {f"o{i}": p for i, p in enumerate(posts, 1)}
    assigned = [(by_ref[r], a) for r, a in result.post_assignments.items() if r in by_ref]
    total_w = sum(_weight(p) for p, _ in assigned) or 1.0
    st_by = {s.angle.id: s for s in stats}
    d90, d180 = (today - timedelta(days=90)).isoformat(), (today - timedelta(days=180)).isoformat()
    rows = []
    for a in result.angles:
        mine = [p for p, aid in assigned if aid == a.id]
        demand = sum(_weight(p) for p in mine) / total_w if assigned else 0.0
        recent = sum(1 for p in mine if p.created >= d90)
        prev = sum(1 for p in mine if d180 <= p.created < d90)
        growth = round((recent + 1) / (prev + 1), 2) if len(mine) >= 5 else None
        s = st_by.get(a.id)
        supply = s.recent_share if s else 0.0
        status = s.status if s else None
        v = "generic" if a.hook_type == "offer" else verdict(
            demand, len(mine), growth, supply, status, s.survival_30 if s else None, s.alive_30 if s else 0)
        rows.append(GapRow(
            a.id, a.name, a.persona, a.pain_point, a.promise, a.hook_type,
            round(demand, 3), len(mine), growth, dict(Counter(p.source for p in mine)),
            round(supply, 3), s.ads if s else 0, len(s.stores) if s else 0, status,
            s.survival_30 if s else None, s.alive_30 if s else 0,
            v, VERDICTS[v], opportunity(demand, supply, growth, v),
            [x for x in result.phrases if x["angle"] == a.id][:12],
        ))
    rows.sort(key=lambda r: (ORDER.index(r.verdict), -r.opportunity))
    return rows
