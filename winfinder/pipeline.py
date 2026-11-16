"""Các bước pipeline: collect (Ad Library) → enrich (domain) → analyze."""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import date, timedelta

from winfinder import db
from winfinder.analysis.scoring import StoreResult
from winfinder.config import Config
from winfinder.http import make_session
from winfinder.sources import domain as domain_src
from winfinder.sources.adlibrary import AdLibraryClient, AdLibraryError, parse_ad

log = logging.getLogger(__name__)


def collect(conn: sqlite3.Connection, cfg: Config, client: AdLibraryClient, today: date) -> int:
    since = today - timedelta(days=cfg.lookback_days)
    total = 0
    for search in cfg.searches:
        for keyword in search.keywords:
            for country in cfg.countries:
                try:
                    raws = list(
                        client.search(
                            keyword, country, since,
                            languages=search.languages or None,
                            max_pages=cfg.max_pages_per_query,
                        )
                    )
                except AdLibraryError as exc:
                    log.error("[%s] '%s' @ %s: %s", search.name, keyword, country, exc)
                    continue
                ads = [parse_ad(r, cfg.exclude_domains) for r in raws]
                n = db.upsert_ads(conn, ads, country=country, search=search.name, keyword=keyword, observed_on=today)
                log.info("[%s] '%s' @ %s: %d ad", search.name, keyword, country, n)
                total += n
    return total


def enrich(conn: sqlite3.Connection, cfg: Config, stores: list[StoreResult], today: date, refresh_days: int = 30) -> int:
    """Tra RDAP / crt.sh / Shopify cho top store chưa được kiểm tra trong `refresh_days` ngày."""
    session = make_session()
    checked = {
        r["domain"]: r["checked_on"]
        for r in conn.execute("SELECT domain, checked_on FROM domains")
    }
    n = 0
    for s in stores[: cfg.enrich_top_n]:
        if not s.domain:
            continue
        last = checked.get(s.domain)
        if last and (today - date.fromisoformat(last)).days < refresh_days:
            continue
        registered = cert = None
        try:
            registered = domain_src.rdap_registration_date(session, s.domain)
        except Exception as exc:  # nguồn ngoài, lỗi không được làm hỏng cả run
            log.warning("RDAP %s: %s", s.domain, exc)
        try:
            cert = domain_src.first_certificate_date(session, s.domain)
        except Exception as exc:
            log.warning("crt.sh %s: %s", s.domain, exc)
        shop = domain_src.shopify_products(session, s.domain)
        db.save_domain(
            conn, s.domain,
            registered_at=registered, first_cert_at=cert,
            is_shopify=shop.is_shopify, product_count=shop.product_count,
            products=shop.products, checked_on=today,
        )
        n += 1
        time.sleep(cfg.enrich_sleep_seconds)
    return n
