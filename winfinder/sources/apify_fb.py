"""Ad Facebook chạy ở US qua Apify (actor apify/facebook-ads-scraper) — thứ API Meta miễn phí không trả.

Khác API Meta (EU):
  + Có đường dẫn landing đầy đủ (`linkUrl`) → phân biệt PDP / advertorial chính xác.
  + Có `collationCount` = số phiên bản dùng chung creative → tín hiệu nhân bản.
  − Chỉ thấy ad ĐANG CHẠY (Ad Library không hiện ad thương mại đã tắt ngoài EU). Vì vậy:
      * `endDate` của ad đang chạy là NGÀY QUÉT, không phải ngày tắt → bỏ qua.
      * Ngày tắt suy ra khi quét TRỌN page của store: ad lần trước còn, lần này không thấy → đã tắt
        (trong khoảng giữa 2 lần quét). Lượt quét chạm giới hạn số lượng → không suy ra (có thể còn ad chưa lấy).
      * Ad thấy lần đầu khi đã già không được tính vào tỉ lệ sống sót (xem analysis/scoring.observable).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import quote, urlparse

from winfinder import db
from winfinder.analysis.scoring import StoreResult
from winfinder.config import Config
from winfinder.sources.adlibrary import Ad, extract_domain
from winfinder.sources.apify import ApifyClient, ApifyError, BudgetExhausted

log = logging.getLogger(__name__)

ACTOR = "apify/facebook-ads-scraper"
SOURCE = "apify_fb"


def library_url(country: str, keyword: str | None = None, page_id: str | None = None) -> str:
    base = f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country={country}&media_type=all"
    if page_id:
        return f"{base}&search_type=page&view_all_page_id={page_id}"
    return f"{base}&q={quote(keyword or '')}&search_type=keyword_unordered"


def _day(ts) -> str | None:
    if ts in (None, ""):
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    return str(ts)[:10]


def _host(url: str | None) -> str | None:
    if not url:
        return None
    h = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else (h or None)


def parse_item(item: dict, exclude: set[str] = frozenset()) -> Ad | None:
    snap = item.get("snapshot") or {}
    cards = snap.get("cards") or []
    first = cards[0] if cards else {}
    ad_id = str(item.get("adArchiveID") or item.get("adArchiveId") or "")
    if not ad_id:
        return None
    link = snap.get("linkUrl") or first.get("linkUrl")
    host = _host(link)
    if host and any(host == x or host.endswith("." + x) for x in exclude):
        host = None
    caption = snap.get("caption") or first.get("caption") or ""
    domain = host or extract_domain([caption], exclude)
    body = ((snap.get("body") or {}).get("text") or first.get("body") or "").strip()
    title = (snap.get("title") or first.get("title") or "").strip()
    active = bool(item.get("isActive"))
    return Ad(
        ad_id=ad_id,
        page_id=str(item.get("pageID") or item.get("pageId") or snap.get("pageId") or ""),
        page_name=item.get("pageName") or snap.get("pageName") or "",
        domain=domain,
        created_at=_day(item.get("startDate")),
        start_date=_day(item.get("startDate")) or (item.get("startDateFormatted") or "")[:10] or None,
        stop_date=None if active else (_day(item.get("endDate")) or (item.get("endDateFormatted") or "")[:10] or None),
        bodies=[body] if body else [],
        titles=[title] if title else [],
        captions=[caption] if caption else [],
        snapshot_url=f"https://www.facebook.com/ads/library/?id={ad_id}",
        platforms=[str(p).lower() for p in item.get("publisherPlatform") or []],
        languages=[],
        eu_total_reach=None,
        link_url=link,
        source=SOURCE,
        versions=int(item.get("collationCount") or 1),
    )


@dataclass
class UsStats:
    ads: int = 0
    pages_swept: int = 0
    stopped_inferred: int = 0
    skipped: list[str] = field(default_factory=list)


def collect(conn: sqlite3.Connection, cfg: Config, client: ApifyClient, today: date) -> UsStats:
    """Ad đang chạy ở US theo keyword của từng ngách."""
    ac = cfg.apify
    st = UsStats()
    for search in cfg.searches:
        for kw in search.keywords:
            try:
                items = client.run(ACTOR, {"startUrls": [{"url": library_url(ac.fb_country, keyword=kw)}],
                                           "resultsLimit": ac.max_ads_per_keyword},
                                   label=f"US '{kw}'")
            except BudgetExhausted as exc:
                st.skipped.append(str(exc))
                return st
            except ApifyError as exc:
                log.warning("Apify US '%s': %s", kw, exc)
                st.skipped.append(f"'{kw}': {exc}")
                continue
            ads = [a for a in (parse_item(i, cfg.exclude_domains) for i in items) if a]
            if ads:
                db.upsert_ads(conn, ads, country=ac.fb_country, search=search.name, keyword=kw, observed_on=today)
            st.ads += len(ads)
            log.info("[%s] US '%s': %d ad", search.name, kw, len(ads))
    return st


def sweep_store_pages(conn: sqlite3.Connection, cfg: Config, client: ApifyClient, stores: list[StoreResult],
                      today: date) -> UsStats:
    """Quét TRỌN ad đang chạy ở US của mọi page của các store điểm cao → đếm đúng + suy ra ad đã tắt."""
    ac = cfg.apify
    st = UsStats()
    cands = [s for s in stores if s.domain and s.archetype != "stopped" and s.score >= cfg.expand.min_score]
    cands = cands[: ac.sweep_top_stores]
    for s in cands:
        pages = [r[0] for r in conn.execute("SELECT DISTINCT page_id FROM ads WHERE domain = ? AND page_id != ''",
                                            (s.domain,))]
        for pid in pages:
            key = f"us:{pid}"
            row = conn.execute("SELECT swept_on FROM swept_pages WHERE page_id = ?", (key,)).fetchone()
            if row and row[0] == today.isoformat():
                continue
            try:
                items = client.run(ACTOR, {"startUrls": [{"url": library_url(ac.fb_country, page_id=pid)}],
                                           "resultsLimit": ac.max_ads_per_page}, label=f"page {pid}")
            except BudgetExhausted as exc:
                st.skipped.append(str(exc))
                return st
            except ApifyError as exc:
                st.skipped.append(f"page {pid}: {exc}")
                continue
            ads = [a for a in (parse_item(i, cfg.exclude_domains) for i in items) if a]
            if ads:
                db.upsert_ads(conn, ads, country=ac.fb_country, search="expand", keyword=f"us-page:{s.domain}",
                              observed_on=today)
            st.ads += len(ads)
            st.pages_swept += 1
            if len(items) < ac.max_ads_per_page:   # lấy trọn page → ad không còn thấy = đã tắt
                st.stopped_inferred += mark_stopped(conn, pid, {a.ad_id for a in ads}, today)
            else:
                log.info("page %s có ≥ %d ad — chưa lấy trọn, không suy ra ad đã tắt", pid, ac.max_ads_per_page)
            conn.execute("INSERT OR REPLACE INTO swept_pages VALUES (?,?)", (key, today.isoformat()))
            conn.commit()
    return st


def mark_stopped(conn: sqlite3.Connection, page_id: str, seen_ids: set[str], today: date) -> int:
    """Ad US của page này lần trước còn chạy mà lần quét trọn này không thấy → tắt vào khoảng last_seen."""
    rows = conn.execute(
        "SELECT ad_id, last_seen FROM ads WHERE page_id = ? AND source = ? AND stop_date IS NULL AND last_seen < ?",
        (page_id, SOURCE, today.isoformat())).fetchall()
    gone = [(r["last_seen"], r["ad_id"]) for r in rows if r["ad_id"] not in seen_ids]
    conn.executemany("UPDATE ads SET stop_date = ? WHERE ad_id = ?", gone)
    conn.commit()
    return len(gone)
