"""Đọc trang sản phẩm của store để biết sản phẩm THỰC SỰ là gì (không chỉ dựa vào tên brand).

Nguồn, theo thứ tự ưu tiên:
  1. Shopify `/products.json`: tên, mô tả (body_html), product_type, tags của mọi sản phẩm.
  2. Trang HTML (trang sản phẩm hero, hoặc trang chủ): <title>, meta/og description,
     JSON-LD Product (name, description, category), h1/h2 và phần chữ chính.

Hero product = sản phẩm khớp nhiều từ nhất với tiêu đề/nội dung ad (tính cả tên brand,
vì ad của 'LiftEyes' nhắc 'LiftEyes' chứ không nhắc 'red light glasses').
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser

import requests

from winfinder.http import HttpError, get_text

log = logging.getLogger(__name__)

# sản phẩm phụ của Shopify — không bao giờ là hero
JUNK_PRODUCTS = re.compile(
    r"gift ?card|shipping protection|insurance|route|package protection|tip|donation|e-?gift|warranty|priority",
    re.I,
)
MAX_TEXT = 4000


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return re.sub(r"\n+", "\n", s).strip()


@dataclass
class PageInfo:
    url: str
    title: str = ""
    description: str = ""          # meta / og description
    product_name: str = ""         # JSON-LD Product.name hoặc og:title
    product_type: str = ""         # Shopify product_type hoặc JSON-LD category
    tags: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    text: str = ""                 # chữ chính (đã cắt ngắn)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PageInfo":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def as_prompt(self) -> str:
        parts = [f"URL: {self.url}"]
        for label, val in (
            ("Page title", self.title), ("Product name", self.product_name), ("Product type", self.product_type),
            ("Tags", ", ".join(self.tags)), ("Meta description", self.description),
            ("Headings", " | ".join(self.headings[:12])),
        ):
            if val:
                parts.append(f"{label}: {val}")
        if self.text:
            parts.append(f"Page text:\n{self.text[:MAX_TEXT]}")
        return "\n".join(parts)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.headings: list[str] = []
        self.jsonld: list[str] = []
        self.text: list[str] = []
        self._stack: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        if tag in ("title", "h1", "h2", "script", "style", "noscript", "svg"):
            self._stack.append(tag if tag != "script" or a.get("type") == "application/ld+json" else "skip")
            self._buf = []

    def handle_endtag(self, tag):
        if not self._stack:
            return
        top = self._stack[-1]
        if (tag == top) or (tag == "script" and top == "skip"):
            self._stack.pop()
            content = "".join(self._buf).strip()
            if top == "title":
                self.title = content
            elif top in ("h1", "h2") and content:
                self.headings.append(re.sub(r"\s+", " ", content))
            elif top == "script" and content:
                self.jsonld.append(content)
            self._buf = []

    def handle_data(self, data):
        if self._stack:
            if self._stack[-1] in ("title", "h1", "h2", "script"):
                self._buf.append(data)
            if self._stack[-1] in ("style", "noscript", "svg", "skip", "script"):
                return
        t = data.strip()
        if len(t) > 2:
            self.text.append(t)


def _jsonld_products(blocks: list[str]) -> list[dict]:
    out = []

    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            types = x.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" in types:
                out.append(x)
            for v in x.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for b in blocks:
        try:
            walk(json.loads(b))
        except ValueError:
            continue
    return out


def parse_page(url: str, raw_html: str) -> PageInfo:
    p = _PageParser()
    try:
        p.feed(raw_html)
    except Exception as exc:  # HTML hỏng không được làm hỏng cả run
        log.debug("parse %s: %s", url, exc)
    products = _jsonld_products(p.jsonld)
    prod = products[0] if products else {}
    category = prod.get("category") or ""
    if isinstance(category, dict):
        category = category.get("name", "")
    text = re.sub(r"\s+", " ", " ".join(p.text))
    return PageInfo(
        url=url,
        title=html.unescape(p.title),
        description=p.meta.get("og:description") or p.meta.get("description") or "",
        product_name=str(prod.get("name") or p.meta.get("og:title") or ""),
        product_type=str(category),
        headings=p.headings[:20],
        text=(html_to_text(str(prod.get("description") or "")) + "\n" + text).strip()[:MAX_TEXT],
    )


def fetch_page(session: requests.Session, url: str) -> PageInfo | None:
    try:
        return parse_page(url, get_text(session, url, retries=1, timeout=20))
    except (HttpError, requests.RequestException) as exc:
        log.warning("Không đọc được %s: %s", url, exc)
        return None


# ------------------------------------------------------------------ hero product
def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 2}


def choose_hero(products: list[dict], ad_texts: list[str]) -> dict | None:
    """Chọn sản phẩm đang được chạy ad: khớp nhiều từ nhất với ad; hoà → mới nhất."""
    candidates = [p for p in products if p.get("title") and not JUNK_PRODUCTS.search(p["title"])]
    if not candidates:
        return None
    ad_tok = set().union(*(_tokens(t) for t in ad_texts)) if ad_texts else set()

    def score(p: dict) -> tuple[int, str]:
        name_tok = _tokens(p["title"]) | _tokens(p.get("handle", "").replace("-", " "))
        return (len(name_tok & ad_tok), p.get("created_at") or "")

    best = max(candidates, key=score)
    if score(best)[0] == 0 and len(candidates) > 3:
        return None  # store nhiều sản phẩm mà không khớp ad → không đoán bừa
    return best


def page_from_shopify(domain: str, product: dict) -> PageInfo:
    return PageInfo(
        url=f"https://{domain}/products/{product.get('handle', '')}",
        product_name=product.get("title") or "",
        product_type=product.get("product_type") or "",
        tags=list(product.get("tags") or [])[:20],
        text=(product.get("description") or "")[:MAX_TEXT],
    )
