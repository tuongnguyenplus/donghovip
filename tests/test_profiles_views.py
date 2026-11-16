from datetime import date
from pathlib import Path

import pytest

from winfinder import db, demo, link, link_report, report, views
from winfinder import profiles as pf
from winfinder.analysis.scoring import analyze
from winfinder.angles_pipeline import run_angles
from winfinder.angles_report import write as write_angles
from winfinder.config import load_config
from winfinder.expand import expand_stores
from winfinder.profiles_ui import parse_form, render_calibration, render_editor
from winfinder.sources.adlibrary import parse_ad

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 16)


# ------------------------------------------------------------------ hồ sơ
def test_default_profiles_parse_and_roundtrip(tmp_path):
    profiles, ev = pf.load(tmp_path / "p.yaml")            # tạo file mặc định
    assert [p.key for p in profiles] == ["quick", "long", "angle_now", "angle_durable"] and ev.gates
    assert [p.key for p in pf.for_kind(profiles, "angle")] == ["angle_now", "angle_durable"]
    assert [p.key for p in pf.for_kind(profiles, "product")] == ["quick", "long"]
    pf.save(tmp_path / "p.yaml", profiles, ev)
    again, ev2 = pf.load(tmp_path / "p.yaml")
    assert [(p.key, len(p.gates), len(p.weights)) for p in again] == [(p.key, len(p.gates), len(p.weights))
                                                                      for p in profiles]
    assert ev2.gates[0].value == ["scaling", "early", "evergreen"]


@pytest.mark.parametrize("bad,msg", [
    ("profiles: [{key: x, weights: [{metric: nope, weight: 1}]}]", "nope"),
    ("profiles: [{key: x, weights: [{metric: archetype, weight: 1}]}]", "chỉ số dạng số"),
    ("profiles: [{key: x, gates: [{metric: archetype, op: '>=', value: 1}]}]", "in"),
    ("profiles: [{key: x, gates: [{metric: archetype, op: in, value: [flying]}]}]", "flying"),
    ("profiles: [{key: x, gates: [{metric: alive_30, op: '>=', value: abc}]}]", "số"),
    ("profiles: [{key: 'a b'}]", "Mã hồ sơ"),
    ("profiles: [{key: a}, {key: a}]", "trùng"),
    ("profiles: [{key: x, weights: [{metric: alive_30, weight: -1}]}]", "âm"),
])
def test_invalid_profiles_rejected(bad, msg):
    with pytest.raises(pf.ProfileError, match=msg):
        pf.parse(bad)


def test_save_text_never_writes_broken_file(tmp_path):
    path = tmp_path / "p.yaml"
    pf.load(path)
    before = path.read_text()
    with pytest.raises(pf.ProfileError):
        pf.save_text(path, "profiles: [{key: 'bad key'}]")
    assert path.read_text() == before


def P(gates=(), weights=(("alive_30", 1, "high"),), strong=70, fit=50):
    return pf.Profile("t", "T", "", [pf.Gate(*g) for g in gates], [pf.Weight(*w) for w in weights], strong, fit)


def test_missing_data_is_not_failure_and_reweights():
    prof = P(gates=[("alive_30", ">=", 2), ("trend_label", "not_in", ["spike"])],
             weights=[("alive_30", 1, "high"), ("trend_score", 1, "high")])
    e = pf.evaluate(prof, {"alive_30": 5})                 # không có dữ liệu trends
    assert [g.status for g in e.gates] == ["pass", "unknown"]
    assert e.score == 100 and e.coverage == 0.5          # chỉ tính trên chỉ số có dữ liệu
    assert e.decision == "strong" and "cần kiểm tra" in e.decision_vi


def test_gate_failure_blocks_and_prefer_low():
    prof = P(gates=[("alive_30", ">=", 2)], weights=[("turnover", 1, "low")])
    e = pf.evaluate(prof, {"alive_30": 1, "turnover": 0})
    assert e.decision == "blocked" and e.gates[0].status == "fail"
    assert e.contributions[0].score == 1.0               # thay creative thấp = tốt
    assert pf.evaluate(P(weights=[("turnover", 1, "low")]), {"turnover": 2.5}).score == 0


def test_bool_and_category_gates():
    g = pf.Gate("in_season", "is", "true")
    pf.validate_gate(g)
    assert g.value is True
    assert pf.check_gate(g, {"in_season": False}).status == "fail"
    gate = pf.Gate("archetype", "in", ["scaling"])
    assert pf.check_gate(gate, {"archetype": "scaling"}).status == "pass"
    assert "Đang scale" in gate.describe()


