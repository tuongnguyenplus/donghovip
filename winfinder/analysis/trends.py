"""Phân tích chuỗi Google Trends theo tuần: tách mùa vụ, đo xu hướng, phân loại.

Phương pháp (không cần numpy/statsmodels):
  1. Trend thô = trung bình trượt 2×52 tuần (căn giữa).
  2. Chỉ số mùa vụ = trung bình (giá trị − trend) theo từng tuần trong năm (cần ≥ 2 năm).
  3. Chuỗi khử mùa vụ = giá trị − chỉ số mùa vụ → dùng để đo xu hướng tới tận tuần gần nhất
     (trend trượt bị mất 26 tuần cuối, nên không dùng nó cho phần "đang tăng hay không").
  4. Độ mạnh mùa vụ F_s = max(0, 1 − Var(R) / Var(S + R))  (Hyndman).
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import date

Series = list[tuple[date, float]]

LABELS = {
    "rising": "Tăng bền vững",
    "breakout": "Bùng nổ gần đây",
    "seasonal": "Mùa vụ",
    "stable": "Ổn định / bão hòa",
    "declining": "Đang giảm",
    "spike": "Nhất thời (đã qua đỉnh)",
    "insufficient": "Thiếu dữ liệu",
}


# Đỉnh − đáy mùa vụ phải ≥ 40% mức trung bình mới coi là "mùa vụ" (lọc dao động nhỏ có chu kỳ)
MIN_SEASONAL_AMPLITUDE = 0.4


def _woy(d: date) -> int:
    return min(d.isocalendar().week, 52)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _slope(ys: list[float]) -> float:
    n = len(ys)
    if n < 2:
        return 0.0
    xm, ym = (n - 1) / 2, _mean(ys)
    var = sum((x - xm) ** 2 for x in range(n))
    return sum((x - xm) * (y - ym) for x, y in enumerate(ys)) / var


def centered_ma(values: list[float], period: int = 52) -> list[float | None]:
    """Trung bình trượt 2×period căn giữa; None ở hai đầu."""
    h = period // 2
    out: list[float | None] = [None] * len(values)
    for i in range(h, len(values) - h):
        window = values[i - h + 1 : i + h]
        out[i] = (0.5 * values[i - h] + sum(window) + 0.5 * values[i + h]) / period
    return out


def seasonal_index(dates: list[date], values: list[float]) -> tuple[dict[int, float] | None, list[float | None]]:
    trend = centered_ma(values)
    if len(values) < 104:
        return None, trend
    buckets: dict[int, list[float]] = {}
    for d, v, t in zip(dates, values, trend):
        if t is not None:
            buckets.setdefault(_woy(d), []).append(v - t)
    idx = {w: _mean(buckets.get(w, [])) for w in range(1, 53)}
    center = _mean(list(idx.values()))
    return {w: s - center for w, s in idx.items()}, trend


@dataclass
class TrendStats:
    n_weeks: int
    label: str
    label_vi: str
    yoy: float | None              # 13 tuần gần nhất so với cùng kỳ năm trước
    trend_growth: float | None     # tăng trưởng/năm ngầm định từ độ dốc chuỗi khử mùa vụ 52 tuần
    momentum: float | None         # 4 tuần gần nhất so với 12 tuần trước đó (khử mùa vụ)
    seasonality: float | None      # 0..1, độ đều đặn của chu kỳ năm (Hyndman F_s)
    seasonal_amplitude: float | None  # (đỉnh − đáy chỉ số mùa vụ) / trung bình — độ LỚN của mùa vụ
    peak_week: int | None          # tuần trong năm đạt đỉnh mùa vụ
    in_season: bool                # hiện tại đang trong mùa cao điểm (±4 tuần)
    recent_mean: float
    deseasonalized: list[float] = field(repr=False, default_factory=list)

    def to_dict(self, with_series: bool = False) -> dict:
        d = asdict(self)
        if not with_series:
            d.pop("deseasonalized")
        return d


def analyze_series(series: Series) -> TrendStats:
    series = sorted(series)
    dates = [d for d, _ in series]
    values = [float(v) for _, v in series]
    n = len(values)
    recent_mean = _mean(values[-13:])
    if n < 52 or max(values, default=0) <= 0:
        return TrendStats(n, "insufficient", LABELS["insufficient"], None, None, None, None, None, None, False,
                          recent_mean, values)

    idx, trend = seasonal_index(dates, values)
    seas = [idx[_woy(d)] if idx else 0.0 for d in dates]
    deseason = [max(v - s, 0.0) for v, s in zip(values, seas)]

    # độ mạnh mùa vụ + đỉnh mùa
    seasonality = peak_week = amplitude = None
    in_season = False
    if idx:
        sr = [v - t for v, t in zip(values, trend) if t is not None]
        r = [v - t - s for v, t, s in zip(values, trend, seas) if t is not None]
        var_sr = statistics.pvariance(sr) if len(sr) > 1 else 0.0
        seasonality = max(0.0, 1 - statistics.pvariance(r) / var_sr) if var_sr else 0.0
        peak_week = max(idx, key=idx.get)
        level = _mean(values)
        amplitude = (max(idx.values()) - min(idx.values())) / level if level > 0 else 0.0
        dist = abs(_woy(dates[-1]) - peak_week)
        in_season = min(dist, 52 - dist) <= 4 and seasonality >= 0.3 and amplitude >= MIN_SEASONAL_AMPLITUDE

    prev_year = values[-65:-52]
    yoy = (recent_mean / _mean(prev_year) - 1) if n >= 65 and _mean(prev_year) > 0 else None

    last52 = deseason[-52:]
    m52 = _mean(last52)
    trend_growth = _slope(last52) * 52 / m52 if m52 > 0 else None

    prior12 = _mean(deseason[-16:-4])
    momentum = _mean(deseason[-4:]) / prior12 - 1 if prior12 > 0 else None

    # nền = trung vị trước 26 tuần gần nhất
    baseline = max(statistics.median(values[:-26]) if n > 26 else 0.0, 1.0)
    last26 = values[-26:]
    peak26 = max(last26)
    peak_pos = len(last26) - 1 - last26.index(peak26)  # số tuần kể từ đỉnh
    is_spike = peak26 >= 3 * baseline and _mean(values[-4:]) <= 0.5 * peak26 and peak_pos >= 4
    is_breakout = _mean(values[-8:]) >= 2 * baseline and (yoy is None or yoy >= 1.0) and not is_spike

    if is_spike:
        label = "spike"
    elif is_breakout:
        label = "breakout"
    elif yoy is not None and yoy >= 0.2 and (trend_growth or 0) > 0.1:
        label = "rising"
    elif yoy is not None and yoy <= -0.2 and (trend_growth or 0) < -0.1:
        label = "declining"
    elif seasonality is not None and seasonality >= 0.5 and (amplitude or 0) >= MIN_SEASONAL_AMPLITUDE:
        label = "seasonal"
    else:
        label = "stable"

    return TrendStats(
        n_weeks=n,
        label=label,
        label_vi=LABELS[label],
        yoy=_r(yoy),
        trend_growth=_r(trend_growth),
        momentum=_r(momentum),
        seasonality=_r(seasonality),
        seasonal_amplitude=_r(amplitude),
        peak_week=peak_week,
        in_season=in_season,
        recent_mean=round(recent_mean, 2),
        deseasonalized=[round(x, 2) for x in deseason],
    )


def _r(x: float | None) -> float | None:
    return round(x, 3) if x is not None else None


def sum_series(series_list: list[Series]) -> Series:
    """Cộng các chuỗi theo ngày (chỉ giữ ngày có ở mọi chuỗi)."""
    if not series_list:
        return []
    maps = [dict(s) for s in series_list]
    common = set(maps[0]).intersection(*maps[1:])
    return sorted((d, sum(m[d] for m in maps)) for d in common)


def trend_score(st: TrendStats) -> float:
    """Điểm 0–100 cho mức độ 'đang lên' của một keyword/chủ đề."""
    if st.label == "insufficient":
        return 0.0
    def clamp(x: float) -> float:
        return max(0.0, min(1.0, x))
    yoy = clamp(((st.yoy or 0) + 0.2) / 1.2)            # −20% → 0, +100% → 1
    growth = clamp(((st.trend_growth or 0) + 0.1) / 0.9)
    mom = clamp(((st.momentum or 0) + 0.2) / 0.6)
    base = 0.45 * yoy + 0.35 * growth + 0.20 * mom
    if st.label == "spike":
        base *= 0.3
    elif st.label == "seasonal" and not st.in_season:
        base *= 0.8
    return round(100 * base, 1)
