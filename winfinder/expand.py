"""Mở rộng store từ domain gốc: tìm MỌI page đang chạy cho store, lấy TOÀN BỘ ad, lần ngược page vệ tinh.

Vì sao: tìm ad theo keyword chỉ thấy ad có nhắc keyword. Store chạy bằng tên brand ('LiftEyes'), chạy nhiều page,
hoặc chạy qua advertorial/listicle trên domain khác sẽ bị bỏ sót / đếm thiếu → đo sai độ bền và đà tăng.

  (a) Tìm ad theo domain + tên brand → page mới có ad trỏ về store (hoặc nhắc tên brand).
  (b) Quét toàn bộ ad của từng page (`search_page_ids`), không lọc keyword.
  (c) Ad của các page đó trỏ tới domain khác → kiểm tra domain đó là store hay trang trung gian,
      nếu là trang trung gian dẫn về store gốc thì nối ad về store (ghi lại via_domain + loại landing).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from winfinder import db
from winfinder.analysis.scoring import StoreResult
from winfinder.analysis.traffic import brand_term
from winfinder.config import Config
from winfinder.sources.adlibrary import AdLibraryClient, AdLibraryError, parse_ad
from winfinder.sources.landing import Resolution

log = logging.getLogger(__name__)

Resolver = Callable[[str], Resolution]


@dataclass
class ExpandStats:
    stores: int = 0
    new_ads: int = 0
    pages_swept: int = 0
    presell_resolved: int = 0
    ads_relinked: int = 0
    pages_by_store: dict[str, int] = field(default_factory=dict)


def _known(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT ad_id FROM ads")}


def _store_pages(conn: sqlite3.Connection, domain: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT page_id FROM ads WHERE domain = ?", (domain,))}


def _swept_recently(conn: sqlite3.Connection, page_id: str, today: date, days: int) -> bool:
    r = conn.execute("SELECT swept_on FROM swept_pages WHERE page_id = ?", (page_id,)).fetchone()
    return bool(r and (today - date.fromisoformat(r[0])).days < days)


def _mentions(term: str | None, raw: dict) -> bool:
    if not term or len(term) < 4:
        return False
    text = " ".join((raw.get("ad_creative_link_titles") or []) + (raw.get("ad_creative_bodies") or [])).lower()
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


def expand_stores(
    conn: sqlite3.Connection, cfg: Config, client: AdLibraryClient, stores: list[StoreResult], today: date,
    resolver: Resolver | None = None,
) -> ExpandStats:
    ec = cfg.expand
    since = today - timedelta(days=cfg.lookback_days)
    countries = ec.countries or cfg.countries[:3]
    cands = [s for s in stores if s.domain and s.archetype != "stopped" and s.score >= ec.min_score][: ec.top_n]
    st = ExpandStats(stores=len(cands))
    before = len(_known(conn))

    def save(raws, country: str, keyword: str) -> None:
        ads = [parse_ad(r, cfg.exclude_domains) for r in raws]
        if ads:
            db.upsert_ads(conn, ads, country=country, search="expand", keyword=keyword, observed_on=today)

    store_pages: dict[str, set[str]] = {}
    for s in cands:
        pages = _store_pages(conn, s.domain)
        term = brand_term(s.domain, s.page_names)
        # (a) tìm theo domain + tên brand
        for q in dict.fromkeys(x for x in (s.domain, term) if x):
            for c in countries:
                keep = []
                try:
                    for raw in client.search(q, c, since, max_pages=ec.max_pages):
                        dom = parse_ad(raw, cfg.exclude_domains).domain
                        if dom == s.domain or str(raw.get("page_id")) in pages or (dom and _mentions(term, raw)):
                            keep.append(raw)
                except AdLibraryError as exc:
                    log.warning("Mở rộng %s ('%s' @ %s): %s", s.domain, q, c, exc)
                    continue
                pages |= {str(r.get("page_id")) for r in keep if r.get("page_id")}
                save(keep, c, f"store:{s.domain}")
        # (b) toàn bộ ad của từng page
        todo = sorted(p for p in pages if p and not _swept_recently(conn, p, today, ec.refresh_days))
        for i in range(0, len(todo), 10):
            chunk = todo[i : i + 10]
            for c in countries:
                try:
                    save(list(client.search_pages(chunk, c, since, max_pages=ec.max_pages)), c, f"pages:{s.domain}")
                except AdLibraryError as exc:
                    log.warning("Quét page của %s @ %s: %s", s.domain, c, exc)
            conn.executemany("INSERT OR REPLACE INTO swept_pages VALUES (?,?)", [(p, today.isoformat()) for p in chunk])
            conn.commit()
            st.pages_swept += len(chunk)
        st.pages_by_store[s.domain] = len(pages)
        store_pages[s.domain] = pages  # gồm cả page vệ tinh (ad trỏ domain khác nhưng nhắc tên brand)
        log.info("[%s] %d page", s.domain, len(pages))

    # (c) domain lạ mà page của store đang trỏ tới → store khác hay trang trung gian?
    if ec.resolve_presell and resolver:
        stale = today - timedelta(days=ec.refresh_days * 4)
        checked = {r[0]: r[1] for r in conn.execute("SELECT domain, checked_on FROM domain_links")}
        for s in cands:
            pages = store_pages.get(s.domain) or _store_pages(conn, s.domain)
            if not pages:
                continue
            q = ",".join("?" * len(pages))
            others = {r[0] for r in conn.execute(
                f"SELECT DISTINCT landing_domain FROM ads WHERE page_id IN ({q}) AND landing_domain IS NOT NULL "
                f"AND landing_domain != ?", (*pages, s.domain))}
            for d in sorted(others):
                if d in checked and date.fromisoformat(checked[d]) >= stale:
                    continue
                res = resolver(d)
                target = (res.resolves_to or "").removeprefix("www.") or None
                db.save_domain_link(conn, d, res.kind, target, res.landing_type, today)
                checked[d] = today.isoformat()
                if res.kind == "presell" and target:
                    st.presell_resolved += 1
                    log.info("%s là %s → dẫn về %s", d, res.landing_type, target)
    st.ads_relinked = db.apply_domain_links(conn)
    st.new_ads = len(_known(conn)) - before
    return st


def store_pages_summary(conn: sqlite3.Connection, domain: str) -> dict:
    rows = conn.execute(
        "SELECT page_id, page_name, COUNT(*) n, SUM(CASE WHEN via_domain IS NULL THEN 0 ELSE 1 END) via "
        "FROM ads WHERE domain = ? GROUP BY page_id ORDER BY n DESC", (domain,)).fetchall()
    return {"pages": [dict(r) for r in rows],
            "via_domains": [r[0] for r in conn.execute(
                "SELECT DISTINCT via_domain FROM ads WHERE domain = ? AND via_domain IS NOT NULL", (domain,))]}

