"""Hồ sơ chiến lược: định nghĩa "winning" do người dùng tự chỉnh (profiles.yaml).

Mỗi hồ sơ = vài ĐIỀU KIỆN BẮT BUỘC + TRỌNG SỐ các chỉ số + NGƯỠNG kết luận.
Mỗi store / sản phẩm / angle được chấm theo MỌI hồ sơ cùng lúc → biết nó hợp chiến lược nào.

Nguyên tắc:
  - Thiếu dữ liệu ≠ trượt. Chỉ số không có dữ liệu: bỏ khỏi phép tính (chia lại trọng số), điều kiện → "chưa rõ".
  - Giải thích được: trả về từng điều kiện đạt/trượt/chưa rõ và điểm đóng góp của từng chỉ số.
  - Ít điều kiện cứng, nhiều điểm mềm.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    group: str
    kind: str = "number"                            # number | category | bool
    norm: Callable[[Any], float] | None = None      # giá trị thô → 0..1 (cao = "nhiều hơn")
    fmt: str = "{:g}"
    choices: tuple[str, ...] = ()
    help: str = ""

    def show(self, v: Any) -> str:
        if v is None:
            return "–"
        if self.kind == "bool":
            return "có" if v else "không"
        if self.kind == "category":
            return CHOICE_VI.get(self.key, {}).get(v, str(v))
        try:
            return self.fmt.format(v)
        except (ValueError, TypeError):
            return str(v)


TREND_LABELS = ("rising", "breakout", "seasonal", "stable", "declining", "spike", "insufficient")
ARCHETYPES = ("scaling", "early", "evergreen", "steady", "testing", "treadmill", "declining", "stopped", "observing")
TRAFFIC = ("confirmed", "organic", "unconfirmed", "flat", "declining", "too_small", "ambiguous", "no_data")
ANGLE_STATUS = ("rising", "new", "steady", "churn", "declining", "stopped", "observing")
SOCIAL_VERDICTS = ("confirmed", "partial", "weak", "not_found")
KINDS = ("product", "store", "angle")
PCT = "{:.0%}"

# mã → tiếng Việt (mã giữ nguyên trong profiles.yaml để ổn định)
CHOICE_VI: dict[str, dict[str, str]] = {
    "trend_label": {"rising": "Tăng bền vững", "breakout": "Bùng nổ gần đây", "seasonal": "Mùa vụ",
                    "stable": "Ổn định/bão hòa", "declining": "Đang giảm", "spike": "Nhất thời",
                    "insufficient": "Thiếu dữ liệu"},
    "archetype": {"scaling": "Đang scale", "early": "Tăng sớm", "evergreen": "Evergreen", "steady": "Ổn định",
                  "testing": "Đang test", "treadmill": "Máy xay creative", "declining": "Đang giảm",
                  "stopped": "Đã dừng", "observing": "Đang theo dõi"},
    "traffic": {"confirmed": "Tăng cùng ad", "organic": "Tăng tự nhiên", "unconfirmed": "Chưa tăng theo ad",
                "flat": "Đi ngang", "declining": "Đang giảm", "too_small": "Chưa đủ lớn", "ambiguous": "Tên mơ hồ",
                "no_data": "Không có dữ liệu"},
    "angle_status": {"rising": "Đang lên", "new": "Mới xuất hiện", "steady": "Ổn định", "churn": "Creative chết nhanh",
                     "declining": "Đang giảm", "stopped": "Đã dừng", "observing": "Đang theo dõi"},
}
CHOICE_VI["need_label"] = CHOICE_VI["trend_label"]
CHOICE_VI["persona_need_label"] = CHOICE_VI["trend_label"]
CHOICE_VI["gap_verdict"] = {"gap": "Khoảng trống", "fumbled": "Tranh nhau sai cách", "proven": "Đã kiểm chứng",
                            "balanced": "Cân bằng", "oversupplied": "Bão hoà", "minor": "Nhỏ",
                            "generic": "Ad chung chung"}
CHOICE_VI["social_verdict"] = {"confirmed": "Xác thực", "partial": "Một phần", "weak": "Yếu",
                               "not_found": "Không tìm thấy"}

METRICS: dict[str, Metric] = {m.key: m for m in [
    # --- Đà tăng & độ bền ad
    Metric("active_now", "Ad đang sống", "Ad", norm=lambda x: _clamp(math.log10(x + 1) / 2), fmt="{:.0f}"),
    Metric("active_growth", "Đà tăng ad đang sống", "Ad", norm=lambda x: _clamp((x - 1) / 1.5), fmt="{:.2f}×",
           help="ad đang sống 2 tuần gần / tuần −6…−4"),
    Metric("survival_30", "Sống sót 30 ngày", "Ad", norm=lambda x: _clamp(x), fmt=PCT,
           help="trong ad đủ 30 ngày tuổi, % sống ≥ 30 ngày"),
    Metric("alive_30", "Ad ≥ 30 ngày đang chạy", "Ad", norm=lambda x: _clamp(x / 5), fmt="{:.0f}"),
    Metric("alive_60", "Ad ≥ 60 ngày đang chạy", "Ad", norm=lambda x: _clamp(x / 3), fmt="{:.0f}"),
    Metric("dup_share", "Nhân bản winner", "Ad", norm=lambda x: _clamp(x), fmt=PCT),
    Metric("turnover", "Tốc độ thay creative", "Ad", norm=lambda x: _clamp(x / 2.5), fmt="{:.2f}",
           help="ad mới 4 tuần / ad đang sống — cao = phải đẻ creative liên tục"),
    Metric("direct_share", "Chạy thẳng store/PDP", "Ad", norm=lambda x: _clamp((x - 0.3) / 0.7), fmt=PCT),
    Metric("archetype", "Dạng store", "Ad", kind="category", choices=ARCHETYPES),
    # --- Store
    Metric("store_age_days", "Tuổi store (ngày)", "Store", norm=lambda x: _clamp(x / 365), fmt="{:.0f}"),
    Metric("pages", "Số page", "Store", norm=lambda x: _clamp((x - 1) / 3), fmt="{:.0f}"),
    Metric("countries", "Số thị trường", "Store", norm=lambda x: _clamp(x / 4), fmt="{:.0f}"),
    Metric("competitors", "Số store đối thủ", "Store", norm=lambda x: _clamp(x / 10), fmt="{:.0f}"),
    # --- Sản phẩm
    Metric("evidence_stores", "Store bằng chứng đạt chuẩn", "Sản phẩm", norm=lambda x: _clamp(x / 4), fmt="{:.0f}"),
    Metric("stores", "Tổng số store", "Sản phẩm", norm=lambda x: _clamp(x / 8), fmt="{:.0f}"),
    # --- Nhu cầu
    Metric("trend_label", "Xu hướng tìm kiếm", "Nhu cầu", kind="category", choices=TREND_LABELS),
    Metric("trend_score", "Điểm trends", "Nhu cầu", norm=lambda x: _clamp(x / 100), fmt="{:.0f}"),
    Metric("trend_yoy", "Tìm kiếm so cùng kỳ", "Nhu cầu", norm=lambda x: _clamp((x + 0.2) / 1.2), fmt="{:+.0%}"),
    Metric("trend_growth", "Xu hướng/năm (khử mùa vụ)", "Nhu cầu", norm=lambda x: _clamp((x + 0.1) / 0.9),
           fmt="{:+.0%}"),
    Metric("seasonal_amplitude", "Biên độ mùa vụ", "Nhu cầu", norm=lambda x: _clamp(x / 2), fmt="{:.0%}"),
    Metric("in_season", "Đang vào mùa", "Nhu cầu", kind="bool"),
    Metric("need_label", "Xu hướng nhu cầu gốc", "Nhu cầu", kind="category", choices=TREND_LABELS),
    Metric("need_score", "Điểm nhu cầu gốc", "Nhu cầu", norm=lambda x: _clamp(x / 100), fmt="{:.0f}"),
    # --- Traffic
    Metric("traffic", "Traffic so với ad", "Traffic", kind="category", choices=TRAFFIC),
    Metric("brand_ratio", "Lượt tìm brand (6 tuần gần / trước)", "Traffic",
           norm=lambda x: _clamp((x - 0.7) / 1.8), fmt="{:.2f}×"),
    Metric("traffic_confirmed", "Store có traffic tăng cùng ad", "Traffic", norm=lambda x: _clamp(x / 3), fmt="{:.0f}"),
    Metric("ig_engagement", "Tương tác Instagram brand (bài mới / bài cũ)", "Traffic",
           norm=lambda x: _clamp((x - 0.7) / 1.8), fmt="{:.2f}×"),
    Metric("ig_posts_30d", "Bài Instagram brand 30 ngày", "Traffic", norm=lambda x: _clamp(x / 12), fmt="{:.0f}"),
    # --- Angle
    Metric("angles_total", "Số angle đang chạy", "Angle", norm=lambda x: _clamp(x / 6), fmt="{:.0f}"),
    Metric("angles_rising", "Số angle đang lên / mới", "Angle", norm=lambda x: _clamp(x / 3), fmt="{:.0f}"),
    Metric("angle_status", "Trạng thái angle", "Angle", kind="category", choices=ANGLE_STATUS),
    Metric("angle_share", "Tỉ trọng ad đang sống của angle", "Angle", norm=lambda x: _clamp(x / 0.5), fmt=PCT),
    Metric("angle_stores", "Số store chạy angle", "Angle", norm=lambda x: _clamp(x / 4), fmt="{:.0f}"),
    # --- Persona (nhu cầu riêng của angle + xác minh mạng xã hội)
    Metric("persona_need_label", "Xu hướng nhu cầu của persona", "Persona", kind="category", choices=TREND_LABELS,
           help="Google Trends cho keyword nỗi đau của chính persona này"),
    Metric("persona_need_score", "Điểm nhu cầu của persona", "Persona", norm=lambda x: _clamp(x / 100), fmt="{:.0f}"),
    Metric("persona_need_yoy", "Nhu cầu persona so cùng kỳ", "Persona", norm=lambda x: _clamp((x + 0.2) / 1.2),
           fmt="{:+.0%}"),
    Metric("social_verdict", "Xác minh trên mạng xã hội", "Persona", kind="category", choices=SOCIAL_VERDICTS,
           help="Claude đọc bài viết thật: persona có thật sự nói về nỗi đau này không"),
    Metric("social_score", "Điểm xác thực mạng xã hội", "Persona", norm=lambda x: _clamp(x / 100), fmt="{:.0f}"),
    Metric("social_volume", "Bài liên quan (90 ngày)", "Persona", norm=lambda x: _clamp(math.log10(x + 1) / 2),
           fmt="{:.0f}"),
    Metric("social_growth", "Đà thảo luận (90 ngày gần / trước)", "Persona", norm=lambda x: _clamp((x - 0.8) / 1.2),
           fmt="{:.2f}×"),
    # --- Khoảng trống (cầu organic vs cung ad, cùng bộ angle)
    Metric("demand_share", "Tỉ trọng thảo luận organic (cầu)", "Khoảng trống", norm=lambda x: _clamp(x / 0.3),
           fmt=PCT),
    Metric("gap_opportunity", "Điểm cơ hội (cầu lớn, cung nhỏ)", "Khoảng trống", norm=lambda x: _clamp(x / 100),
           fmt="{:.0f}"),
    Metric("gap_verdict", "Kết luận khoảng trống", "Khoảng trống", kind="category",
           choices=("gap", "fumbled", "proven", "balanced", "oversupplied", "minor", "generic")),
]}

OPS = {">=": "≥", "<=": "≤", "in": "thuộc", "not_in": "không thuộc", "is": "là"}


# ------------------------------------------------------------------ định nghĩa hồ sơ
@dataclass
class Gate:
    metric: str
    op: str                       # >= | <= | in | not_in | is
    value: Any

    def describe(self) -> str:
        m = METRICS.get(self.metric)
        label = m.label if m else self.metric
        if isinstance(self.value, list):
            v = ", ".join(m.show(x) if m else str(x) for x in self.value)
        else:
            v = m.show(self.value) if m else str(self.value)
        return f"{label} {OPS.get(self.op, self.op)} {v}"


@dataclass
class Weight:
    metric: str
    weight: float
    prefer: str = "high"          # high = càng cao càng tốt | low = càng thấp càng tốt


@dataclass
class Profile:
    key: str
    label: str
    description: str = ""
    gates: list[Gate] = field(default_factory=list)
    weights: list[Weight] = field(default_factory=list)
    strong: float = 70.0          # điểm ≥ strong và đạt mọi điều kiện → "Rất hợp"
    fit: float = 50.0             # điểm ≥ fit → "Hợp"
    applies_to: list[str] = field(default_factory=lambda: ["product", "store"])


@dataclass
class EvidenceRule:
    """Store nào được tính là BẰNG CHỨNG cho sản phẩm (góc nhìn sản phẩm)."""
    gates: list[Gate] = field(default_factory=list)


DEFAULT_YAML = """# Hồ sơ chiến lược — sửa trên giao diện (winfinder serve → Hồ sơ) hoặc sửa tay.
# gates: điều kiện bắt buộc. op: >=, <=, in, not_in, is. Thiếu dữ liệu → "chưa rõ" (không tính là trượt).
# weights: prefer high = càng cao càng tốt, low = càng thấp càng tốt. Trọng số tương đối, không cần cộng = 1.

