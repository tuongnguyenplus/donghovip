"""winfinder CLI.

  winfinder run      --config config.yaml   # collect → analyze → enrich → analyze → report
  winfinder collect  --config config.yaml   # chỉ lấy ad từ Ad Library
  winfinder analyze  --config config.yaml   # chấm điểm + xuất báo cáo từ DB hiện có
  winfinder demo     --out demo_out         # chạy với dữ liệu giả lập, không cần token
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

from winfinder import db, demo, pipeline, report
from winfinder.analysis.scoring import analyze
from winfinder.config import load_config
from winfinder.sources.adlibrary import AdLibraryClient, AdLibraryError, parse_ad

log = logging.getLogger("winfinder")


def _print_top(results, n: int = 15) -> None:
    print(f"{'#':>3}  {'điểm':>5}  {'scale':5}  {'ad':>4}  {'tuổi':>5}  creative/tuần       store")
    for i, s in enumerate(results[:n], 1):
        print(
            f"{i:>3}  {s.score:>5.1f}  {'✔' if s.scaling else '':5}  {s.ad_count:>4}  "
            f"{(str(s.age_days) + 'd') if s.age_days is not None else '–':>5}  "
            f"{' '.join(map(str, s.window['counts'])):18}  {s.domain or s.key}"
        )


def _report(conn, cfg, today: date) -> list:
    results = analyze(conn, cfg, today)
    report.write_json(results, cfg.report_dir / "latest.json", today)
    report.write_html(results, cfg.report_dir / "index.html", today)
    return results


def cmd_collect(args) -> int:
    cfg = load_config(args.config)
    today = date.fromisoformat(args.as_of) if args.as_of else date.today()
    try:
        client = AdLibraryClient(
            os.environ.get("META_AD_LIBRARY_TOKEN", ""),
            cfg.api_version,
            sleep_seconds=cfg.request_sleep_seconds,
        )
    except AdLibraryError as exc:
        log.error("%s", exc)
        return 2
    conn = db.connect(cfg.database)
    n = pipeline.collect(conn, cfg, client, today)
    log.info("Đã lưu %d ad", n)
    return 0


def cmd_analyze(args) -> int:
    cfg = load_config(args.config)
    today = date.fromisoformat(args.as_of) if args.as_of else date.today()
    conn = db.connect(cfg.database)
    if args.enrich:
        pipeline.enrich(conn, cfg, analyze(conn, cfg, today), today)
    results = _report(conn, cfg, today)
    _print_top(results)
    print(f"\nBáo cáo: {cfg.report_dir / 'index.html'}")
    return 0


def cmd_run(args) -> int:
    rc = cmd_collect(args)
    if rc:
        return rc
    args.enrich = not args.no_enrich
    return cmd_analyze(args)


def cmd_demo(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = load_config(args.config)
    cfg.database = out / "demo.db"
    cfg.report_dir = out
    if cfg.database.exists():
        cfg.database.unlink()
    today = date.fromisoformat(args.as_of) if args.as_of else date.today()
    conn = db.connect(cfg.database)
    for search, country, raw in demo.generate(today):
        db.upsert_ads(
            conn, [parse_ad(raw, cfg.exclude_domains)],
            country=country, search=search, keyword="demo", observed_on=today,
        )
    results = _report(conn, cfg, today)
    _print_top(results)
    print(f"\nBáo cáo demo: {out / 'index.html'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="winfinder", description="Tìm store/sản phẩm đang vào phase scale")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--config", default="config.yaml")
        sp.add_argument("--as-of", help="ngày tính toán (YYYY-MM-DD), mặc định hôm nay")

    sp = sub.add_parser("collect", help="lấy ad từ Meta Ad Library");  common(sp); sp.set_defaults(fn=cmd_collect)
    sp = sub.add_parser("analyze", help="chấm điểm + xuất báo cáo"); common(sp)
    sp.add_argument("--enrich", action="store_true", help="tra RDAP / crt.sh / Shopify cho top store")
    sp.set_defaults(fn=cmd_analyze)
    sp = sub.add_parser("run", help="collect + enrich + analyze"); common(sp)
    sp.add_argument("--no-enrich", action="store_true"); sp.set_defaults(fn=cmd_run)
    sp = sub.add_parser("demo", help="chạy thử với dữ liệu giả lập"); common(sp)
    sp.add_argument("--out", default="demo_out"); sp.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
