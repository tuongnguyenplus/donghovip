"""Dữ liệu giả lập đúng định dạng Ad Library API — để test và xem thử dashboard khi chưa có token."""

from __future__ import annotations

import random
import zlib
from datetime import date, timedelta
from typing import Any

# (domain, page_name, số creative mới mỗi tuần — cũ → mới, các nước, reach/ad, search, tiêu đề ad, sản phẩm Shopify)
PATTERNS: list[tuple] = [
    ("postureglow.com", "PostureGlow", [0, 0, 1, 1, 1, 2, 2, 3, 4, 5, 6, 8], ["IE", "NL", "DE"], 9000, "posture",
     ["PostureGlow™ Smart Posture Corrector", "Posture Corrector — 50% OFF today", "The posture corrector doctors recommend"],
     ["PostureGlow™ Smart Posture Corrector 2.0 - Adjustable", "Gift Card", "Shipping Protection"]),
    ("spinealign.co", "SpineAlign", [0, 0, 0, 2, 3, 4, 5, 5, 6, 6, 7, 7], ["IE", "DK"], 4000, "posture",
     ["Posture Corrector Pro for desk workers", "SpineAlign Posture Corrector"], []),
    ("backbuddy.store", "BackBuddy", [4] + [1] * 11, ["IE", "NL", "DE", "FR"], 15000, "posture",
     ["Adjustable Back Straightener", "Back Straightener — free shipping"], ["Back Straightener (Black)"]),
    ("starrynight.shop", "StarryNight", [0, 0, 4, 3, 2, 0, 0, 0, 0, 0, 0, 0], ["IE"], 1200, "posture",
     ["Galaxy Star Projector", "Star Projector for bedroom"], []),
    ("furrygroom.com", "FurryGroom", [0] * 6 + [1, 1, 2, 3, 4, 6], ["IE", "SE"], 6000, "pet-grooming",
     ["Cat Hair Remover — reusable", "Cat Hair Remover Roller", "The #1 cat hair remover"], ["Cat Hair Remover Roller"]),
    ("petlint.io", "PetLint", [1, 0, 2, 1, 0, 1, 2, 0, 1, 1, 0, 1], ["NL"], 800, "pet-grooming",
     ["Heated Dog Vest", "Heated dog vest for winter walks"], ["Heated Dog Vest - USB"]),
    (None, "Groomy Paws", [0] * 8 + [2, 2, 2, 2], ["IE"], 2500, "pet-grooming",
     ["Self Cleaning Slicker Brush", "Slicker brush for long hair dogs"], []),
    # Tên sản phẩm chỉ là brand — tiêu đề ad không nói sản phẩm là gì
    ("lifteyes.co", "LiftEyes", [0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 5, 7], ["IE", "NL", "DK"], 8000, "eye-care",
     ["LiftEyes™ — wake up looking 10 years younger", "LiftEyes: 10 minutes a day", "Why everyone loves LiftEyes"],
     [{"title": "LiftEyes™ Pro", "handle": "lifteyes-pro", "product_type": "Red Light Therapy Glasses",
       "tags": ["eye bags", "red light therapy", "puffy eyes"],
       "description": "LiftEyes uses 630nm red light therapy to reduce under eye bags, dark circles and "
                      "puffiness. Wear the glasses 10 minutes a day. Rechargeable, hands-free."},
      "Gift Card"]),
]

SEARCH_KEYWORD = {"posture": "posture corrector", "pet-grooming": "cat hair remover", "eye-care": "eye bags"}

BODIES = [
    "30-day money back guarantee. Thousands of happy customers.",
    "Limited stock — order today and save 50%.",
]

