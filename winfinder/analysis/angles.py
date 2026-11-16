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

from winfinder.analysis.durability import AdLife, DurabilityRules, measure

log = logging.getLogger(__name__)

MAX_UNITS = 150          # số đơn vị ad tối đa gửi cho Claude / sản phẩm
UNIT_CHARS = 320
HOOK_TYPES = ["problem-solution", "testimonial", "social-proof", "gift", "demonstration", "before-after",
              "fear", "curiosity", "offer", "authority", "comparison", "lifestyle", "other"]

ANGLE_LABELS = {
    "rising": "Đang lên",
    "new": "Mới xuất hiện",
    "steady": "Ổn định",
    "churn": "Creative chết nhanh",
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
                    "pain_keywords": {
                        "type": "array", "items": {"type": "string"},
                        "description": "1-3 Google searches (English, lowercase, no brand) THIS persona types about "
                                       "THIS pain/desire, e.g. 'back pain from sitting'. Not the product name.",
                    },
                    "social_queries": {
                        "type": "array", "items": {"type": "string"},
                        "description": "2-4 short phrases (English) this persona would write in a Reddit post or "
                                       "TikTok caption about the pain, e.g. 'desk job ruined my posture'.",
                    },
                },
                "required": ["id", "name", "persona", "pain_point", "promise", "hook_type", "pain_keywords",
                             "social_queries"],
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
Quote nothing; describe. Exception: pain_keywords and social_queries are in English, in the words real people
from that persona use (they are used to measure demand on Google Trends and to find real posts on social media)."""


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
    pain_keywords: list[str] = field(default_factory=list)    # tiếng Anh — đo nhu cầu của persona (Trends)
    social_queries: list[str] = field(default_factory=list)   # tiếng Anh — tìm bài viết thật trên mạng xã hội


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
            meta[r["ad_id"]] = {"start": r["start_date"], "stop": r["stop_date"], "store": key,
                                "reach": r["eu_total_reach"] or 0}
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


SCHEMA_VERSION = "2"  # đổi khi SCHEMA đổi → phân tích lại (kết quả cũ thiếu trường mới)


def fingerprint(product: str, units: list[AdUnit]) -> str:
    h = hashlib.sha256((SCHEMA_VERSION + product).encode())
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
    def clean(xs) -> list[str]:
        return list(dict.fromkeys(" ".join(str(x).lower().split()) for x in xs or [] if str(x).strip()))[:4]

    angles = [AngleDef(**{k: a[k] for k in AngleDef.__dataclass_fields__ if k in a})
              for a in data.get("angles", [])]
    for a in angles:
        a.pain_keywords, a.social_queries = clean(a.pain_keywords), clean(a.social_queries)
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
    history: list[int]           # ad ĐANG SỐNG / tuần, 12 tuần
    recent_share: float          # tỉ trọng trong số ad đang sống hiện tại
    growth_ratio: float          # đà tăng ad đang sống
    first_seen: str | None
    survival_30: float | None = None
    alive_30: int = 0
    samples: list[str] = field(default_factory=list)


def angle_status(arch: str, first_seen: date | None, as_of: date, product_first: date | None,
                 active_now: int) -> str:
    """Trạng thái angle từ dạng tăng trưởng (cùng thước đo store).

    'new' chỉ khi angle xuất hiện ≤ 3 tuần nay VÀ sản phẩm đã chạy ad từ trước đó ≥ 3 tuần
    (store mới mở thì mọi angle đều 'mới' — không có ý nghĩa)."""
    if active_now == 0:
        return "stopped"
    if (first_seen and (as_of - first_seen).days <= 21
            and product_first and (first_seen - product_first).days >= 21):
        return "new"
    return {"scaling": "rising", "early": "rising", "treadmill": "churn", "declining": "declining",
            "stopped": "stopped"}.get(arch, "steady")


def angle_stats(result: AngleResult, units: list[AdUnit], meta: dict[str, dict], as_of: date,
                rules: DurabilityRules | None = None) -> list[AngleStats]:
    by_angle: dict[str, list[AdUnit]] = {a.id: [] for a in result.angles}
    for u in units:
        aid = result.assignments.get(u.ref)
        if aid in by_angle:
            by_angle[aid].append(u)

    def d(s):
        return date.fromisoformat(s) if s else None

    tmp = []
    for a in result.angles:
        us = by_angle[a.id]
        lives = [AdLife(d(meta[i]["start"]), d(meta[i].get("stop")), u.text)
                 for u in us for i in u.ad_ids if meta.get(i, {}).get("start")]
        if not lives:
            continue
        tmp.append((a, us, lives, measure(lives, as_of, 12, rules)))

    total_active = sum(m.active_now for *_, m in tmp)
    firsts = [min(x.start for x in lives) for _, _, lives, _ in tmp]
    product_first = min(firsts) if firsts else None
    out = []
    for a, us, lives, m in tmp:
        first = min(x.start for x in lives)
        st = angle_status(m.archetype, first, as_of, product_first, m.active_now)
        out.append(AngleStats(
            angle=a, status=st, status_vi=ANGLE_LABELS[st], ads=len(lives),
            stores=sorted({s for u in us for s in u.stores}), reach=sum(u.reach for u in us),
            history=m.active_history,
            recent_share=round(m.active_now / total_active, 3) if total_active else 0.0,
            growth_ratio=m.active_growth, first_seen=first.isoformat(),
            survival_30=m.survival_30, alive_30=m.alive_30,
            samples=[u.text for u in sorted(us, key=lambda u: -u.reach)[:2]],
        ))
    order = {"rising": 0, "new": 1, "steady": 2, "churn": 3, "declining": 4, "stopped": 5}
    out.sort(key=lambda s: (order[s.status], -s.recent_share))
    return out
