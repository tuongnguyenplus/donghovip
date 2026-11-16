"""Làm giàu dữ liệu domain: ngày đăng ký (RDAP), SSL đầu tiên (crt.sh), Shopify products.json."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import requests

from winfinder.http import HttpError, get_json
from winfinder.sources.product_page import html_to_text

log = logging.getLogger(__name__)


def registrable_domain(domain: str) -> str:
    """Rút gọn subdomain: 'shop.example.com' → 'example.com'. Đơn giản, đủ cho phần lớn TLD .com/.co/.store.

    Với TLD 2 cấp (vd .co.uk, .com.au) giữ 3 nhãn cuối.
    """
    parts = domain.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "net", "org"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def rdap_registration_date(session: requests.Session, domain: str) -> str | None:
    data = get_json(session, f"https://rdap.org/domain/{registrable_domain(domain)}", retries=2)
    for ev in data.get("events", []):
        if ev.get("eventAction") == "registration" and ev.get("eventDate"):
            return ev["eventDate"][:10]
    return None


def first_certificate_date(session: requests.Session, domain: str) -> str | None:
    """Ngày cấp SSL sớm nhất cho domain (≈ ngày store thật sự lên sóng)."""
    data = get_json(session, "https://crt.sh/", {"q": domain, "output": "json"}, retries=2, timeout=60)
    dates = [row["not_before"][:10] for row in data or [] if row.get("not_before")]
    return min(dates) if dates else None


@dataclass
class ShopifyInfo:
    is_shopify: bool
    product_count: int = 0
    products: list[dict[str, Any]] = field(default_factory=list)


def shopify_products(session: requests.Session, domain: str, limit: int = 250) -> ShopifyInfo:
    """Đọc /products.json công khai của store Shopify (nếu có)."""
    try:
        data = get_json(session, f"https://{domain}/products.json", {"limit": limit}, retries=1)
    except (HttpError, requests.RequestException, ValueError):
        return ShopifyInfo(is_shopify=False)
    if not isinstance(data, dict) or "products" not in data:
        return ShopifyInfo(is_shopify=False)
    products = []
    for p in data["products"]:
        prices = [float(v["price"]) for v in p.get("variants", []) if v.get("price")]
        tags = p.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        products.append(
            {
                "title": p.get("title"),
                "handle": p.get("handle"),
                "created_at": (p.get("created_at") or "")[:10] or None,
                "min_price": min(prices) if prices else None,
                "max_price": max(prices) if prices else None,
                "product_type": p.get("product_type") or "",
                "tags": tags[:20],
                # mô tả sản phẩm (đã bỏ HTML, cắt ngắn) — nguồn chính để hiểu sản phẩm là gì
                "description": html_to_text(p.get("body_html"))[:1500],
            }
        )
    return ShopifyInfo(is_shopify=True, product_count=len(products), products=products)
