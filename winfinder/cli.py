"""winfinder CLI.

  winfinder run      --config config.yaml   # collect → analyze → enrich → analyze → report
  winfinder collect  --config config.yaml   # chỉ lấy ad từ Ad Library
  winfinder analyze  --config config.yaml   # chấm điểm + xuất báo cáo từ DB hiện có
  winfinder trends   --config config.yaml   # Google Trends cho các chủ đề
  winfinder link     --config config.yaml   # store → keyword sản phẩm → Trends → kết luận
  winfinder angles   --config config.yaml   # angle/persona của từng sản phẩm, angle nào đang lên
  winfinder demo     --out demo_out         # chạy với dữ liệu giả lập, không cần token
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

from winfinder import (angles_pipeline, angles_report, db, demo, link, link_report, pipeline, report,
                       trends_pipeline, trends_report)
from winfinder.analysis.angles import make_angle_analyzer
from winfinder.analysis.product_llm import make_extractor
from winfinder.config import TrendTopic
from winfinder.sources.trends import TrendsError
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
            country=country, search=search, keyword=demo.SEARCH_KEYWORD[search], observed_on=today,
        )
    for domain, products in demo.shopify_products().items():
        db.save_domain(conn, domain, registered_at=None, first_cert_at=None, is_shopify=True,
                       product_count=len(products), products=products, checked_on=today)
    results = _report(conn, cfg, today)
    _print_top(results)

    cfg.trends.autocomplete = False
    cfg.trends.geos = ["US", "GB", "DE"]
    cfg.trends.topics = demo.TREND_TOPICS
    topics = trends_pipeline.run_trends(conn, cfg, demo.DemoTrendsProvider(today), today)
    trends_report.write_trends(topics, out, today, "demo")
    print()
    _print_trends(topics)

    cfg.link.min_ad_score = 0
    linked = link.link_stores(conn, cfg, results, demo.DemoTrendsProvider(today), today,
                              extractor=demo.DemoExtractor(), page_fetcher=lambda url: None)
    link_report.write(linked, out, today, "demo")
    print()
    _print_linked(linked)

    products = angles_pipeline.run_angles(conn, cfg, link.to_dicts(linked), demo.DemoAngleAnalyzer(), today)
    angles_report.write(products, out, today, "dữ liệu giả lập")
    print()
    _print_angles(products)
    print(f"\nBáo cáo demo: {out / 'winners.html'} · {out / 'angles.html'} · {out / 'index.html'} · {out / 'trends.html'}")
    return 0


def _print_linked(linked) -> None:
    print(f"{'tổng':>5}  {'ad':>4}  {'trend':>5}  {'sản phẩm':24} store — kết luận")
    for x in linked:
        ts = f"{x.trend_score:.0f}" if x.trend_score is not None else "–"
        print(f"{x.win_score:>5.0f}  {x.ad_score:>4.0f}  {ts:>5}  {(x.product or '–'):24} "
              f"{x.domain or x.key} — {x.verdict_text.split(' — ')[0]}"
              + (" (theo nhu cầu: " + x.problem_keywords[0] + ")" if x.verdict_basis == "need" else ""))


def _print_angles(products) -> None:
    for p in products:
        print(f"[{p.generic or p.product}] {p.units} nội dung ad · {len(p.stores)} store")
        for a in p.angles:
            print(f"   {a.status_vi:14} {a.recent_share * 100:>4.0f}%  {a.angle.name} — {a.angle.persona}")
        for u in p.untapped:
            print(f"   {'chưa khai thác':14}        {u.get('name')} — {u.get('persona')}")


def cmd_angles(args) -> int:
    cfg = load_config(args.config)
    today = date.fromisoformat(args.as_of) if args.as_of else date.today()
    linked = angles_pipeline.load_linked(cfg.report_dir)
    if not linked:
        log.error("Chưa có %s — chạy 'winfinder link' trước", cfg.report_dir / "winners.json")
        return 1
    analyzer = make_angle_analyzer(cfg.angles.enabled, cfg.angles.model, cfg.angles.effort)
    conn = db.connect(cfg.database)
    products = angles_pipeline.run_angles(conn, cfg, linked, analyzer, today)
    note = "" if analyzer else "chưa có ANTHROPIC_API_KEY — chỉ hiện kết quả đã lưu"
    angles_report.write(products, cfg.report_dir, today, note)
    _print_angles(products)
    print(f"\nBáo cáo: {cfg.report_dir / 'angles.html'}")
    return 0 if analyzer or products else 2


def cmd_link(args) -> int:
    cfg = load_config(args.config)
    today = date.fromisoformat(args.as_of) if args.as_of else date.today()
    if args.provider:
        cfg.trends.provider = args.provider
    if args.only_scaling:
        cfg.link.only_scaling = True
    try:
        provider = trends_pipeline.make_provider(cfg)
    except TrendsError as exc:
        log.error("%s", exc)
        return 2
    conn = db.connect(cfg.database)
    stores = analyze(conn, cfg, today)
    if not stores:
        log.error("DB chưa có ad nào — chạy 'winfinder collect' trước")
        return 1
    extractor = None if args.no_llm else make_extractor(
        cfg.link.llm_enabled, cfg.link.llm_model, cfg.link.llm_effort)
    linked = link.link_stores(conn, cfg, stores, provider, today, extractor=extractor)
    link_report.write(linked, cfg.report_dir, today, provider.name)
    _print_linked(linked)
    print(f"\nBáo cáo: {cfg.report_dir / 'winners.html'}")
    return 0


def _print_trends(topics) -> None:
    print(f"{'điểm':>5}  {'phân loại':24} {'YoY':>6} {'xu hướng':>8}  chủ đề — lan rộng")
    for t in topics:
        h = t.home
        if not h:
            print(f"{'–':>5}  {'không có dữ liệu':24} {'':>6} {'':>8}  {t.name}")
            continue
        s = h.stats
        pct = lambda x: "–" if x is None else f"{x * 100:+.0f}%"  # noqa: E731
        print(f"{h.score:>5.0f}  {s['label_vi']:24} {pct(s['yoy']):>6} {pct(s['trend_growth']):>8}  "
              f"{t.name} — {t.spread.get('stage', '')}")


def cmd_trends(args) -> int:
    cfg = load_config(args.config)
    today = date.fromisoformat(args.as_of) if args.as_of else date.today()
    if args.provider:
        cfg.trends.provider = args.provider
    if args.geos:
        cfg.trends.geos = [g.strip().upper() for g in args.geos.split(",") if g.strip()]
    topics = cfg.trends.topics
    if args.keyword:
        kws = [k.strip() for k in args.keyword.split(",") if k.strip()]
        topics = [TrendTopic(name=kws[0], seeds=kws[:1], keywords=kws[1:])]
    elif args.topic:
        topics = [t for t in topics if t.name == args.topic]
        if not topics:
            log.error("Không có chủ đề '%s' trong config", args.topic)
            return 2
    try:
        provider = trends_pipeline.make_provider(cfg)
    except TrendsError as exc:
        log.error("%s", exc)
        return 2
    conn = db.connect(cfg.database)
    results = trends_pipeline.run_trends(conn, cfg, provider, today, topics=topics, force=args.force)
    trends_report.write_trends(results, cfg.report_dir, today, provider.name)
    _print_trends(results)
    print(f"\nBáo cáo: {cfg.report_dir / 'trends.html'}")
    return 0 if any(t.geos for t in results) else 1


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
    sp = sub.add_parser("trends", help="phân tích Google Trends cho các chủ đề"); common(sp)
    sp.add_argument("--topic", help="chỉ chạy một chủ đề trong config")
    sp.add_argument("--keyword", help="chủ đề nhanh: 'keyword chính, keyword phụ, ...'")
    sp.add_argument("--geos", help="ghi đè thị trường, vd US,GB,DE (nước đầu tiên là thị trường gốc)")
    sp.add_argument("--provider", choices=["serpapi", "direct", "csv"])
    sp.add_argument("--force", action="store_true", help="bỏ qua cache")
    sp.set_defaults(fn=cmd_trends)
    sp = sub.add_parser("link", help="nối store đang scale với Trends của sản phẩm"); common(sp)
    sp.add_argument("--provider", choices=["serpapi", "direct", "csv"])
    sp.add_argument("--only-scaling", action="store_true", help="chỉ store có nhãn SCALE")
    sp.add_argument("--no-llm", action="store_true", help="không gọi Claude, chỉ dùng heuristic")
    sp.set_defaults(fn=cmd_link)
    sp = sub.add_parser("angles", help="chia angle/persona cho sản phẩm trên trang Ứng viên (Claude)"); common(sp)
    sp.set_defaults(fn=cmd_angles)
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