# Store được tính là bằng chứng cho sản phẩm khi đạt các điều kiện này
evidence:
  gates:
    - {metric: archetype, op: in, value: [scaling, early, evergreen]}
    - {metric: direct_share, op: ">=", value: 0.5}

profiles:
  - key: quick
    label: Đánh nhanh
    description: Bắt trend đang lên — vào sớm, chấp nhận sản phẩm chưa chứng minh độ bền lâu dài.
    applies_to: [product, store]
    strong: 70
    fit: 50
    gates:
      - {metric: active_growth, op: ">=", value: 1.5}
      - {metric: trend_label, op: not_in, value: [declining, spike]}
    weights:
      - {metric: active_growth, weight: 3}
      - {metric: trend_score, weight: 2}
      - {metric: need_score, weight: 1}
      - {metric: store_age_days, weight: 1.5, prefer: low}
      - {metric: competitors, weight: 1, prefer: low}
      - {metric: angles_rising, weight: 1}
      - {metric: dup_share, weight: 1}
      - {metric: direct_share, weight: 1}
      - {metric: brand_ratio, weight: 1}
      - {metric: angle_share, weight: 1}

  - key: long
    label: Xây lâu dài
    description: Sản phẩm đã chứng minh có lãi và dễ scale — ad bền, nhu cầu tăng bền vững, không mùa vụ.
    applies_to: [product, store]
    strong: 70
    fit: 50
    gates:
      - {metric: alive_30, op: ">=", value: 2}
      - {metric: trend_label, op: not_in, value: [declining, spike]}
      - {metric: archetype, op: not_in, value: [treadmill, stopped]}
    weights:
      - {metric: survival_30, weight: 3}
      - {metric: alive_60, weight: 2}
      - {metric: turnover, weight: 2, prefer: low}
      - {metric: trend_growth, weight: 2}
      - {metric: need_score, weight: 1}
      - {metric: direct_share, weight: 1.5}
      - {metric: evidence_stores, weight: 1.5}
      - {metric: angles_total, weight: 1}
      - {metric: seasonal_amplitude, weight: 1, prefer: low}
      - {metric: traffic_confirmed, weight: 1}
      - {metric: angle_stores, weight: 1}

  # --- Hồ sơ cho ANGLE: đúng persona, đúng thời điểm, đã xác minh bằng người thật
  - key: angle_now
    label: Angle đúng thời điểm
    description: Angle đang được dồn ad, nhu cầu của chính persona đang tăng, và người thật đang nói về nỗi đau này.
    applies_to: [angle]
    strong: 70
    fit: 50
    gates:
      - {metric: angle_status, op: in, value: [rising, new]}
      - {metric: persona_need_label, op: not_in, value: [declining, spike]}
      - {metric: social_verdict, op: in, value: [confirmed, partial]}
    weights:
      - {metric: active_growth, weight: 2}
      - {metric: angle_share, weight: 1.5}
      - {metric: angle_stores, weight: 1.5}
      - {metric: persona_need_score, weight: 2}
      - {metric: social_score, weight: 2}
      - {metric: social_growth, weight: 1.5}
      - {metric: trend_score, weight: 1}
      - {metric: gap_opportunity, weight: 2}

  - key: angle_durable
    label: Angle bền
    description: Angle có ad sống lâu, persona thật và nhu cầu ổn định — nền để xây dài hạn.
    applies_to: [angle]
    strong: 70
    fit: 50
    gates:
      - {metric: angle_status, op: not_in, value: [churn, stopped, declining]}
      - {metric: social_verdict, op: in, value: [confirmed, partial]}
    weights:
      - {metric: survival_30, weight: 2.5}
      - {metric: alive_30, weight: 2}
      - {metric: social_score, weight: 2}
      - {metric: social_volume, weight: 1.5}
      - {metric: persona_need_score, weight: 1.5}
      - {metric: angle_stores, weight: 1}
      - {metric: seasonal_amplitude, weight: 1, prefer: low}
      - {metric: demand_share, weight: 1.5}
