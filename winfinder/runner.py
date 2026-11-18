"""Chạy toàn bộ một lần quét: Ad Library → chấm điểm → Trends → Ứng viên → Angle.

Dùng chung cho giao diện web (`winfinder serve`) và dòng lệnh (`winfinder scan`).
Bước nào thiếu key hoặc lỗi thì ghi lý do vào log và chạy tiếp bước sau.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from datetime import date

from winfinder import angles_pipeline, angles_report, db, link, link_report, pipeline, report
from winfinder import personas_pipeline, trends_pipeline, trends_report, views
from winfinder.analysis.social import ClaudeVerifier
from winfinder.sources import apify_fb
from winfinder.sources.apify import ApifyBudget, ApifyClient
from winfinder.sources.apify import available as apify_available
from winfinder.sources.social import make_sources
from winfinder.analysis.angles import make_angle_analyzer
from winfinder.analysis.product_llm import make_extractor
from winfinder.analysis.scoring import analyze
from winfinder.config import Config
from winfinder.expand import expand_stores
from winfinder.http import make_session
from winfinder.sources.landing import resolve
from winfinder.sources.adlibrary import AdLibraryClient, AdLibraryError
from winfinder.sources.trends import TrendsError

log = logging.getLogger("winfinder.scan")

STEPS = ["collect", "trends", "link", "angles", "personas", "views"]
STEP_LABELS = {
    "collect": "Lấy ad từ Ad Library + chấm điểm store",
    "trends": "Google Trends cho các ngách",
    "link": "Ứng viên: hiểu sản phẩm, nhu cầu, traffic",
    "angles": "Angle / persona",
    "personas": "Persona: nhu cầu riêng + xác minh trên mạng xã hội",
    "views": "Chấm theo hồ sơ chiến lược (Sản phẩm / Store / Cơ hội angle)",
}


@dataclass
class ScanResult:
    done: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)   # bước → lý do
    failed: dict[str, str] = field(default_factory=dict)
    apify_spent: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed


def only_niche(cfg: Config, name: str | None) -> Config:
    """Giới hạn lần quét vào một ngách (Ad Library + chủ đề Trends cùng tên)."""
    if not name:
        return cfg
    searches = [s for s in cfg.searches if s.name.lower() == name.lower()]
    topics = [t for t in cfg.trends.topics if t.name.lower() == name.lower()]
    return replace(cfg, searches=searches, trends=replace(cfg.trends, topics=topics))


def _trends_provider(cfg: Config):
    if cfg.trends.provider == "serpapi" and not os.environ.get("SERPAPI_API_KEY"):
        log.warning("Chưa có SERPAPI_API_KEY — dùng Google Trends trực tiếp (miễn phí, có thể bị giới hạn)")
        cfg.trends.provider = "direct"
    return trends_pipeline.make_provider(cfg)


def run_scan(cfg: Config, steps: list[str] | None = None, today: date | None = None) -> ScanResult:
    steps = steps or STEPS
    today = today or date.today()
    res = ScanResult()
    conn = db.connect(cfg.database)
    budget = ApifyBudget(cfg.apify.budget_per_scan_usd)
    apify_client = ApifyClient(budget, per_run_cap=cfg.apify.per_run_cap_usd) \
        if cfg.apify.enabled and apify_available() else None
    stores = []

    def step(name: str, fn) -> None:
        if name not in steps:
            return
        log.info("▶ %s", STEP_LABELS[name])
        try:
            reason = fn()
        except Exception as exc:  # một bước lỗi không được dừng cả lần quét
            log.exception("✖ %s lỗi: %s", STEP_LABELS[name], exc)
            res.failed[name] = str(exc)
            return
        if reason:
            log.warning("⏭ Bỏ qua %s: %s", STEP_LABELS[name], reason)
            res.skipped[name] = reason
        else:
            log.info("✔ Xong: %s", STEP_LABELS[name])
            res.done.append(name)

    def collect():
        nonlocal stores
        if not cfg.searches:
            return "chưa có ngách nào được bật"
        meta_token = os.environ.get("META_AD_LIBRARY_TOKEN", "")
        use_us = cfg.apify.enabled and cfg.apify.fb_us and apify_client is not None
        if not meta_token and not use_us:
            stores = analyze(conn, cfg, today)
            report.write_json(stores, cfg.report_dir / "latest.json", today)
            report.write_html(stores, cfg.report_dir / "index.html", today)
            return "chưa có META_AD_LIBRARY_TOKEN hay APIFY_TOKEN (chỉ chấm lại dữ liệu cũ)"
        if meta_token:  # EU — API Meta miễn phí, đủ lịch sử 1 năm
            try:
                client = AdLibraryClient(meta_token, cfg.api_version, sleep_seconds=cfg.request_sleep_seconds)
                n = pipeline.collect(conn, cfg, client, today)
                log.info("EU (Meta): %d ad", n)
                if cfg.expand.enabled:
                    session = make_session()
                    st = expand_stores(conn, cfg, client, analyze(conn, cfg, today), today,
                                       resolver=lambda d: resolve(session, d, cfg.exclude_domains))
                    log.info("Mở rộng EU %d store: +%d ad từ %d page, %d trang trung gian nối về store",
                             st.stores, st.new_ads, st.pages_swept, st.presell_resolved)
            except AdLibraryError as exc:
                log.warning("EU (Meta): %s", exc)
        else:
            log.info("Bỏ qua EU: chưa có META_AD_LIBRARY_TOKEN")
        if use_us:  # US — qua Apify, chỉ thấy ad đang chạy
            us = apify_fb.collect(conn, cfg, apify_client, today)
            log.info("US (Apify): %d ad theo keyword", us.ads)
            if cfg.apify.sweep_top_stores:
                sw = apify_fb.sweep_store_pages(conn, cfg, apify_client, analyze(conn, cfg, today), today)
                log.info("US: quét trọn %d page (+%d ad), suy ra %d ad đã tắt", sw.pages_swept, sw.ads,
                         sw.stopped_inferred)
                us.skipped += sw.skipped
            for why in us.skipped:
                log.warning("US: %s", why)
        elif cfg.apify.enabled and cfg.apify.fb_us:
            log.info("Bỏ qua US: chưa có APIFY_TOKEN")
        stores = analyze(conn, cfg, today)
        pipeline.enrich(conn, cfg, stores, today)
        stores = analyze(conn, cfg, today)
        report.write_json(stores, cfg.report_dir / "latest.json", today)
        report.write_html(stores, cfg.report_dir / "index.html", today)
        log.info("%d store, %d đang scale", len(stores), sum(1 for s in stores if s.scaling))
        return None

    def trends():
        if not cfg.trends.topics:
            return "không có chủ đề nào"
        try:
            provider = _trends_provider(cfg)
        except TrendsError as exc:
            return str(exc)
        topics = trends_pipeline.run_trends(conn, cfg, provider, today)
        trends_report.write_trends(topics, cfg.report_dir, today, provider.name)
        return None if any(t.geos for t in topics) else "không lấy được dữ liệu Trends nào"

    def link_step():
        nonlocal stores
        stores = stores or analyze(conn, cfg, today)
        if not stores:
            return "chưa có store nào trong dữ liệu"
        try:
            provider = _trends_provider(cfg)
        except TrendsError as exc:
            return str(exc)
        extractor = make_extractor(cfg.link.llm_enabled, cfg.link.llm_model, cfg.link.llm_effort)
        linked = link.link_stores(conn, cfg, stores, provider, today, extractor=extractor)
        link_report.write(linked, cfg.report_dir, today, provider.name)
        return None

    def angles():
        linked = angles_pipeline.load_linked(cfg.report_dir)
        if not linked:
            return "chưa có danh sách ứng viên"
        analyzer = make_angle_analyzer(cfg.angles.enabled, cfg.angles.model, cfg.angles.effort)
        products = angles_pipeline.run_angles(conn, cfg, linked, analyzer, today)
        angles_report.write(products, cfg.report_dir, today,
                            "" if analyzer else "chưa có ANTHROPIC_API_KEY — chỉ hiện kết quả đã lưu")
        return None if analyzer else "chưa có ANTHROPIC_API_KEY"

    def personas_step():
        if not cfg.personas.enabled:
            return "đã tắt trong config"
        doc = angles_pipeline.load_doc(cfg.report_dir)
        if not doc.get("products"):
            return "chưa có phân tích angle"
        provider = None
        try:
            provider = _trends_provider(cfg) if cfg.personas.trends else None
        except TrendsError as exc:
            log.warning("Trends cho persona: %s", exc)
        sources, skipped = make_sources(cfg.personas.sources, dict(os.environ))
        verifier = None
        if cfg.personas.verify and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            verifier = ClaudeVerifier(cfg.personas.model, cfg.personas.effort)
        res = personas_pipeline.run_personas(conn, cfg, doc, today, provider=provider, sources=sources,
                                             verifier=verifier, skipped_sources=skipped)
        personas_pipeline.write(res, cfg.report_dir, today)
        if not sources:
            return "chưa có nguồn mạng xã hội (REDDIT_CLIENT_ID/SECRET hoặc YOUTUBE_API_KEY) — chỉ đo nhu cầu persona"
        return None

    def views_step():
        model = views.build(cfg.report_dir, cfg.profiles_file, db.get_labels(conn))
        views.write_static(model, cfg.report_dir)
        log.info("%d sản phẩm, %d store, %d angle đã chấm theo %d hồ sơ",
                 len(model.products), len(model.stores), len(model.angles), len(model.profiles))
        return None if model.stores else "chưa có dữ liệu store"

    step("collect", collect)
    step("trends", trends)
    step("link", link_step)
    step("angles", angles)
    step("personas", personas_step)
    step("views", views_step)
    if budget.runs:
        log.info(budget.summary())
    res.apify_spent = round(budget.spent_usd, 3)
    return res