def test_evidence_requires_known_data():
    rule = pf.EvidenceRule([pf.Gate("direct_share", ">=", 0.5)])
    assert pf.passes(rule, {"direct_share": 0.8})[0]
    assert not pf.passes(rule, {})[0]                    # thiếu dữ liệu không được tính là bằng chứng


# ------------------------------------------------------------------ dựng 3 góc nhìn trên demo
@pytest.fixture(scope="module")
def demo_reports(tmp_path_factory):
    out = tmp_path_factory.mktemp("rep")
    cfg = load_config(ROOT / "config.yaml")
    cfg.trends.geos = ["US"]
    cfg.link.min_ad_score = 0
    cfg.link.fetch_pages = False
    cfg.expand.countries = ["IE"]
    conn = db.connect(":memory:")
    for search, country, raw in demo.generate(AS_OF):
        db.upsert_ads(conn, [parse_ad(raw, cfg.exclude_domains)], country=country, search=search,
                      keyword=demo.SEARCH_KEYWORD[search], observed_on=AS_OF)
    for d, products in demo.shopify_products().items():
        db.save_domain(conn, d, registered_at=None, first_cert_at=None, is_shopify=True,
                       product_count=len(products), products=products, checked_on=AS_OF)
    expand_stores(conn, cfg, demo.DemoAdLibraryClient(AS_OF), analyze(conn, cfg, AS_OF), AS_OF,
                  resolver=demo.demo_resolver)
    stores = analyze(conn, cfg, AS_OF)
    report.write_json(stores, out / "latest.json", AS_OF)
    linked = link.link_stores(conn, cfg, stores, demo.DemoTrendsProvider(AS_OF), AS_OF,
                              extractor=demo.DemoExtractor())
    link_report.write(linked, out, AS_OF, "demo")
    write_angles(run_angles(conn, cfg, link.to_dicts(linked), demo.DemoAngleAnalyzer(), AS_OF), out, AS_OF)
    return out


def test_model_products_use_evidence_stores(demo_reports, tmp_path):
    m = views.build(demo_reports, tmp_path / "p.yaml")
    posture = m.product("posture corrector")
    assert posture.info["evidence"] == ["postureglow.com"]          # đang scale + chạy thẳng
    assert posture.info["others"] == ["spinealign.co"]              # máy xay creative → không phải bằng chứng
    assert posture.metrics["evidence_stores"] == 1 and posture.metrics["stores"] == 2
    # chỉ số sản phẩm lấy từ store bằng chứng, không bị store máy xay kéo xuống
    assert posture.metrics["survival_30"] == m.store("postureglow.com").metrics["survival_30"]


def test_profiles_separate_quick_from_long(demo_reports, tmp_path):
    m = views.build(demo_reports, tmp_path / "p.yaml")
    dec = {p.key: {e.profile: e.decision for e in p.evals} for p in m.products}
    assert dec["cat hair remover"]["quick"] == "strong"   # bùng nổ, store mới
    assert dec["cat hair remover"]["long"] == "blocked"   # chưa có ad ≥ 30 ngày
    assert dec["back straightener"]["long"] in ("fit", "strong")
    assert dec["back straightener"]["quick"] == "blocked"  # evergreen, không có đà tăng
    assert dec["star projector"]["quick"] == "blocked" and dec["star projector"]["long"] == "blocked"


def test_sort_puts_blocked_last(demo_reports, tmp_path):
    m = views.build(demo_reports, tmp_path / "p.yaml")
    order = [p.key for p in sorted(m.products, key=lambda e: e.rank("long"))]
    blocked = [p.key for p in m.products if next(e for e in p.evals if e.profile == "long").decision == "blocked"]
    assert set(order[-len(blocked):]) == set(blocked)


def test_angles_and_cross_links(demo_reports, tmp_path):
    m = views.build(demo_reports, tmp_path / "p.yaml")
    gift = next(a for a in m.angles if a.name == "Quà tặng cho mẹ")
    assert gift.metrics["angle_status"] == "rising" and gift.metrics["trend_label"] == "rising"
    html = views.render_products(m)
    assert 'href="stores.html#s-postureglow-com"' in html and 'href="opportunities.html#a-posture-corrector"' in html
    shtml = views.render_stores(m)
    assert 'id="s-postureglow-com"' in shtml and 'href="products.html#p-posture-corrector"' in shtml
    ahtml = views.render_angles(m)
    assert 'id="a-posture-corrector"' in ahtml
    assert "<form" not in html                              # bản tĩnh: không có nút đánh dấu
    assert 'name="label"' in views.render_products(m, token="t")


