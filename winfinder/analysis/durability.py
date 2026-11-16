"""Đà tăng thật + độ bền ad + nhân bản winner.

Tư duy (đã thống nhất):
  - Tăng trưởng thật = lượng ad ĐANG SỐNG tăng, không phải số creative mới ra. Store phải liên tục đẻ creative
    vì creative chết nhanh là "máy xay creative" → khó scale.
  - Độ bền: ad sống ≥ 30 ngày = bằng chứng có lãi; ≥ 60 ngày = evergreen. Tỉ lệ sống sót chỉ tính trên ad
    đã đủ tuổi (ad mới chưa thể sống 30 ngày → không được kéo tỉ lệ xuống).
  - Nhân bản: media buyer chỉ nhân bản ad đang thắng → cùng nội dung được chạy lại vào ngày khác.
    (API chỉ có chữ, không có ảnh/video → nhận diện creative theo nội dung chữ.)
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

ARCHETYPES = {
    "scaling": "Đang scale — ad đang sống tăng, ad bền",
    "early": "Tăng sớm — ad đang sống tăng, chưa đủ tuổi để chứng minh độ bền",
    "treadmill": "Máy xay creative — ra nhiều nhưng chết nhanh, khó scale",
    "evergreen": "Evergreen — ít ad mới nhưng ad cũ sống rất lâu",
    "declining": "Đang giảm — ad đang sống giảm",
    "testing": "Đang test — chưa ad nào đủ tuổi",
    "steady": "Ổn định",
    "stopped": "Đã dừng — không còn ad nào chạy",
}


@dataclass
class AdLife:
    start: date
    stop: date | None          # None = đang chạy
    creative: str              # nội dung đã chuẩn hoá (nhận diện creative)

    def alive_on(self, d: date) -> bool:
        return self.start <= d and (self.stop is None or self.stop >= d)

    def lifespan(self, as_of: date) -> int:
        return ((self.stop or as_of) - self.start).days


@dataclass
class DurabilityRules:
    profit_days: int = 30        # ad sống ≥ N ngày = bằng chứng có lãi
    evergreen_days: int = 60
    momentum_ratio: float = 1.5  # ad đang sống (2 tuần gần) / (tuần -6..-4)
    min_aged_ads: int = 3        # cần ≥ N ad đủ tuổi mới kết luận được về độ bền
    durable_survival: float = 0.3
    treadmill_survival: float = 0.15
    treadmill_launches_4w: int = 6


@dataclass
class Durability:
    active_now: int
    active_history: list[int]          # ad đang sống mỗi tuần (cũ → mới)
    launch_history: list[int]          # ad mới mỗi tuần
    active_growth: float               # đà tăng ad đang sống
    aged_ads: int                      # số ad đủ tuổi profit_days
    survival_30: float | None          # trong ad đủ tuổi, % sống ≥ profit_days
    survival_60: float | None
    alive_30: int                      # đang chạy VÀ đã sống ≥ profit_days
    alive_60: int
    oldest_active_days: int | None
    median_lifespan: float | None      # của ad đủ tuổi (ngày)
    launches_4w: int
    turnover: float                    # ad mới 4 tuần / ad đang sống — cao = phải thay creative liên tục
    creatives: int
    duplicated_creatives: int
    dup_share: float                   # % ad đang sống thuộc creative được nhân bản
    archetype: str
    archetype_vi: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def creative_key(title: str, body: str) -> str:
    text = f"{title} | {body}".lower()
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"[^a-z#|]+", " ", text).strip()


def active_weekly(ads: list[AdLife], as_of: date, weeks: int) -> list[int]:
    """Số ad sống vào ngày cuối của mỗi tuần (cũ → mới)."""
    out = []
    for w in range(weeks):
        day = as_of - timedelta(weeks=weeks - 1 - w)
        out.append(sum(1 for a in ads if a.alive_on(day)))
    return out


def launch_weekly(ads: list[AdLife], as_of: date, weeks: int) -> list[int]:
    out = [0] * weeks
    for a in ads:
        delta = (as_of - a.start).days
        if 0 <= delta < weeks * 7:
            out[weeks - 1 - delta // 7] += 1
    return out


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def measure(ads: list[AdLife], as_of: date, weeks: int = 12, rules: DurabilityRules | None = None) -> Durability:
    r = rules or DurabilityRules()
    active_hist = active_weekly(ads, as_of, weeks)
    launch_hist = launch_weekly(ads, as_of, weeks)
    active_now = active_hist[-1] if active_hist else 0
    # đà tăng: trung bình 2 tuần gần nhất so với tuần -6..-4 (làm mượt +1 cho store nhỏ)
    recent, before = _mean(active_hist[-2:]), _mean(active_hist[-6:-3])
    growth = (recent + 1) / (before + 1)

    aged = [a for a in ads if (as_of - a.start).days >= r.profit_days]
    aged60 = [a for a in ads if (as_of - a.start).days >= r.evergreen_days]
    surv30 = _mean(1.0 if a.lifespan(as_of) >= r.profit_days else 0.0 for a in aged) if aged else None
    surv60 = _mean(1.0 if a.lifespan(as_of) >= r.evergreen_days else 0.0 for a in aged60) if aged60 else None
    live = [a for a in ads if a.stop is None or a.stop >= as_of]
    alive30 = sum(1 for a in live if a.lifespan(as_of) >= r.profit_days)
    alive60 = sum(1 for a in live if a.lifespan(as_of) >= r.evergreen_days)
    oldest = max((a.lifespan(as_of) for a in live), default=None)
    median = statistics.median(a.lifespan(as_of) for a in aged) if aged else None
    launches_4w = sum(launch_hist[-4:])
    turnover = launches_4w / max(active_now, 1)

    by_creative: dict[str, list[AdLife]] = defaultdict(list)
    for a in ads:
        by_creative[a.creative].append(a)
    dup = {c for c, xs in by_creative.items() if len({a.start for a in xs}) >= 2}
    dup_share = (sum(1 for a in live if a.creative in dup) / len(live)) if live else 0.0

    momentum = growth >= r.momentum_ratio
    enough_age = len(aged) >= r.min_aged_ads
    durable = enough_age and ((surv30 or 0) >= r.durable_survival or alive30 >= 2)
    notes = []
    if not enough_age:
        notes.append(f"chỉ {len(aged)} ad đủ {r.profit_days} ngày — chưa kết luận được độ bền")

    if active_now == 0:
        arch = "stopped"
    elif momentum:
        if durable:
            arch = "scaling"
        elif not enough_age:
            # chưa ad nào đủ 30 ngày: nếu ad đủ 14 ngày phần lớn đã chết → vẫn chỉ là đang test
            aged14 = [a for a in ads if (as_of - a.start).days >= 14]
            surv14 = _mean(1.0 if a.lifespan(as_of) >= 14 else 0.0 for a in aged14) if aged14 else None
            arch = "testing" if surv14 is not None and surv14 < 0.5 else "early"
        else:
            arch = "treadmill"
    elif enough_age and (surv30 or 0) < r.treadmill_survival and launches_4w >= r.treadmill_launches_4w:
        arch = "treadmill"
    elif growth <= 1 / r.momentum_ratio:
        arch = "declining"
    elif alive60 >= 2:
        arch = "evergreen"
    elif not enough_age and launches_4w >= 3:
        arch = "testing"
    else:
        arch = "steady"

    return Durability(
        active_now=active_now, active_history=active_hist, launch_history=launch_hist,
        active_growth=round(growth, 2), aged_ads=len(aged),
        survival_30=round(surv30, 3) if surv30 is not None else None,
        survival_60=round(surv60, 3) if surv60 is not None else None,
        alive_30=alive30, alive_60=alive60, oldest_active_days=oldest,
        median_lifespan=round(median, 1) if median is not None else None,
        launches_4w=launches_4w, turnover=round(turnover, 2),
        creatives=len(by_creative), duplicated_creatives=len(dup), dup_share=round(dup_share, 3),
        archetype=arch, archetype_vi=ARCHETYPES[arch], notes=notes,
    )
