"""Dữ liệu giả lập đúng định dạng Ad Library API — để test và xem thử dashboard khi chưa có token."""

from __future__ import annotations

import random
import zlib
from datetime import date, timedelta
from typing import Any

# (domain, page_name, số creative mới mỗi tuần — cũ → mới, các nước, reach/ad)
PATTERNS: list[tuple[str | None, str, list[int], list[str], int]] = [
    ("postureglow.com", "PostureGlow", [0] * 7 + [1, 2, 3, 5, 8], ["IE", "NL", "DE"], 9000),
    ("spinealign.co", "SpineAlign", [0] * 8 + [2, 2, 4, 6], ["IE", "DK"], 4000),
    ("backbuddy.store", "BackBuddy", [3] * 12, ["IE", "NL", "DE", "FR"], 15000),
    ("quickfixposture.shop", "QuickFix", [0, 0, 4, 3, 2, 0, 0, 0, 0, 0, 0, 0], ["IE"], 1200),
    ("furrygroom.com", "FurryGroom", [0] * 6 + [1, 1, 2, 3, 4, 6], ["IE", "SE"], 6000),
    ("petlint.io", "PetLint", [1, 0, 2, 1, 0, 1, 2, 0, 1, 1, 0, 1], ["NL"], 800),
    (None, "Groomy Paws (no link)", [0] * 9 + [2, 3, 5], ["IE"], 2500),
]

BODIES = [
    "Fix your posture in 10 minutes a day. Doctors hate how simple this is.",
    "Gift for mom who sits all day 💝 Free shipping today only.",
    "I tried 7 posture correctors. This is the only one I still wear.",
    "Desk job back pain? 30-day money back guarantee.",
    "Remove pet hair from your couch in seconds — no batteries.",
]


def generate(as_of: date, seed: int = 7) -> list[tuple[str, str, dict[str, Any]]]:
    """Trả về list (search, country, raw_ad)."""
    rng = random.Random(seed)
    out = []
    ad_id = 100000
    for domain, page, weeks, countries, reach in PATTERNS:
        search = "pet-grooming" if any(k in page.lower() for k in ("groom", "pet")) else "posture"
        page_id = str(zlib.crc32(page.encode()))
        n = len(weeks)
        for w, count in enumerate(weeks):
            for _ in range(count):
                ad_id += 1
                start = as_of - timedelta(days=(n - 1 - w) * 7 + rng.randint(0, 6))
                stopped = rng.random() < 0.25 and w < n - 2
                raw = {
                    "id": str(ad_id),
                    "page_id": page_id,
                    "page_name": page,
                    "ad_creation_time": start.isoformat(),
                    "ad_delivery_start_time": start.isoformat(),
                    "ad_delivery_stop_time": (start + timedelta(days=10)).isoformat() if stopped else None,
                    "ad_creative_bodies": [rng.choice(BODIES)],
                    "ad_creative_link_titles": [f"{page} — Shop now"],
                    "ad_creative_link_captions": [domain.upper()] if domain else [],
                    "ad_snapshot_url": f"https://www.facebook.com/ads/library/?id={ad_id}",
                    "publisher_platforms": ["facebook", "instagram"],
                    "languages": ["en"],
                    "eu_total_reach": int(reach * rng.uniform(0.3, 1.7)),
                }
                for c in countries:
                    out.append((search, c, raw))
    return out


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
    else:
        v = 20
    return max(0.0, round(v + noise, 1))


class DemoTrendsProvider:
    name = "demo"

    def __init__(self, as_of: date, weeks: int = 260):
        self.as_of = as_of
        self.weeks = weeks

    def interest_over_time(self, keywords, geo, timeframe):
        out = {}
        for k in keywords:
            kind = _GEO_OVERRIDE.get((k, geo), _PATTERN_BY_KEYWORD.get(k, "stable"))
            rng = random.Random(zlib.crc32(f"{k}|{geo}".encode()))
            scale = 1.0 if k in {t.seeds[0] for t in TREND_TOPICS} else 0.45
            out[k] = [
                (self.as_of - timedelta(weeks=self.weeks - 1 - i),
                 round(scale * _shape(kind, i, self.weeks, rng, self.as_of), 1))
                for i in range(self.weeks)
            ]
        return out