# Nội dung ad theo angle; trọng số theo tiến độ thời gian t ∈ [0, 1] (0 = tuần cũ nhất, 1 = tuần này)
ANGLE_COPY: dict[str, list[tuple[str, list[str], object]]] = {
    "posture": [
        ("desk", ["Desk job back pain? Fix your posture in 2 weeks.",
                  "Sitting 8 hours a day is wrecking your back. Office workers swear by this."],
         lambda t: max(0.15, 1.2 - t)),                       # đang giảm
        ("gift", ["The perfect gift for mom who sits all day 💝",
                  "Christmas gift idea: help mom stand tall again 🎁"],
         lambda t: 0.05 + 2.0 * t ** 3),                      # đang lên mạnh
        ("confidence", ["Stand taller and look more confident in every photo.",
                        "Slouching makes you look older. Straighten up in days."],
         lambda t: 0.5),                                      # ổn định
        ("offer", BODIES, lambda t: 0.3),
    ],
    "pet-grooming": [
        ("black-clothes", ["No more cat hair on your black clothes before work.",
                           "Cat hair on your work outfit? Gone in one swipe."],
         lambda t: 0.1 + 1.5 * t ** 2),
        ("couch", ["Your couch will look brand new — no batteries, reusable forever.",
                   "Remove pet hair from sofas and car seats in seconds."],
         lambda t: 0.6),
        ("offer", BODIES, lambda t: 0.3),
    ],
    "eye-care": [
        ("younger", ["Look 10 years younger in photos — no injections.",
                     "Women over 40 are ditching eye creams for this."],
         lambda t: 0.2 + t),
        ("night-shift", ["Nurses on night shift swear by this for puffy eyes.",
                         "Long shifts, no sleep? Wake up without dark circles."],
         lambda t: 2.0 if t > 0.82 else 0.0),                 # mới xuất hiện 2 tuần gần đây
        ("science", ["Dermatologist-recommended 630nm red light therapy.",
                     "Clinically studied red light for under-eye bags."],
         lambda t: 0.5),
        ("offer", BODIES, lambda t: 0.3),
    ],
}


ANGLE_SET = {"postureglow.com": "posture", "spinealign.co": "posture", "backbuddy.store": "posture",
             "furrygroom.com": "pet-grooming", "lifteyes.co": "eye-care"}


def _body(rng: random.Random, domain: str | None, t: float) -> str:
    options = ANGLE_COPY.get(ANGLE_SET.get(domain or "", ""))
    if not options:
        return rng.choice(BODIES)
    weights = [max(0.0, float(fn(t))) for _, _, fn in options]
    _, texts, _ = rng.choices(options, weights=weights)[0]
    return rng.choice(texts)


# Vòng đời ad theo store: (hàm số ngày sống — None = vẫn đang chạy, mỗi ad một nội dung riêng?)
#   w = vị trí tuần (0..n-1), i = thứ tự ad trong tuần
LIFE = {
    "postureglow.com": (lambda rng, w, i: None if rng.random() < 0.8 else rng.randint(7, 14), False),   # đang scale
    "lifteyes.co": (lambda rng, w, i: None if rng.random() < 0.8 else rng.randint(7, 14), False),       # đang scale
    "spinealign.co": (lambda rng, w, i: rng.randint(4, 12), True),                                       # máy xay
    "backbuddy.store": (lambda rng, w, i: None if w == 0 else rng.randint(6, 10), False),               # evergreen
    "starrynight.shop": (lambda rng, w, i: rng.randint(8, 12), False),                                   # đã dừng
    "furrygroom.com": (lambda rng, w, i: None if rng.random() < 0.85 else rng.randint(7, 14), False),   # tăng sớm
    "petlint.io": (lambda rng, w, i: rng.randint(20, 45), False),                                        # ổn định
    "Groomy Paws": (lambda rng, w, i: rng.randint(4, 10), True),                                         # đang test
}
_WORDS = "bold calm crisp daring fresh gentle honest lively quiet rapid sharp smart steady sunny swift vivid".split()


def generate(as_of: date, seed: int = 7) -> list[tuple[str, str, dict[str, Any]]]:
    """Trả về list (search, country, raw_ad)."""
    rng = random.Random(seed)
    out = []
    ad_id = 100000
    for domain, page, weeks, countries, reach, search, titles, _products in PATTERNS:
        page_id = str(zlib.crc32(page.encode()))
        n = len(weeks)
        life_fn, unique = LIFE.get(domain or page, (lambda rng, w, i: None, False))
        for w, count in enumerate(weeks):
            for i in range(count):
                ad_id += 1
                start = as_of - timedelta(days=(n - 1 - w) * 7 + rng.randint(0, 6))
                days = life_fn(rng, w, i)
                stop = start + timedelta(days=days) if days is not None and start + timedelta(days=days) < as_of else None
                body = _body(rng, domain, w / (n - 1))
                if unique:  # mỗi ad một creative mới (không nhân bản)
                    body += " " + " ".join(rng.choice(_WORDS) for _ in range(3))
                raw = {
                    "id": str(ad_id),
                    "page_id": page_id,
                    "page_name": page,
                    "ad_creation_time": start.isoformat(),
                    "ad_delivery_start_time": start.isoformat(),
                    "ad_delivery_stop_time": stop.isoformat() if stop else None,
                    "ad_creative_bodies": [body],
                    "ad_creative_link_titles": [rng.choice(titles)],
                    "ad_creative_link_captions": [domain.upper()] if domain else [],
                    "ad_snapshot_url": f"https://www.facebook.com/ads/library/?id={ad_id}",
                    "publisher_platforms": ["facebook", "instagram"],
                    "languages": ["en"],
                    "eu_total_reach": int(reach * rng.uniform(0.3, 1.7)),
                }
                for c in countries:
                    out.append((search, c, raw))
    return out


