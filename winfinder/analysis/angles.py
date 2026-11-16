"""Angle / persona: một sản phẩm được bán bằng nhiều angle, mỗi angle nhắm một tệp khách.

Với mỗi sản phẩm (gộp mọi store cùng bán):
  1. Gom nội dung ad (tiêu đề + nội dung), bỏ trùng → "đơn vị ad".
  2. Claude chia các đơn vị thành 3–8 angle (persona, nỗi đau, lời hứa, kiểu hook), gán từng đơn vị,
     và đề xuất angle chưa ai khai thác.
  3. Đo tăng trưởng từng angle bằng ngày bắt đầu chạy của ad (cùng cách đo creative velocity):
     angle nào đang được nhiều store đổ creative mới → đang thắng; angle nào đã dừng → đã thua.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Protocol

from winfinder.analysis.velocity import growth, weekly_counts

log = logging.getLogger(__name__)

MAX_UNITS = 150          # số đơn vị ad tối đa gửi cho Claude / sản phẩm
UNIT_CHARS = 320
HOOK_TYPES = ["problem-solution", "testimonial", "social-proof", "gift", "demonstration", "before-after",
              "fear", "curiosity", "offer", "authority", "comparison", "lifestyle", "other"]

ANGLE_LABELS = {
    "rising": "Đang lên",
    "new": "Mới xuất hiện",
    "steady": "Ổn định",
    "declining": "Đang giảm",
    "stopped": "Đã dừng",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "angles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "A1, A2, ..."},
                    "name": {"type": "string", "description": "Tên angle ngắn gọn, tiếng Việt"},
                    "persona": {"type": "string", "description": "Tệp khách nhắm tới, tiếng Việt"},
                    "pain_point": {"type": "string", "description": "Nỗi đau / mong muốn chạm tới, tiếng Việt"},
                    "promise": {"type": "string", "description": "Lời hứa chính của ad, tiếng Việt"},
                    "hook_type": {"type": "string", "enum": HOOK_TYPES},
                },
                "required": ["id", "name", "persona", "pain_point", "promise", "hook_type"],
                "additionalProperties": False,
            },
        },
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ad": {"type": "string"}, "angle": {"type": "string"}},
                "required": ["ad", "angle"],
                "additionalProperties": False,
            },
        },
        "untapped_angles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "persona": {"type": "string"},
                    "why": {"type": "string", "description": "Vì sao angle này có tiềm năng, tiếng Việt"},
                },
                "required": ["name", "persona", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["angles", "assignments", "untapped_angles"],
    "additionalProperties": False,
}

SYSTEM = """You are a direct-response ad strategist analysing competitor Facebook ads for one product.

An "angle" is the specific reason-to-buy an ad sells: who it speaks to (persona), which pain or desire it
touches, and what it promises. The same product is often sold with several angles at once, e.g. a posture
corrector sold as "relief for desk workers' back pain", "confidence / look taller" or "a gift for mom".

Tasks:
1. Group the ads into 3-8 distinct angles. Merge near-duplicates; don't create an angle for a single odd ad
   unless it is clearly different. Generic ads with no clear angle (only brand + discount) go to one
   "offer" angle.
2. Assign EVERY ad id to exactly one angle id.
3. Suggest 2-4 promising angles (personas/pains) that none of these ads use yet, realistic for this product.

