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