def shopify_products() -> dict[str, list[dict]]:
    return {
        d: [p if isinstance(p, dict) else {"title": p} for p in prods]
        for d, *_rest, prods in PATTERNS if d and prods
    }


# ------------------------------------------------------------------ Claude giả lập
_INSIGHTS = {
    "lifteyes.co": dict(generic_product="red light therapy glasses", category="beauty devices",
                        search_keywords=["red light therapy glasses", "red light eye mask", "red light therapy for eyes"],
                        problem_keywords=["eye bags", "puffy eyes", "dark circles"],
                        target_persona="women 35-55 worried about tired-looking eyes", confidence="high"),
    "postureglow.com": dict(generic_product="posture corrector", category="health",
                            search_keywords=["posture corrector", "posture brace"],
                            problem_keywords=["back pain"], target_persona="desk workers", confidence="high"),
}


class DemoExtractor:
    """Thay Claude trong demo/test: trả kết quả soạn sẵn, None cho store không biết (→ heuristic)."""

    model = "demo"

    def __init__(self):
        self.calls = 0

    def extract(self, ctx):
        from winfinder.analysis.product_llm import ProductInsight

        self.calls += 1
        d = _INSIGHTS.get(ctx.domain or "")
        return ProductInsight(**d, model="demo") if d else None


# ------------------------------------------------------------------ Trends giả lập
import math  # noqa: E402

from winfinder.config import TrendTopic  # noqa: E402

TREND_TOPICS = [
    TrendTopic("posture", ["posture corrector"], ["posture brace", "back straightener"], volume_hint=74000),
    TrendTopic("heated vest", ["heated vest"], ["heated jacket", "battery heated vest"]),
    TrendTopic("cat hair remover", ["cat hair remover"], ["pet hair roller", "lint remover for pets"]),
    TrendTopic("galaxy projector", ["galaxy projector"], ["star projector"]),
    TrendTopic("fidget toy", ["fidget toy"], ["pop it fidget"]),
]

# (keyword, geo) → kiểu chuỗi; mặc định theo keyword
_PATTERN_BY_KEYWORD = {
    "posture corrector": "rising", "posture brace": "rising", "back straightener": "stable",
    "heated vest": "seasonal", "heated jacket": "seasonal", "battery heated vest": "seasonal_rising",
    "cat hair remover": "breakout", "pet hair roller": "breakout", "lint remover for pets": "stable",
    "galaxy projector": "spike", "star projector": "spike",
    "fidget toy": "declining", "pop it fidget": "declining",
    "smart posture corrector": "rising", "heated dog vest": "seasonal", "dog vest": "seasonal",
    "cat hair remover roller": "breakout", "hair remover roller": "breakout",
    "eye bags": "rising", "puffy eyes": "rising", "dark circles": "stable", "back pain": "stable",
    # lượt tìm tên brand
    "postureglow": "brand_ramp", "lifteyes": "brand_ramp", "spinealign": "brand_flat",
    "backbuddy": "brand_flat", "starrynight": "brand_old",
}
_GEO_OVERRIDE = {("posture corrector", "DE"): "stable", ("posture brace", "DE"): "stable",
                 ("cat hair remover", "GB"): "stable", ("cat hair remover", "DE"): "stable"}