Write angle names, personas, pains, promises and suggestions in Vietnamese, concise (under 15 words each).
Quote nothing; describe."""


@dataclass
class AdUnit:
    ref: str                       # a1, a2...
    text: str
    ad_ids: list[str]
    stores: list[str]
    first_start: str | None
    reach: int


@dataclass
class AngleDef:
    id: str
    name: str
    persona: str
    pain_point: str
    promise: str
    hook_type: str


@dataclass
class AngleResult:
    angles: list[AngleDef]
    assignments: dict[str, str]          # ad ref → angle id
    untapped: list[dict]
    model: str = ""

    def to_dict(self) -> dict:
        return {"angles": [asdict(a) for a in self.angles], "assignments": self.assignments,
                "untapped": self.untapped, "model": self.model}

    @classmethod
    def from_dict(cls, d: dict) -> "AngleResult":
        return cls([AngleDef(**a) for a in d["angles"]], dict(d["assignments"]), list(d["untapped"]),
                   d.get("model", ""))


class AngleAnalyzer(Protocol):
    model: str

    def analyze(self, product: str, generic: str | None, units: list[AdUnit]) -> AngleResult | None: ...


# ------------------------------------------------------------------ gom đơn vị ad
def ad_units(conn: sqlite3.Connection, store_keys: list[str]) -> tuple[list[AdUnit], dict[str, dict]]:
    """Đơn vị ad (nội dung đã bỏ trùng) cho các store + meta từng ad (ngày bắt đầu, store)."""
    meta: dict[str, dict] = {}
    by_text: dict[str, dict] = {}
    for key in store_keys:
        if key.startswith("page:"):
            rows = conn.execute("SELECT * FROM ads WHERE domain IS NULL AND page_id = ?", (key[5:],)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ads WHERE domain = ?", (key,)).fetchall()
        for r in rows:
            title = (json.loads(r["titles"] or "[]") or [""])[0]
            body = (json.loads(r["bodies"] or "[]") or [""])[0]
            text = " | ".join(t for t in (title.strip(), body.strip()) if t)[:UNIT_CHARS]
            if not text:
                continue
            meta[r["ad_id"]] = {"start": r["start_date"], "store": key, "reach": r["eu_total_reach"] or 0}
            u = by_text.setdefault(text.lower(), {"text": text, "ads": [], "stores": set(), "starts": [], "reach": 0})
            u["ads"].append(r["ad_id"])
            u["stores"].add(key)
            if r["start_date"]:
                u["starts"].append(r["start_date"])
            u["reach"] += r["eu_total_reach"] or 0
    # ưu tiên đơn vị nhiều ad + mới nhất khi phải cắt bớt
    ordered = sorted(by_text.values(), key=lambda u: (len(u["ads"]), max(u["starts"] or [""])), reverse=True)
    units = [
        AdUnit(f"a{i}", u["text"], u["ads"], sorted(u["stores"]), min(u["starts"]) if u["starts"] else None, u["reach"])
        for i, u in enumerate(ordered[:MAX_UNITS], 1)
    ]
    return units, meta


def fingerprint(product: str, units: list[AdUnit]) -> str:
    h = hashlib.sha256(product.encode())
    for u in sorted(units, key=lambda u: u.text):
        h.update(u.text.encode())
    return h.hexdigest()[:16]


# ------------------------------------------------------------------ Claude
class ClaudeAngleAnalyzer:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", client=None):
        import anthropic

        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic()
        self._anthropic = anthropic

    def analyze(self, product: str, generic: str | None, units: list[AdUnit]) -> AngleResult | None:
        a = self._anthropic
        listing = "\n".join(f"{u.ref}: {u.text}" for u in units)
        prompt = (f"Product: {generic or product} (search keyword: {product})\n"
                  f"{len(units)} distinct ads from competing stores:\n\n{listing}")
        try:
            # nhiều ad → output dài; stream để tránh timeout HTTP
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=32000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                resp = stream.get_final_message()
        except a.APIStatusError as exc:
            log.error("Claude API lỗi %s khi phân tích angle '%s': %s", exc.status_code, product, exc.message)
            return None
        except a.APIConnectionError as exc:
            log.error("Không kết nối được Claude API: %s", exc)
            return None
        if resp.stop_reason in ("refusal", "max_tokens"):
            log.warning("Phân tích angle '%s' dừng: %s", product, resp.stop_reason)
            return None
        try:
            data = json.loads(next(b.text for b in resp.content if b.type == "text"))
        except (StopIteration, ValueError):
            log.error("Claude trả về JSON không hợp lệ (angle '%s')", product)
            return None
        return parse_result(data, units, resp.model)


def parse_result(data: dict, units: list[AdUnit], model: str = "") -> AngleResult:
    angles = [AngleDef(**{k: a[k] for k in AngleDef.__dataclass_fields__}) for a in data.get("angles", [])]
    ids = {a.id for a in angles}
    refs = {u.ref for u in units}
    assignments = {x["ad"]: x["angle"] for x in data.get("assignments", []) if x["ad"] in refs and x["angle"] in ids}
    return AngleResult(angles, assignments, list(data.get("untapped_angles", [])), model)


def make_angle_analyzer(enabled: bool, model: str, effort: str) -> AngleAnalyzer | None:
    import os

    if not enabled:
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log.warning("Chưa có ANTHROPIC_API_KEY — bỏ qua phân tích angle")
        return None
    try:
        return ClaudeAngleAnalyzer(model=model, effort=effort)
    except ImportError:
        return None


# ------------------------------------------------------------------ đo tăng trưởng angle
@dataclass
class AngleStats:
    angle: AngleDef
    status: str
    status_vi: str
    ads: int
    stores: list[str]
    reach: int
    history: list[int]           # ad mới / tuần, 12 tuần
    recent_share: float          # tỉ trọng trong ad mới 4 tuần gần nhất
    growth_ratio: float
    first_seen: str | None
    samples: list[str] = field(default_factory=list)


def angle_status(history: list[int], first_seen: date | None, as_of: date, window: int,
                 product_first: date | None = None) -> str:
    """'new' chỉ khi angle xuất hiện ≤ 3 tuần nay VÀ sản phẩm đã chạy ad từ trước đó ≥ 3 tuần
    (store mới mở thì mọi angle đều 'mới' — không có ý nghĩa)."""
    g = growth(history[-window:])
    if (first_seen and (as_of - first_seen).days <= 21 and g.total > 0
            and product_first and (first_seen - product_first).days >= 21):
        return "new"
    if not g.recent:
        return "stopped"
    if g.growth_ratio >= 1.5 and g.slope > 0:
        return "rising"
    if g.growth_ratio <= 0.67 and g.slope < 0:
        return "declining"
    return "steady"


def angle_stats(result: AngleResult, units: list[AdUnit], meta: dict[str, dict], as_of: date,
                window: int = 6) -> list[AngleStats]:
    by_angle: dict[str, list[AdUnit]] = {a.id: [] for a in result.angles}
    for u in units:
        aid = result.assignments.get(u.ref)
        if aid in by_angle:
            by_angle[aid].append(u)

    recent_total = 0
    tmp = []
    for a in result.angles:
        us = by_angle[a.id]
        ad_ids = [i for u in us for i in u.ad_ids]
        starts = [date.fromisoformat(meta[i]["start"]) for i in ad_ids if meta.get(i, {}).get("start")]
        hist = weekly_counts(starts, as_of, 12)
        recent = sum(hist[-4:])
        recent_total += recent
        first = min(starts) if starts else None
        tmp.append((a, us, ad_ids, hist, recent, first))

    firsts = [f for *_, f in tmp if f]
    product_first = min(firsts) if firsts else None
    out = []
    for a, us, ad_ids, hist, recent, first in tmp:
        if not ad_ids:
            continue
        st = angle_status(hist, first, as_of, window, product_first)
        out.append(AngleStats(
            angle=a, status=st, status_vi=ANGLE_LABELS[st], ads=len(ad_ids),
            stores=sorted({s for u in us for s in u.stores}), reach=sum(u.reach for u in us),
            history=hist, recent_share=round(recent / recent_total, 3) if recent_total else 0.0,
            growth_ratio=round(growth(hist[-window:]).growth_ratio, 2),
            first_seen=first.isoformat() if first else None,
            samples=[u.text for u in sorted(us, key=lambda u: -u.reach)[:2]],
        ))
    order = {"rising": 0, "new": 1, "steady": 2, "declining": 3, "stopped": 4}
    out.sort(key=lambda s: (order[s.status], -s.recent_share))
    return out
