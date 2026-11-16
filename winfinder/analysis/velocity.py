"""Creative velocity: số creative mới mỗi tuần và tín hiệu "đang vào phase scale"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


def weekly_counts(start_dates: list[date], as_of: date, weeks: int) -> list[int]:
    """Đếm số ad bắt đầu chạy theo từng tuần, cũ → mới. Tuần cuối kết thúc tại `as_of`."""
    counts = [0] * weeks
    for d in start_dates:
        delta = (as_of - d).days
        if 0 <= delta < weeks * 7:
            counts[weeks - 1 - delta // 7] += 1
    return counts


@dataclass
class Growth:
    counts: list[int]
    total: int
    active_weeks: int
    slope: float          # creative/tuần tăng thêm mỗi tuần (hồi quy tuyến tính)
    slope_norm: float     # slope / trung bình → so sánh được giữa store to và nhỏ
    growth_ratio: float   # (nửa sau + 1) / (nửa đầu + 1)
    recent: bool          # 2 tuần gần nhất còn ra creative mới


def growth(counts: list[int]) -> Growth:
    n = len(counts)
    total = sum(counts)
    mean = total / n if n else 0.0
    xs = range(n)
    x_mean = (n - 1) / 2
    var = sum((x - x_mean) ** 2 for x in xs)
    slope = sum((x - x_mean) * (c - mean) for x, c in zip(xs, counts)) / var if var else 0.0
    half = n // 2
    first, second = sum(counts[:half]), sum(counts[n - half:])
    return Growth(
        counts=counts,
        total=total,
        active_weeks=sum(1 for c in counts if c > 0),
        slope=slope,
        slope_norm=slope / mean if mean else 0.0,
        growth_ratio=(second + 1) / (first + 1),
        recent=sum(counts[-2:]) > 0,
    )


def is_scaling(g: Growth, *, min_total_ads: int, min_active_weeks: int, min_growth_ratio: float) -> bool:
    """Store đang scale: đủ ad, ra creative đều đặn, nửa sau nhiều hơn nửa đầu, vẫn còn đang ra."""
    return (
        g.total >= min_total_ads
        and g.active_weeks >= min_active_weeks
        and g.growth_ratio >= min_growth_ratio
        and g.slope > 0
        and g.recent
    )
