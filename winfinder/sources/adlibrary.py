"""Client cho Meta Ad Library API (ads_archive).

Lưu ý: với quảng cáo thương mại (không phải chính trị), API chỉ trả ad được phân phối
ở EU — vì vậy mỗi truy vấn đi kèm một nước EU trong `ad_reached_countries`.
Tài liệu: https://www.facebook.com/ads/library/api
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterator

import requests

from winfinder.http import HttpError, get_json, make_session

log = logging.getLogger(__name__)

FIELDS = [
    "id",
    "page_id",
    "page_name",
    "ad_creation_time",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_creative_bodies",
    "ad_creative_link_titles",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_snapshot_url",
    "publisher_platforms",
    "languages",
    "eu_total_reach",
]

# Mã lỗi rate limit của Graph API (trả về kèm HTTP 400/403)
RATE_LIMIT_CODES = {4, 17, 32, 613, 80004}

_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")


class AdLibraryError(RuntimeError):
    pass


@dataclass
class Ad:
    ad_id: str
    page_id: str
    page_name: str
    domain: str | None
    created_at: str | None
    start_date: str | None
    stop_date: str | None
    bodies: list[str]
    titles: list[str]
    captions: list[str]
    snapshot_url: str | None
    platforms: list[str]
    languages: list[str]
    eu_total_reach: int | None


def extract_domain(captions: list[str] | None, exclude: set[str] = frozenset()) -> str | None:
    """Lấy domain store từ link caption của ad (vd 'SHOP.EXAMPLE.COM' → 'shop.example.com')."""
    for cap in captions or []:
        cand = cap.strip().lower()
        cand = re.sub(r"^https?://", "", cand).split("/")[0].split("?")[0]
        if cand.startswith("www."):
            cand = cand[4:]
        if not _DOMAIN_RE.match(cand):
            continue
        if any(cand == ex or cand.endswith("." + ex) for ex in exclude):
            continue
        return cand
    return None


def _day(ts: str | None) -> str | None:
    return ts[:10] if ts else None


def parse_ad(raw: dict[str, Any], exclude_domains: set[str] = frozenset()) -> Ad:
    captions = raw.get("ad_creative_link_captions") or []
    reach = raw.get("eu_total_reach")
    return Ad(
        ad_id=str(raw["id"]),
        page_id=str(raw.get("page_id", "")),
        page_name=raw.get("page_name", ""),
        domain=extract_domain(captions, exclude_domains),
        created_at=_day(raw.get("ad_creation_time")),
        start_date=_day(raw.get("ad_delivery_start_time")),
        stop_date=_day(raw.get("ad_delivery_stop_time")),
        bodies=raw.get("ad_creative_bodies") or [],
        titles=raw.get("ad_creative_link_titles") or [],
        captions=captions,
        snapshot_url=raw.get("ad_snapshot_url"),
        platforms=raw.get("publisher_platforms") or [],
        languages=raw.get("languages") or [],
        eu_total_reach=int(reach) if reach is not None else None,
    )


class AdLibraryClient:
    def __init__(
        self,
        token: str,
        api_version: str = "v23.0",
        session: requests.Session | None = None,
        sleep_seconds: float = 1.0,
        rate_limit_wait: float = 60.0,
    ):
        if not token:
            raise AdLibraryError("Thiếu access token (biến môi trường META_AD_LIBRARY_TOKEN)")
        self.token = token
        self.base_url = f"https://graph.facebook.com/{api_version}/ads_archive"
        self.session = session or make_session()
        self.sleep_seconds = sleep_seconds
        self.rate_limit_wait = rate_limit_wait

    def _get(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        for attempt in range(4):
            try:
                return get_json(self.session, url, params)
            except HttpError as exc:
                code = _graph_error_code(exc.body)
                if code in RATE_LIMIT_CODES and attempt < 3:
                    log.warning("Ad Library rate limit (code %s), chờ %.0fs", code, self.rate_limit_wait)
                    time.sleep(self.rate_limit_wait * (attempt + 1))
                    continue
                raise AdLibraryError(f"Ad Library API lỗi: {exc}") from exc
        raise AdLibraryError("Ad Library API: hết lượt thử lại do rate limit")

    def search(
        self,
        keyword: str,
        country: str,
        since: date,
        languages: list[str] | None = None,
        max_pages: int = 20,
    ) -> Iterator[dict[str, Any]]:
        """Trả về ad thô (dict) khớp keyword, đã phân phối tới `country`, bắt đầu từ `since`."""
        params: dict[str, Any] | None = {
            "access_token": self.token,
            "search_terms": keyword,
            "search_type": "KEYWORD_UNORDERED",
            "ad_type": "ALL",
            "ad_active_status": "ALL",
            "ad_reached_countries": json.dumps([country]),
            "ad_delivery_date_min": since.isoformat(),
            "fields": ",".join(FIELDS),
            "limit": 250,
        }
        if languages:
            params["languages"] = json.dumps(languages)
        url: str | None = self.base_url
        pages = 0
        while url and pages < max_pages:
            payload = self._get(url, params)
            yield from payload.get("data", [])
            pages += 1
            # link "next" đã chứa đủ tham số (kể cả token)
            url = (payload.get("paging") or {}).get("next")
            params = None
            if url:
                time.sleep(self.sleep_seconds)


def _graph_error_code(body: str) -> int | None:
    try:
        return int(json.loads(body)["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return None