"""


class ProfileError(ValueError):
    pass


def _gate(x: dict) -> Gate:
    g = Gate(str(x["metric"]), str(x["op"]), x.get("value"))
    validate_gate(g)
    return g


def validate_gate(g: Gate) -> None:
    m = METRICS.get(g.metric)
    if not m:
        raise ProfileError(f"Không có chỉ số '{g.metric}'")
    if g.op not in OPS:
        raise ProfileError(f"Phép so sánh không hợp lệ: {g.op}")
    if m.kind == "category":
        if g.op not in ("in", "not_in"):
            raise ProfileError(f"{m.label}: chỉ dùng 'in' / 'not_in'")
        vals = g.value if isinstance(g.value, list) else [g.value]
        bad = [v for v in vals if v not in m.choices]
        if bad or not vals:
            raise ProfileError(f"{m.label}: giá trị không hợp lệ {bad} (chọn trong {', '.join(m.choices)})")
        g.value = list(vals)
    elif m.kind == "bool":
        if g.op != "is":
            raise ProfileError(f"{m.label}: chỉ dùng 'is'")
        g.value = bool(g.value) if not isinstance(g.value, str) else g.value.lower() in ("1", "true", "có", "yes")
    else:
        if g.op not in (">=", "<="):
            raise ProfileError(f"{m.label}: chỉ dùng >= hoặc <=")
        try:
            g.value = float(g.value)
        except (TypeError, ValueError):
            raise ProfileError(f"{m.label}: ngưỡng phải là số") from None


def _profile(x: dict) -> Profile:
    key = str(x.get("key") or "").strip()
    if not key or not key.replace("_", "").replace("-", "").isalnum():
        raise ProfileError(f"Mã hồ sơ không hợp lệ: '{key}' (chỉ chữ, số, - và _)")
    weights = []
    for w in x.get("weights") or []:
        m = METRICS.get(str(w["metric"]))
        if not m or m.norm is None:
            raise ProfileError(f"Không chấm điểm được theo '{w['metric']}' (chỉ dùng chỉ số dạng số)")
        prefer = str(w.get("prefer", "high"))
        if prefer not in ("high", "low"):
            raise ProfileError(f"prefer phải là high hoặc low ({w['metric']})")
        weight = float(w.get("weight", 1))
        if weight < 0:
            raise ProfileError("Trọng số không được âm — dùng prefer: low")
        if weight > 0:
            weights.append(Weight(m.key, weight, prefer))
    applies = [str(k) for k in (x.get("applies_to") or ["product", "store"])]
    bad = [k for k in applies if k not in KINDS]
    if bad or not applies:
        raise ProfileError(f"applies_to không hợp lệ: {bad} (chọn trong {', '.join(KINDS)})")
    return Profile(
        key=key, label=str(x.get("label") or key), description=str(x.get("description") or ""),
        gates=[_gate(g) for g in x.get("gates") or []], weights=weights,
        strong=float(x.get("strong", 70)), fit=float(x.get("fit", 50)), applies_to=applies,
    )


def for_kind(profiles: list[Profile], kind: str) -> list[Profile]:
    """Hồ sơ áp dụng cho loại này; file cũ chưa có hồ sơ nào cho loại này → dùng tất cả (tương thích ngược)."""
    mine = [p for p in profiles if kind in p.applies_to]
    return mine or profiles


def parse(text: str) -> tuple[list[Profile], EvidenceRule]:
    raw = yaml.safe_load(text) or {}
    profiles = [_profile(p) for p in raw.get("profiles") or []]
    keys = [p.key for p in profiles]
    if len(set(keys)) != len(keys):
        raise ProfileError("Mã hồ sơ bị trùng")
    evidence = EvidenceRule([_gate(g) for g in (raw.get("evidence") or {}).get("gates") or []])
    return profiles, evidence


def load(path: str | Path) -> tuple[list[Profile], EvidenceRule]:
    path = Path(path)
    if not path.exists():
        save_text(path, DEFAULT_YAML)
    return parse(path.read_text(encoding="utf-8"))


def dump(profiles: list[Profile], evidence: EvidenceRule) -> str:
    def clean(obj):
        d = asdict(obj)
        return d

    body = {"evidence": {"gates": [clean(g) for g in evidence.gates]},
            "profiles": [clean(p) for p in profiles]}
    head = DEFAULT_YAML.split("\n\n", 1)[0] + "\n\n"
    return head + yaml.safe_dump(body, allow_unicode=True, sort_keys=False)


def save_text(path: Path, text: str) -> None:
    parse(text)  # không bao giờ ghi file hỏng
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".profiles-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save(path: Path, profiles: list[Profile], evidence: EvidenceRule) -> None:
    save_text(path, dump(profiles, evidence))


# ------------------------------------------------------------------ đánh giá
@dataclass
class GateResult:
    gate: str
    status: str                   # pass | fail | unknown
    actual: str


@dataclass
class Contribution:
    metric: str
    label: str
    value: str                    # giá trị thô đã định dạng
    score: float | None           # 0..1 sau khi xét prefer; None = thiếu dữ liệu
    weight: float
    points: float                 # điểm đóng góp trên thang 100


@dataclass
class Evaluation:
    profile: str
    label: str
    score: float                  # 0–100, chỉ tính trên chỉ số có dữ liệu
    coverage: float               # % trọng số có dữ liệu
    decision: str                 # strong | fit | weak | blocked
    decision_vi: str
    gates: list[GateResult]
    contributions: list[Contribution]

    @property
    def unknown_gates(self) -> list[GateResult]:
        return [g for g in self.gates if g.status == "unknown"]

    def to_dict(self) -> dict:
        return asdict(self)


DECISION_VI = {"strong": "Rất hợp", "fit": "Hợp", "weak": "Chưa hợp", "blocked": "Không đạt điều kiện"}


def check_gate(g: Gate, metrics: dict[str, Any]) -> GateResult:
    m = METRICS[g.metric]
    v = metrics.get(g.metric)
    if v is None:
        return GateResult(g.describe(), "unknown", "chưa có dữ liệu")
    if g.op == ">=":
        ok = v >= g.value
    elif g.op == "<=":
        ok = v <= g.value
    elif g.op == "in":
        ok = v in g.value
    elif g.op == "not_in":
        ok = v not in g.value
    else:
        ok = bool(v) == bool(g.value)
    return GateResult(g.describe(), "pass" if ok else "fail", m.show(v))


def evaluate(p: Profile, metrics: dict[str, Any]) -> Evaluation:
    gates = [check_gate(g, metrics) for g in p.gates]
    contribs, total_w, have_w, acc = [], 0.0, 0.0, 0.0
    for w in p.weights:
        m = METRICS[w.metric]
        v = metrics.get(w.metric)
        total_w += w.weight
        if v is None:
            contribs.append(Contribution(m.key, m.label, "–", None, w.weight, 0.0))
            continue
        s = m.norm(v)
        s = 1 - s if w.prefer == "low" else s
        have_w += w.weight
        acc += s * w.weight
        contribs.append(Contribution(m.key, m.label, m.show(v), round(s, 3), w.weight, 0.0))
    score = round(100 * acc / have_w, 1) if have_w else 0.0
    for c in contribs:
        if c.score is not None and have_w:
            c.points = round(100 * c.score * c.weight / have_w, 1)
    if any(g.status == "fail" for g in gates):
        decision = "blocked"
    elif score >= p.strong:
        decision = "strong"
    elif score >= p.fit:
        decision = "fit"
    else:
        decision = "weak"
    label = DECISION_VI[decision]
    if decision != "blocked" and any(g.status == "unknown" for g in gates):
        label += " (cần kiểm tra)"
    return Evaluation(p.key, p.label, score, round(have_w / total_w, 3) if total_w else 0.0,
                      decision, label, gates, contribs)


def passes(rule: EvidenceRule, metrics: dict[str, Any]) -> tuple[bool, list[GateResult]]:
    """Bằng chứng phải ĐẠT rõ ràng — thiếu dữ liệu không tính là đạt."""
    res = [check_gate(g, metrics) for g in rule.gates]
    return all(r.status == "pass" for r in res), res


def best_fit(evals: list[Evaluation]) -> Evaluation | None:
    ok = [e for e in evals if e.decision in ("strong", "fit")]
    return max(ok, key=lambda e: e.score) if ok else None
