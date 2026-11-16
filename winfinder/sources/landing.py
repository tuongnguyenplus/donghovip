"""Loại landing page + lần ngược trang trung gian (advertorial / listicle / quiz) về store gốc.

Ưu tiên đối thủ chạy thẳng PDP: đó là cách nguyên bản nhất chứng minh sản phẩm win
("win thật thì chạy kiểu gì cũng win"), không nhờ phễu presell.

Giới hạn: API Meta chỉ trả domain hiển thị, không trả đường dẫn đầy đủ → chỉ biết ad trỏ thẳng store hay qua
domain trung gian. Phân biệt PDP / trang chủ / collection cần `link_url` (connector như Foreplay có).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from winfinder.http import HttpError, get_text

log = logging.getLogger(__name__)

# điểm "trực tiếp" của từng loại landing (dùng cho chỉ số direct_share)
DIRECTNESS = {
    "pdp": 1.0, "store": 0.8, "collection": 0.7, "home": 0.6,
    "quiz": 0.4, "advertorial": 0.3, "listicle": 0.3, "presell": 0.3,
}
LANDING_VI = {
    "pdp": "PDP", "store": "Thẳng store", "collection": "Collection", "home": "Trang chủ",
    "advertorial": "Advertorial", "listicle": "Listicle", "quiz": "Quiz", "presell": "Trang trung gian",
}
CTA_RE = re.compile(r"\b(buy|shop|order|get (it|yours|\d+%)|check (availability|price)|claim|discount|"
                    r"\d+% off|try|add to cart|see (price|offer)|learn more|official (site|store))\b", re.I)
STORE_MARKERS = re.compile(r'/cart/add|name="add"|add to cart|add-to-cart|addtocart|"@type":\s*"Product"', re.I)
LISTICLE_RE = re.compile(r"\b(top \d+|\d+ (reasons|ways|things|signs|best|products|tips)|best .{0,40} (of|in) 20\d\d)\b",
                         re.I)
QUIZ_RE = re.compile(r"\bquiz\b|take the (quiz|test)|answer \d+ questions", re.I)
IGNORE_HOSTS = re.compile(r"(facebook|instagram|tiktok|youtube|twitter|x|pinterest|google|apple|trustpilot|"
                          r"shopify|cloudflare|klaviyo|gstatic|googleapis|jsdelivr|cdn)\.", re.I)


def host_of(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def classify_url(url: str | None, store_domain: str | None = None) -> str | None:
    """Loại landing từ đường dẫn đầy đủ (khi có link_url)."""
    if not url:
        return None
    u = urlparse(url if "://" in url else "https://" + url)
    host = host_of(u.geturl())
    path = u.path.lower()
    if store_domain and host and host != store_domain and not host.endswith("." + store_domain):
        if QUIZ_RE.search(path):
            return "quiz"
        return "listicle" if LISTICLE_RE.search(path.replace("-", " ")) else "presell"
    if "/products/" in path or re.search(r"/(p|product|item)/[^/]+", path):
        return "pdp"
    if "/collections/" in path or "/category/" in path:
        return "collection"
    if QUIZ_RE.search(path):
        return "quiz"
    if re.search(r"/(pages|blogs|articles|blog|news|story)/", path):
        return "listicle" if LISTICLE_RE.search(path.replace("-", " ")) else "advertorial"
    if path in ("", "/"):
        return "home"
    return "store"


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []
        self.title = ""
        self._in_title = False
        self.h1: list[str] = []
        self._in_h1 = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._href, self._text = a["href"], [a.get("title") or ""]
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._href:
            self._text.append(data)
        if self._in_title:
            self.title += data
        if self._in_h1:
            self.h1.append(data.strip())


@dataclass
class Resolution:
    kind: str                    # store | presell | unknown
    resolves_to: str | None
    landing_type: str | None     # advertorial | listicle | quiz | presell (khi kind = presell)


def analyse_html(base_url: str, raw: str, exclude: set[str] = frozenset()) -> Resolution:
    """Trang này là store, hay trang trung gian dẫn về store nào?"""
    me = host_of(base_url)
    if STORE_MARKERS.search(raw):
        return Resolution("store", None, None)
    p = _Links()
    try:
        p.feed(raw)
    except Exception:
        pass
    scores: Counter[str] = Counter()
    for href, text in p.links:
        url = urljoin(base_url, href)
        h = host_of(url)
        if not h or h == me or h.endswith("." + me) or IGNORE_HOSTS.search(h + "."):
            continue
        if any(h == x or h.endswith("." + x) for x in exclude):
            continue
        w = 1.0
        if CTA_RE.search(text):
            w += 2
        if "/products/" in url or "/cart" in url or "checkout" in url:
            w += 2
        scores[h] += w
    if not scores:
        return Resolution("unknown", None, None)
    target, score = scores.most_common(1)[0]
    if score < 3:  # 1–2 link lạc thì không đủ để kết luận
        return Resolution("unknown", None, None)
    heading = f"{p.title} {' '.join(p.h1)}"
    kind = "quiz" if QUIZ_RE.search(raw[:20000]) else ("listicle" if LISTICLE_RE.search(heading) else "advertorial")
    return Resolution("presell", target, kind)


def resolve(session: requests.Session, domain: str, exclude: set[str] = frozenset()) -> Resolution:
    """Domain hiển thị trên ad → store hay trang trung gian (và store đích)."""
    try:
        data = get_text(session, f"https://{domain}/products.json", {"limit": 1}, retries=0, timeout=15)
        if '"products"' in data[:200]:
            return Resolution("store", None, None)
    except (HttpError, requests.RequestException):
        pass
    try:
        raw = get_text(session, f"https://{domain}/", retries=1, timeout=20)
    except (HttpError, requests.RequestException) as exc:
        log.info("Không đọc được %s: %s", domain, exc)
        return Resolution("unknown", None, None)
    return analyse_html(f"https://{domain}/", raw, exclude)