def _shape(kind: str, i: int, n: int, rng: random.Random, as_of: date) -> float:
    t = i / (n - 1)
    woy = ((as_of - timedelta(weeks=n - 1 - i)).isocalendar().week) / 52
    winter = max(0.0, math.cos(2 * math.pi * (woy - 0.97)))  # đỉnh cuối tháng 12
    noise = rng.gauss(0, 2)
    if kind == "rising":
        v = 15 + 45 * t ** 1.6 + 4 * winter
    elif kind == "stable":
        v = 30 + 3 * math.sin(2 * math.pi * woy)
    elif kind == "seasonal":
        v = 8 + 70 * winter ** 3
    elif kind == "seasonal_rising":
        v = (5 + 50 * winter ** 3) * (0.6 + 0.8 * t)
    elif kind == "breakout":
        v = 6 + (40 * (i - (n - 10)) / 10 if i > n - 10 else 0)
    elif kind == "spike":
        peak = n - 20
        v = 6 + 90 * math.exp(-((i - peak) / 4) ** 2) + (8 if i > peak else 0)
    elif kind == "declining":
        v = 70 * (1 - t) ** 1.3 + 6
    elif kind == "brand_ramp":   # brand mới, lượt tìm tăng theo ad 8 tuần gần đây
        v = 0.0 if i < n - 8 else 8 * (i - (n - 8)) + 10
    elif kind == "brand_flat":   # có lượt tìm từ ~14 tuần nay nhưng không tăng
        v = 0.0 if i < n - 14 else 25
    elif kind == "brand_old":    # từ thông dụng, có lượt tìm từ nhiều năm
        v = 40 + 5 * math.sin(i / 6)
    else:  # keyword không ai tìm → Trends trả toàn 0
        return 0.0
    return max(0.0, round(v + noise, 1))


class DemoTrendsProvider:
    name = "demo"

    def __init__(self, as_of: date, weeks: int = 260):
        self.as_of = as_of
        self.weeks = weeks

    def interest_over_time(self, keywords, geo, timeframe):
        out = {}
        for k in keywords:
            kind = _GEO_OVERRIDE.get((k, geo), _PATTERN_BY_KEYWORD.get(k, "none"))
            rng = random.Random(zlib.crc32(f"{k}|{geo}".encode()))
            scale = 1.0 if k in {t.seeds[0] for t in TREND_TOPICS} else 0.45
            out[k] = [
                (self.as_of - timedelta(weeks=self.weeks - 1 - i),
                 round(scale * _shape(kind, i, self.weeks, rng, self.as_of), 1))
                for i in range(self.weeks)
            ]
        return out


# ------------------------------------------------------------------ phân tích angle giả lập
ANGLE_DEFS = {
    "desk": ("Đau lưng dân văn phòng", "Nhân viên văn phòng ngồi nhiều", "Đau lưng, mỏi vai do ngồi lâu",
             "Hết đau lưng sau 2 tuần", "problem-solution"),
    "gift": ("Quà tặng cho mẹ", "Con cái mua quà cho mẹ", "Muốn mẹ khoẻ, đứng thẳng", "Món quà thiết thực", "gift"),
    "confidence": ("Tự tin, dáng đẹp", "Người ngại dáng gù khi chụp ảnh", "Trông già, thiếu tự tin",
                   "Đứng thẳng, trông trẻ hơn", "before-after"),
    "black-clothes": ("Quần áo đen sạch lông", "Người nuôi mèo đi làm văn phòng", "Lông mèo dính quần áo",
                      "Sạch lông trong một lần lăn", "problem-solution"),
    "couch": ("Sofa như mới", "Chủ nhà nuôi thú cưng", "Lông bám sofa, ghế xe", "Sạch nhanh, không cần pin",
              "demonstration"),
    "younger": ("Trẻ hơn 10 tuổi", "Phụ nữ trên 40", "Bọng mắt làm trông già", "Trẻ hơn không cần tiêm", "before-after"),
    "night-shift": ("Ca đêm vẫn tươi tỉnh", "Y tá, người làm ca đêm", "Bọng mắt, quầng thâm do thiếu ngủ",
                    "Thức dậy không còn bọng mắt", "testimonial"),
    "science": ("Chứng minh khoa học", "Người cần bằng chứng", "Nghi ngờ hiệu quả", "Ánh sáng đỏ 630nm được nghiên cứu",
                "authority"),
    "offer": ("Ưu đãi / cam kết hoàn tiền", "Người săn khuyến mãi", "Sợ mua phải hàng kém", "Giảm giá, hoàn tiền 30 ngày",
              "offer"),
}
UNTAPPED = {
    "posture": [{"name": "Học sinh ôn thi", "persona": "Phụ huynh có con tuổi teen",
                 "why": "Chưa store nào nhắm phụ huynh lo dáng gù của con"}],
    "eye-care": [{"name": "Chú rể / cô dâu trước cưới", "persona": "Người sắp cưới",
                  "why": "Nhu cầu đẹp trong ảnh cưới, chưa ai khai thác"}],
}


