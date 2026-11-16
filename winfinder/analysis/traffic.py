"""Traffic: ad tăng thì traffic phải tăng song song mới là store hiệu quả.

Tín hiệu (miễn phí, đo được ngay):
  - Lượt tìm kiếm TÊN BRAND trên Google Trends (12 tháng, theo tuần): người search tên store = ad đang
    tạo ra khách thật / nhận biết thương hiệu. Store nhỏ thường chưa có dữ liệu → 'chưa đủ lớn'.
  - Reach EU của các ad ra mắt mỗi tuần (Ad Library `eu_total_reach`).

Bẫy tên brand trùng từ thông dụng ('glow', 'buddy'...): nếu lượt tìm đã có từ TRƯỚC khi store chạy ad
thì đó không phải traffic của store → đánh dấu 'mơ hồ' và bỏ qua.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

VERDICTS = {
    "confirmed": "Traffic tăng song song với ad — store đang bán được",
    "unconfirmed": "Ad tăng nhưng lượt tìm brand chưa tăng — có thể còn đang test, chưa chắc có lãi",
    "organic": "Lượt tìm brand tăng dù ad không tăng — brand có lực tự nhiên",
    "flat": "Ad và lượt tìm brand đều chưa tăng",
    "declining": "Lượt tìm brand đang giảm",
    "too_small": "Tên brand gần như chưa có lượt tìm — store còn nhỏ hoặc rất mới",
    "ambiguous": "Tên brand trùng từ thông dụng (đã có lượt tìm từ trước khi store chạy ad) — không đo được",
    "no_data": "Không lấy được dữ liệu",
}
FACTORS = {"confirmed": 1.1, "unconfirmed": 0.9, "declining": 0.9}  # nhân vào điểm tổng


def brand_term(domain: str | None, page_names: list[str]) -> str | None:
    """Tên brand để search: ưu tiên tên page (giữ khoảng trắng như người ta gõ), khớp với domain nếu có."""
    label = domain.split(".")[-2].lower() if domain and "." in domain else None
    for name in page_names:
        clean = re.sub(r"\(.*?\)|[™®©]", "", name).strip().lower()
        if not clean:
            continue
        if label is None or re.sub(r"[^a-z0-9]", "", clean) == re.sub(r"[^a-z0-9]", "", label):
            return clean
    return label


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _ratio(series: list[float], half: int) -> float:
    a, b = _mean(series[-2 * half : -half]), _mean(series[-half:])
    return (b + 1) / (a + 1) if (a or b) else 1.0


def _corr(x: list[float], y: list[float]) -> float | None:
    n = min(len(x), len(y))
    x, y = x[-n:], y[-n:]
    mx, my = _mean(x), _mean(y)
    vx = sum((a - mx) ** 2 for a in x)
    vy = sum((b - my) ** 2 for b in y)
    if not vx or not vy:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (vx * vy) ** 0.5


@dataclass
class TrafficSignal:
    brand_term: str | None
    verdict: str
    verdict_text: str
    brand_weeks: list[float] = field(default_factory=list)   # 12 tuần gần nhất, cùng trục với ad_history
    brand_ratio: float | None = None                         # 6 tuần gần / 6 tuần trước
    ad_ratio: float | None = None
    correlation: float | None = None                         # tương quan ad mới/tuần ↔ lượt tìm brand
    reach_weeks: list[int] = field(default_factory=list)     # reach EU của ad ra mắt mỗi tuần

    @property
    def factor(self) -> float:
        return FACTORS.get(self.verdict, 1.0)

    def to_dict(self) -> dict:
        return asdict(self)


def weekly_by_date(points: list[tuple[date, float]], as_of: date, weeks: int = 12) -> list[float]:
    """Chuỗi tuần của Trends → `weeks` giá trị cuối, căn theo cùng trục tuần với lịch sử ad."""
    out = [0.0] * weeks
    for d, v in points:
        delta = (as_of - d).days
        if 0 <= delta < weeks * 7:
            out[weeks - 1 - delta // 7] = max(out[weeks - 1 - delta // 7], v)
    return out


def assess(
    term: str | None,
    brand_points: list[tuple[date, float]] | None,
    ad_history: list[int],
    store_first: date | None,
    as_of: date,
    reach_weeks: list[int] | None = None,
    *,
    min_peak: float = 5.0,
) -> TrafficSignal:
    reach_weeks = reach_weeks or []
    if not term or brand_points is None:
        return TrafficSignal(term, "no_data", VERDICTS["no_data"], reach_weeks=reach_weeks)
    pts = sorted(brand_points)
    recent = weekly_by_date(pts, as_of, len(ad_history) or 12)
    ad_ratio = _ratio([float(x) for x in ad_history], 6)

    def sig(v: str, **kw) -> TrafficSignal:
        return TrafficSignal(term, v, VERDICTS[v], brand_weeks=[round(x, 1) for x in recent],
                             ad_ratio=round(ad_ratio, 2), reach_weeks=reach_weeks, **kw)

    if max(recent, default=0) < min_peak:
        return sig("too_small")
    # lượt tìm đã có từ trước khi store tồn tại → từ thông dụng, không phải brand này
    if store_first:
        before = [v for d, v in pts if d < store_first - timedelta(weeks=8)]
        if len(before) >= 8 and _mean(before) >= 0.3 * max(_mean(recent[-6:]), 1e-9):
            return sig("ambiguous")
    brand_ratio = _ratio(recent, 6)
    corr = _corr([float(x) for x in ad_history], recent)
    kw = dict(brand_ratio=round(brand_ratio, 2), correlation=round(corr, 2) if corr is not None else None)
    ad_up, brand_up = ad_ratio >= 1.5, brand_ratio >= 1.3
    if ad_up and brand_up:
        return sig("confirmed", **kw)
    if ad_up:
        return sig("unconfirmed", **kw)
    if brand_up:
        return sig("organic", **kw)
    if brand_ratio <= 0.7:
        return sig("declining", **kw)
    return sig("flat", **kw)
