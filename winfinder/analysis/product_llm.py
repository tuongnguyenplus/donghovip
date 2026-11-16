"""Hiểu sản phẩm bằng Claude: đọc ad + trang sản phẩm → tên gọi chung + keyword người mua thực sự tìm.

Giải quyết trường hợp tên sản phẩm là brand ('LiftEyes') không nói lên sản phẩm là gì
('red light therapy glasses' trị 'eye bags').

Cần ANTHROPIC_API_KEY. Không có key / SDK → link.py dùng heuristic (analysis/keywords.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "generic_product": {
            "type": "string",
            "description": "What the product is, in plain generic English, no brand. e.g. 'red light therapy glasses'",
        },
        "category": {"type": "string"},
        "search_keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-6 generic Google searches a shopper would type to find THIS kind of product. "
                           "No brand names. Most searched first.",
        },
        "problem_keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "0-4 searches describing the need/problem the product solves, e.g. 'eye bags', "
                           "'puffy eyes'. Empty if the product is not problem-driven.",
        },
        "target_persona": {"type": "string", "description": "Who the ads target, one short phrase."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["generic_product", "category", "search_keywords", "problem_keywords", "target_persona",
                 "confidence"],
    "additionalProperties": False,
}

SYSTEM = """You identify e-commerce products for market research.

Given a store's ad copy and its product page, work out what the product actually is, even when it is sold
under a made-up brand name. Then list the generic Google searches a US shopper would type to find this kind
of product, and the searches describing the problem it solves.

Rules for keywords:
- Lowercase English, 1-4 words, no brand or store names, no adjectives like "best" or "cheap".
- They must be phrases real people search, so they work in Google Trends (prefer "red light therapy glasses"
  over "630nm photobiomodulation eyewear").
- search_keywords describe the product; problem_keywords describe the need. Don't mix them.
- If the page and ads disagree, trust the product page.
- If the information is too thin to tell, give your best guess and set confidence to "low"."""


@dataclass
class ProductContext:
    domain: str | None
    page_names: list[str]
    ad_titles: list[str]
    ad_bodies: list[str]
    page: dict | None = None                 # PageInfo.to_dict()
    niche_keywords: list[str] = field(default_factory=list)

    def prompt(self) -> str:
        from winfinder.sources.product_page import PageInfo

        lines = [f"Store: {self.domain or '(no website)'} — Facebook page: {', '.join(self.page_names) or '-'}"]
        if self.niche_keywords:
            lines.append(f"Found while searching ads for: {', '.join(self.niche_keywords)}")
        lines.append("\nAd headlines:")
        lines += [f"- {t}" for t in self.ad_titles[:12]]
        if self.ad_bodies:
            lines.append("\nAd copy samples:")
            lines += [f"- {b[:400]}" for b in self.ad_bodies[:6]]
        if self.page:
            lines.append("\nProduct page:\n" + PageInfo.from_dict(self.page).as_prompt())
        else:
            lines.append("\nProduct page: not available")
        return "\n".join(lines)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.prompt().encode()).hexdigest()[:16]


@dataclass
class ProductInsight:
    generic_product: str
    category: str
    search_keywords: list[str]
    problem_keywords: list[str]
    target_persona: str
    confidence: str
    model: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProductInsight":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class ProductExtractor(Protocol):
    model: str

    def extract(self, ctx: ProductContext) -> ProductInsight | None: ...


def _clean(kws: list[str], limit: int) -> list[str]:
    out: list[str] = []
    for k in kws:
        k = " ".join(str(k).lower().split())
        if k and k not in out and len(k.split()) <= 5:
            out.append(k)
    return out[:limit]


class ClaudeProductExtractor:
    """Một request / sản phẩm, structured output theo SCHEMA."""

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low", client=None):
        import anthropic  # chỉ import khi thực sự dùng

        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic()
        self._anthropic = anthropic

    def extract(self, ctx: ProductContext) -> ProductInsight | None:
        a = self._anthropic
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=2000,
                system=SYSTEM,
                messages=[{"role": "user", "content": ctx.prompt()}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                # bị bộ lọc an toàn từ chối → server tự chạy lại trên model dự phòng
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except a.RateLimitError as exc:
            log.warning("Claude rate limit: %s", exc)
            return None
        except a.APIStatusError as exc:
            log.error("Claude API lỗi %s: %s", exc.status_code, exc.message)
            return None
        except a.APIConnectionError as exc:
            log.error("Không kết nối được Claude API: %s", exc)
            return None
        if resp.stop_reason == "refusal":
            log.warning("Claude từ chối phân tích %s", ctx.domain)
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            log.error("Claude trả về JSON không hợp lệ cho %s", ctx.domain)
            return None
        return ProductInsight(
            generic_product=" ".join(data["generic_product"].lower().split()),
            category=data["category"],
            search_keywords=_clean(data["search_keywords"], 6),
            problem_keywords=_clean(data["problem_keywords"], 4),
            target_persona=data["target_persona"],
            confidence=data["confidence"],
            model=resp.model,
        )


def make_extractor(enabled: bool, model: str, effort: str) -> ProductExtractor | None:
    import os

    if not enabled:
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log.warning("Chưa có ANTHROPIC_API_KEY — rút keyword sản phẩm bằng heuristic (kém chính xác hơn)")
        return None
    try:
        return ClaudeProductExtractor(model=model, effort=effort)
    except ImportError:
        log.warning("Chưa cài thư viện 'anthropic' — dùng heuristic")
        return None