def test_labels_and_calibration(demo_reports, tmp_path):
    labels = {("product", "posture corrector"): "win", ("product", "star projector"): "fail",
              ("product", "cat hair remover"): "win"}
    m = views.build(demo_reports, tmp_path / "p.yaml", labels)
    st = views.label_stats(m)
    assert st["counts"] == {"win": 2, "fail": 1}
    surv = next(x for x in st["metrics"] if x["metric"] == "active_growth")
    assert surv["gap"] > 0                                  # win có đà tăng cao hơn fail
    assert "Đoán đúng" in render_calibration(st)
    assert "Chưa đủ dữ liệu" in render_calibration(views.label_stats(views.build(demo_reports, tmp_path / "p.yaml")))


def test_write_static(demo_reports, tmp_path):
    m = views.build(demo_reports, tmp_path / "p.yaml")
    views.write_static(m, tmp_path / "out")
    for f in ("products.html", "stores.html", "opportunities.html", "evaluation.json"):
        assert (tmp_path / "out" / f).exists()


# ------------------------------------------------------------------ trình sửa hồ sơ
def _form_from(profiles, evidence):
    form: dict[str, list[str]] = {}
    for p in profiles:
        form[f"{p.key}.label"] = [p.label]
        form[f"{p.key}.description"] = [p.description]
        form[f"{p.key}.strong"] = [str(p.strong)]
        form[f"{p.key}.fit"] = [str(p.fit)]
        for w in p.weights:
            form[f"{p.key}.w.{w.metric}"] = [str(w.weight)]
            form[f"{p.key}.p.{w.metric}"] = [w.prefer]
        for i, g in enumerate(p.gates):
            form[f"{p.key}.g{i}.metric"] = [g.metric]
            form[f"{p.key}.g{i}.op"] = [g.op]
            form[f"{p.key}.g{i}.value"] = [", ".join(g.value) if isinstance(g.value, list) else str(g.value)]
    for i, g in enumerate(evidence.gates):
        form[f"evidence.g{i}.metric"] = [g.metric]
        form[f"evidence.g{i}.op"] = [g.op]
        form[f"evidence.g{i}.value"] = [", ".join(g.value) if isinstance(g.value, list) else str(g.value)]
    return form


def test_editor_form_roundtrip_and_validation(tmp_path):
    profiles, ev = pf.load(tmp_path / "p.yaml")
    form = _form_from(profiles, ev)
    form["long.strong"] = ["60"]
    form["quick.w.turnover"] = ["2"]
    form["quick.p.turnover"] = ["low"]
    new, ev2 = parse_form(form, profiles)
    long = next(p for p in new if p.key == "long")
    quick = next(p for p in new if p.key == "quick")
    assert long.strong == 60 and any(w.metric == "turnover" and w.prefer == "low" for w in quick.weights)
    assert [g.metric for g in ev2.gates] == [g.metric for g in ev.gates]
    form["long.fit"] = ["90"]
    with pytest.raises(pf.ProfileError, match="Hợp"):
        parse_form(form, profiles)
    html = render_editor(profiles, ev, "tok", "x")
    assert 'name="long.strong"' in html and "Máy xay creative" in html


def test_angle_view_uses_persona_and_social_data(demo_reports, tmp_path):
    from winfinder import personas_pipeline
    from winfinder.angles_pipeline import load_doc

    cfg = load_config(ROOT / "config.yaml")
    cfg.trends.geos = ["US"]
    res = personas_pipeline.run_personas(db.connect(":memory:"), cfg, load_doc(demo_reports), AS_OF,
                                         provider=demo.DemoTrendsProvider(AS_OF),
                                         sources=[demo.DemoSocialSource(AS_OF)], verifier=demo.DemoVerifier())
    personas_pipeline.write(res, demo_reports, AS_OF)
    m = views.build(demo_reports, tmp_path / "p.yaml")
    a = next(x for x in m.angles if x.name == "Quần áo đen sạch lông")
    assert a.metrics["social_verdict"] == "confirmed" and a.metrics["persona_need_label"] == "rising"
    assert [e.profile for e in a.evals] == ["angle_now", "angle_durable"]      # hồ sơ riêng cho angle
    now = a.evals[0]
    assert now.coverage == 1.0 and now.decision == "strong"
    conf = next(x for x in m.angles if x.name == "Tự tin, dáng đẹp")
    assert conf.evals[0].decision == "blocked"                                   # không tìm thấy persona thật
    html = views.render_angles(m)
    assert "Xác thực" in html and "Lời khách hàng" in html and "reddit.com" in html
