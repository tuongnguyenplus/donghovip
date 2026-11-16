"""Chạy phân tích angle cho các sản phẩm trên trang Ứng viên."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date

from winfinder.analysis.angles import AngleAnalyzer, AngleResult, AngleStats, ad_units, angle_stats, fingerprint
from winfinder.config import Config

log = logging.getLogger(__name__)


@dataclass
class ProductAngles:
    product: str
    generic: str | None
    stores: list[str]
    units: int                     # số nội dung ad khác nhau
    assigned: int                  # số nội dung đã gán angle
    angles: list[AngleStats]
    untapped: list[dict] = field(default_factory=list)
    model: str = ""
    cached: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _group(linked: list[dict]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for x in linked:
        if not x.get("product"):
            continue
        g = groups.setdefault(x["product"], {"generic": x.get("generic_product"), "stores": [], "best": 0.0})
        g["stores"].append(x["key"])
        g["generic"] = g["generic"] or x.get("generic_product")
        g["best"] = max(g["best"], x.get("win_score") or 0.0)
    return dict(sorted(groups.items(), key=lambda kv: -kv[1]["best"]))


def run_angles(
    conn: sqlite3.Connection, cfg: Config, linked: list[dict], analyzer: AngleAnalyzer | None, today: date,
) -> list[ProductAngles]:
    ac = cfg.angles
    out = []
    for product, g in list(_group(linked).items())[: ac.max_products]:
        units, meta = ad_units(conn, g["stores"])
        if len(units) < ac.min_ads:
            log.info("[%s] chỉ %d nội dung ad — bỏ qua phân tích angle", product, len(units))
            continue
        fp = fingerprint(product, units)
        row = conn.execute("SELECT fingerprint, result FROM product_angles WHERE product = ?", (product,)).fetchone()
        cached = bool(row and row["fingerprint"] == fp)
        if cached:
            result = AngleResult.from_dict(json.loads(row["result"]))
        elif analyzer is None:
            continue
        else:
            log.info("[%s] phân tích angle %d nội dung ad (%s)", product, len(units), analyzer.model)
            result = analyzer.analyze(product, g["generic"], units)
            if result is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO product_angles VALUES (?,?,?,?,?)",
                (product, fp, json.dumps(result.to_dict(), ensure_ascii=False), result.model, today.isoformat()),
            )
            conn.commit()
        stats = angle_stats(result, units, meta, today, cfg.velocity_weeks)
        out.append(ProductAngles(
            product=product, generic=g["generic"], stores=g["stores"], units=len(units),
            assigned=sum(1 for u in units if u.ref in result.assignments),
            angles=stats, untapped=result.untapped, model=result.model, cached=cached,
        ))
    return out


def load_linked(report_dir) -> list[dict]:
    from pathlib import Path

    p = Path(report_dir) / "winners.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("stores", [])
