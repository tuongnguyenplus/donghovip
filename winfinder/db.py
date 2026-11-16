"""Lưu trữ SQLite. Lịch sử giữ lại qua các lần chạy để so sánh tăng trưởng theo tuần."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable

from winfinder.sources.adlibrary import Ad

SCHEMA = """
CREATE TABLE IF NOT EXISTS ads (
    ad_id          TEXT PRIMARY KEY,
    page_id        TEXT NOT NULL,
    page_name      TEXT,
    domain         TEXT,
    created_at     TEXT,
    start_date     TEXT,
    stop_date      TEXT,
    bodies         TEXT,
    titles         TEXT,
    captions       TEXT,
    snapshot_url   TEXT,
    platforms      TEXT,
    languages      TEXT,
    eu_total_reach INTEGER,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ads_domain ON ads(domain);
CREATE INDEX IF NOT EXISTS idx_ads_page ON ads(page_id);

-- ad đã được thấy ở nước nào (mỗi truy vấn Ad Library gắn với 1 nước)
CREATE TABLE IF NOT EXISTS ad_countries (
    ad_id   TEXT NOT NULL,
    country TEXT NOT NULL,
    PRIMARY KEY (ad_id, country)
);

-- ad khớp ngách/keyword nào
CREATE TABLE IF NOT EXISTS ad_searches (
    ad_id   TEXT NOT NULL,
    search  TEXT NOT NULL,
    keyword TEXT NOT NULL,
    PRIMARY KEY (ad_id, search, keyword)
);

-- reach theo thời gian: mỗi lần chạy ghi 1 snapshot → đo tốc độ tăng reach
CREATE TABLE IF NOT EXISTS reach_snapshots (
    ad_id          TEXT NOT NULL,
    observed_on    TEXT NOT NULL,
    eu_total_reach INTEGER NOT NULL,
    PRIMARY KEY (ad_id, observed_on)
);

CREATE TABLE IF NOT EXISTS domains (
    domain         TEXT PRIMARY KEY,
    registered_at  TEXT,
    first_cert_at  TEXT,
    is_shopify     INTEGER,
    product_count  INTEGER,
    products       TEXT,
    checked_on     TEXT
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_ads(
    conn: sqlite3.Connection,
    ads: Iterable[Ad],
    *,
    country: str,
    search: str,
    keyword: str,
    observed_on: date,
) -> int:
    today = observed_on.isoformat()
    n = 0
    for ad in ads:
        conn.execute(
            """
            INSERT INTO ads (ad_id, page_id, page_name, domain, created_at, start_date, stop_date,
                             bodies, titles, captions, snapshot_url, platforms, languages,
                             eu_total_reach, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ad_id) DO UPDATE SET
                page_name=excluded.page_name,
                domain=COALESCE(excluded.domain, ads.domain),
                stop_date=excluded.stop_date,
                eu_total_reach=COALESCE(excluded.eu_total_reach, ads.eu_total_reach),
                last_seen=excluded.last_seen
            """,
            (
                ad.ad_id, ad.page_id, ad.page_name, ad.domain, ad.created_at, ad.start_date,
                ad.stop_date, json.dumps(ad.bodies, ensure_ascii=False),
                json.dumps(ad.titles, ensure_ascii=False), json.dumps(ad.captions, ensure_ascii=False),
                ad.snapshot_url, json.dumps(ad.platforms), json.dumps(ad.languages),
                ad.eu_total_reach, today, today,
            ),
        )
        conn.execute("INSERT OR IGNORE INTO ad_countries VALUES (?,?)", (ad.ad_id, country))
        conn.execute("INSERT OR IGNORE INTO ad_searches VALUES (?,?,?)", (ad.ad_id, search, keyword))
        if ad.eu_total_reach is not None:
            conn.execute(
                "INSERT OR REPLACE INTO reach_snapshots VALUES (?,?,?)",
                (ad.ad_id, today, ad.eu_total_reach),
            )
        n += 1
    conn.commit()
    return n


def save_domain(
    conn: sqlite3.Connection,
    domain: str,
    *,
    registered_at: str | None,
    first_cert_at: str | None,
    is_shopify: bool | None,
    product_count: int | None,
    products: list | None,
    checked_on: date,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO domains VALUES (?,?,?,?,?,?,?)",
        (
            domain, registered_at, first_cert_at,
            None if is_shopify is None else int(is_shopify),
            product_count, json.dumps(products or [], ensure_ascii=False), checked_on.isoformat(),
        ),
    )
    conn.commit()