class DemoAngleAnalyzer:
    """Thay Claude trong demo/test: phân loại theo nội dung mẫu đã biết."""

    model = "demo"

    def __init__(self):
        self.calls = 0

    def analyze(self, product, generic, units):
        from winfinder.analysis.angles import AngleDef, AngleResult

        self.calls += 1
        text_to_key = {txt.lower(): key for opts in ANGLE_COPY.values() for key, texts, _ in opts for txt in texts}
        ids: dict[str, str] = {}
        assignments = {}
        for u in units:
            body = u.text.split(" | ")[-1].lower()
            key = text_to_key.get(body, "offer")
            ids.setdefault(key, f"A{len(ids) + 1}")
            assignments[u.ref] = ids[key]
        angles = [AngleDef(aid, *ANGLE_DEFS[key]) for key, aid in ids.items()]
        cat = "posture" if "posture" in product else ("eye-care" if "light" in product or "eye" in product else "")
        return AngleResult(angles, assignments, UNTAPPED.get(cat, []), "demo")


# ------------------------------------------------------------------ ad chỉ tìm thấy khi mở rộng theo domain
# (không chứa keyword ngách → tìm theo keyword không thấy)
HIDDEN = [
    # page thứ 2 của PostureGlow, trỏ thẳng store
    ("postureglow.com", "PostureGlow Official", "POSTUREGLOW.COM",
     ["PostureGlow — stand tall", "Meet PostureGlow"], [0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 4]),
    # page vệ tinh chạy advertorial trên domain trung gian, dẫn về lifteyes.co
    ("lifteyes.co", "Beauty Insider Daily", "BEAUTYINSIDERDAILY.COM",
     ["I tried LiftEyes for 30 days — here's what happened", "Why women over 40 are switching to LiftEyes"],
     [0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3, 4]),
]
PRESELL = {"beautyinsiderdaily.com": ("presell", "lifteyes.co", "advertorial")}


def hidden_raws(as_of: date, seed: int = 11) -> list[tuple[str, dict[str, Any]]]:
    rng = random.Random(seed)
    out = []
    ad_id = 900000
    for _store, page, caption, titles, weeks in HIDDEN:
        n = len(weeks)
        for w, count in enumerate(weeks):
            for _ in range(count):
                ad_id += 1
                start = as_of - timedelta(days=(n - 1 - w) * 7 + rng.randint(0, 6))
                raw = {
                    "id": str(ad_id), "page_id": str(zlib.crc32(page.encode())), "page_name": page,
                    "ad_creation_time": start.isoformat(), "ad_delivery_start_time": start.isoformat(),
                    "ad_delivery_stop_time": None if rng.random() < 0.85 else (start + timedelta(days=9)).isoformat(),
                    "ad_creative_bodies": [rng.choice(titles) + " Read the full story."],
                    "ad_creative_link_titles": [rng.choice(titles)],
                    "ad_creative_link_captions": [caption],
                    "ad_snapshot_url": f"https://www.facebook.com/ads/library/?id={ad_id}",
                    "publisher_platforms": ["facebook"], "languages": ["en"],
                    "eu_total_reach": int(5000 * rng.uniform(0.3, 1.7)),
                }
                out.append(("IE", raw))
    return out


class DemoAdLibraryClient:
    """Thay Ad Library API trong demo/test: một 'vũ trụ' ad cố định, tìm theo keyword và theo page."""

    def __init__(self, as_of: date):
        main = [(c, raw) for _s, c, raw in generate(as_of)]
        self.universe = {(c, raw["id"]): raw for c, raw in main + hidden_raws(as_of)}
        self.calls: list[tuple] = []

    def _in(self, country):
        return [raw for (c, _), raw in self.universe.items() if c == country]

    def search(self, keyword, country, since, languages=None, max_pages=20):
        self.calls.append(("search", keyword, country))
        k = keyword.lower()
        for raw in self._in(country):
            text = " ".join(raw["ad_creative_link_titles"] + raw["ad_creative_bodies"]
                            + raw["ad_creative_link_captions"]).lower()
            if k in text:
                yield raw

    def search_pages(self, page_ids, country, since, max_pages=20):
        self.calls.append(("pages", tuple(page_ids), country))
        ids = set(map(str, page_ids))
        for raw in self._in(country):
            if raw["page_id"] in ids:
                yield raw


def demo_resolver(domain: str):
    from winfinder.sources.landing import Resolution

    kind, target, lt = PRESELL.get(domain, ("store", None, None))
    return Resolution(kind, target, lt)
