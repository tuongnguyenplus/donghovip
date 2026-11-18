"""3 góc nhìn liên kết qua lại: Sản phẩm ↔ Store ↔ Cơ hội angle, chấm theo mọi hồ sơ chiến lược.

  - Sản phẩm: tổng hợp từ các store BẰNG CHỨNG (đạt `evidence` trong profiles.yaml — vd đang scale + chạy thẳng).
    Store không đạt vẫn liệt kê, kèm lý do. Rộng hay hẹp do bằng chứng quyết định, không chia theo loại/giá.
  - Store: từng store, bấm sang sản phẩm của nó.
  - Cơ hội angle: angle × sản phẩm — "winning product là một dạng angle/persona win mang tính thời điểm".

Dữ liệu đọc từ báo cáo đã có (latest.json, winners.json, angles.json) → tính lại trong tích tắc khi sửa hồ sơ.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from winfinder import profiles as pf
from winfinder.report import page

ARCH_RANK = ["scaling", "evergreen", "early", "steady", "observing", "testing", "treadmill", "declining", "stopped"]
DECISION_COLORS = {"strong": "#1a7f4b", "fit": "#2c6fb7", "weak": "#8a8a85", "blocked": "#c0392b"}
LABEL_VI = {"win": "Đã win", "fail": "Đã fail", "unsure": "Chưa rõ"}
LABEL_COLORS = {"win": "#1a7f4b", "fail": "#c0392b", "unsure": "#8a8a85"}


def _e(x) -> str:
    return html.escape(str(x), quote=True) if x is not None else ""


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"


@dataclass
class Entity:
    kind: str                         # product | store | angle
    key: str
    name: str
    metrics: dict[str, Any]
    evals: list[pf.Evaluation] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    @property
    def anchor(self) -> str:
        return {"product": "p-", "store": "s-", "angle": "a-"}[self.kind] + slug(self.key)

    @property
    def best(self) -> pf.Evaluation | None:
        return pf.best_fit(self.evals)

    def score(self, profile: str | None) -> float:
        if profile:
            return next((e.score for e in self.evals if e.profile == profile), 0.0)
        b = self.best
        return b.score if b else max((e.score for e in self.evals), default=0.0) - 100

    def rank(self, profile: str | None) -> tuple:
        """Xếp: hợp (đủ dữ liệu) → hợp nhưng còn điều kiện chưa rõ → chưa hợp → không đạt; cùng nhóm theo điểm."""
        order = {"strong": 0, "fit": 0, "weak": 2, "blocked": 3}

        def key(e: pf.Evaluation) -> tuple:
            o = order[e.decision]
            if o == 0 and e.unknown_gates:
                o = 1
            return (o, -e.score)

        if not profile:
            return min((key(e) for e in self.evals), default=(4, 0))
        e = next((x for x in self.evals if x.profile == profile), None)
        return key(e) if e else (4, 0)


@dataclass
class Model:
    as_of: str
    products: list[Entity]
    stores: list[Entity]
    angles: list[Entity]
    profiles: list[pf.Profile]
    evidence: pf.EvidenceRule

    def product(self, key: str) -> Entity | None:
        return next((p for p in self.products if p.key == key), None)

    def store(self, key: str) -> Entity | None:
        return next((s for s in self.stores if s.key == key), None)


# ------------------------------------------------------------------ dựng chỉ số
def _read(report_dir: Path, name: str) -> dict:
    p = report_dir / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _growth(hist: list[int]) -> float | None:
    if len(hist) < 6:
        return None
    recent, before = sum(hist[-2:]) / 2, sum(hist[-6:-3]) / 3
    return round((recent + 1) / (before + 1), 2)


def _trend_metrics(linked: dict | None) -> dict[str, Any]:
    if not linked:
        return {}
    t, n, tr = linked.get("trend") or {}, linked.get("need_trend") or {}, linked.get("traffic") or {}
    return {
        "trend_label": t.get("label"), "trend_score": linked.get("trend_score"), "trend_yoy": t.get("yoy"),
        "trend_growth": t.get("trend_growth"), "seasonal_amplitude": t.get("seasonal_amplitude"),
        "in_season": t.get("in_season") if t else None,
        "need_label": n.get("label"), "need_score": linked.get("need_score"),
        "traffic": tr.get("verdict"), "brand_ratio": tr.get("brand_ratio"),
        "competitors": linked.get("competitors"),
        "ig_engagement": (linked.get("instagram") or {}).get("engagement_ratio"),
        "ig_posts_30d": (linked.get("instagram") or {}).get("posts_30d"),
    }


def store_metrics(s: dict, linked: dict | None, angles: list[dict]) -> dict[str, Any]:
    d = s.get("durability") or {}
    m = {
        "active_now": d.get("active_now"), "active_growth": d.get("active_growth"),
        "survival_30": d.get("survival_30"), "alive_30": d.get("alive_30"), "alive_60": d.get("alive_60"),
        "dup_share": d.get("dup_share"), "turnover": d.get("turnover"), "direct_share": s.get("direct_share"),
        "archetype": s.get("archetype"), "store_age_days": s.get("age_days"), "pages": s.get("pages"),
        "countries": len(s.get("countries") or []),
    }
    m.update(_trend_metrics(linked))
    mine = [a for a in angles if s["key"] in (a.get("stores") or [])]
    if angles:
        m["angles_total"] = len(mine)
        m["angles_rising"] = sum(1 for a in mine if a["status"] in ("rising", "new"))
    return m


def product_metrics(ev_stores: list[dict], all_stores: list[dict], linked: list[dict],
                    angles: list[dict]) -> dict[str, Any]:
    base = ev_stores or all_stores
    durs = [s.get("durability") or {} for s in base]
    hist = [sum(x) for x in zip(*[s.get("history") or [] for s in base])] if base else []
    active = sum(d.get("active_now") or 0 for d in durs)
    aged = sum(d.get("aged_ads") or 0 for d in durs)

    def wmean(key: str, w: str) -> float | None:
        pairs = [(d.get(key), d.get(w) or 0) for d in durs if d.get(key) is not None]
        tot = sum(x for _, x in pairs)
        return round(sum(v * x for v, x in pairs) / tot, 3) if tot else None

    best_arch = min((s.get("archetype") for s in base if s.get("archetype") in ARCH_RANK),
                    key=ARCH_RANK.index, default=None)
    direct = [(s.get("direct_share"), (s.get("durability") or {}).get("active_now") or 0) for s in base
              if s.get("direct_share") is not None]
    traffic = [(x.get("traffic") or {}) for x in linked]
    ratios = sorted(t["brand_ratio"] for t in traffic if t.get("brand_ratio") is not None)
    m = {
        "evidence_stores": len(ev_stores), "stores": len(all_stores), "competitors": max(len(all_stores) - 1, 0),
        "active_now": active, "active_growth": _growth(hist),
        "survival_30": wmean("survival_30", "aged_ads") if aged else None,
        "alive_30": sum(d.get("alive_30") or 0 for d in durs), "alive_60": sum(d.get("alive_60") or 0 for d in durs),
        "dup_share": wmean("dup_share", "active_now"),
        "turnover": round(sum(d.get("launches_4w") or 0 for d in durs) / max(active, 1), 2) if durs else None,
        "direct_share": (round(sum(v * w for v, w in direct) / sum(w for _, w in direct), 3)
                         if direct and sum(w for _, w in direct) else None),
        "archetype": best_arch,
        "store_age_days": max((s.get("age_days") for s in all_stores if s.get("age_days") is not None), default=None),
        "pages": sum(s.get("pages") or 0 for s in base),
        "countries": len(set().union(*[set(s.get("countries") or []) for s in base])) if base else 0,
        "traffic_confirmed": sum(1 for t in traffic if t.get("verdict") == "confirmed"),
        "brand_ratio": ratios[len(ratios) // 2] if ratios else None,
    }
    first = next((x for x in linked if x.get("trend") or x.get("need_trend")), None)
    tm = _trend_metrics(first)
    tm.pop("competitors", None)
    tm.pop("traffic", None)
    tm.pop("brand_ratio", None)
    igs = sorted(x["instagram"]["engagement_ratio"] for x in linked
                 if (x.get("instagram") or {}).get("engagement_ratio") is not None)
    tm["ig_engagement"] = igs[len(igs) // 2] if igs else None
    tm["ig_posts_30d"] = sum((x.get("instagram") or {}).get("posts_30d") or 0 for x in linked) or None
    m.update(tm)
    if angles:
        m["angles_total"] = len(angles)
        m["angles_rising"] = sum(1 for a in angles if a["status"] in ("rising", "new"))
    return m


def build(report_dir: str | Path, profiles_path: str | Path, labels: dict | None = None) -> Model:
    report_dir = Path(report_dir)
    profiles, evidence = pf.load(profiles_path)
    latest = _read(report_dir, "latest.json")
    winners = _read(report_dir, "winners.json")
    angles_doc = _read(report_dir, "angles.json")
    personas = {x["key"]: x for x in _read(report_dir, "personas.json").get("personas", [])}
    labels = labels or {}
    stores_raw = {s["key"]: s for s in latest.get("stores", [])}
    linked = {x["key"]: x for x in winners.get("stores", [])}
    angles_by_product = {p["product"]: p for p in angles_doc.get("products", [])}

    def evaluate(ent: Entity) -> Entity:
        ent.evals = [pf.evaluate(p, ent.metrics) for p in pf.for_kind(profiles, ent.kind)]
        ent.info["label"] = labels.get((ent.kind, ent.key))
        return ent

    # --- store
    stores: list[Entity] = []
    for key, s in stores_raw.items():
        lk = linked.get(key)
        prod = (lk or {}).get("product")
        ang = (angles_by_product.get(prod) or {}).get("angles", []) if prod else []
        m = store_metrics(s, lk, ang)
        ok, ev_gates = pf.passes(evidence, m)
        stores.append(evaluate(Entity("store", key, s.get("domain") or (s.get("page_names") or [key])[0], m, info={
            "product": prod, "generic": (lk or {}).get("generic_product"), "archetype_vi": s.get("archetype_vi"),
            "history": s.get("history") or [], "landing_mix": s.get("landing_mix") or {},
            "via_domains": s.get("via_domains") or [], "page_names": s.get("page_names") or [],
            "evidence": ok, "evidence_gates": ev_gates, "domain": s.get("domain"),
            "angles": [a["angle"]["name"] for a in ang if key in (a.get("stores") or [])],
        })))
    by_key = {e.key: e for e in stores}

    # --- sản phẩm
    groups: dict[str, list[str]] = {}
    for key, lk in linked.items():
        if lk.get("product") and key in stores_raw:
            groups.setdefault(lk["product"], []).append(key)
    products: list[Entity] = []
    for prod, keys in groups.items():
        all_s = [stores_raw[k] for k in keys]
        ev = [stores_raw[k] for k in keys if by_key[k].info["evidence"]]
        ang_doc = angles_by_product.get(prod) or {}
        ang = ang_doc.get("angles", [])
        m = product_metrics(ev, all_s, [linked[k] for k in keys], ang)
        generic = next((linked[k].get("generic_product") for k in keys if linked[k].get("generic_product")), None)
        hist = [sum(x) for x in zip(*[s.get("history") or [] for s in (ev or all_s)])]
        products.append(evaluate(Entity("product", prod, generic or prod, m, info={
            "keyword": prod, "evidence": [k for k in keys if by_key[k].info["evidence"]],
            "others": [k for k in keys if not by_key[k].info["evidence"]], "history": hist,
            "angles": [a["angle"]["name"] for a in ang], "untapped": ang_doc.get("untapped", []),
            "persona": next((linked[k].get("persona") for k in keys if linked[k].get("persona")), None),
            "problem": next((linked[k]["problem_keywords"][0] for k in keys if linked[k].get("problem_keywords")), None),
            "basis": "evidence" if ev else "all",
        })))

    # --- cơ hội angle
    angles: list[Entity] = []
    for prod, doc in angles_by_product.items():
        pe = next((p for p in products if p.key == prod), None)
        for a in doc.get("angles", []):
            d = a["angle"]
            m = {
                "angle_status": a["status"], "angle_share": a.get("recent_share"),
                "angle_stores": len(a.get("stores") or []), "active_growth": a.get("growth_ratio"),
                "survival_30": a.get("survival_30"), "alive_30": a.get("alive_30"),
                "active_now": (a.get("history") or [None])[-1],
            }
            per = personas.get(f"{prod}|{d['id']}") or {}
            ver, soc, need = per.get("verification") or {}, per.get("social") or {}, per.get("need") or {}
            m.update({
                "persona_need_label": need.get("label"), "persona_need_score": per.get("need_score"),
                "persona_need_yoy": need.get("yoy"),
                "social_verdict": ver.get("verdict") if ver.get("verdict") in pf.SOCIAL_VERDICTS else None,
                "social_score": per.get("social_score"),
                "social_volume": (round(soc["volume_90"] * (ver.get("relevant_share") if ver.get("relevant_share")
                                                            is not None else soc.get("match_share", 0)))
                                  if soc else None),
                "social_growth": soc.get("growth") if soc else None,
            })
            if pe:  # nhu cầu và bằng chứng của sản phẩm áp cho angle
                for k in ("trend_label", "trend_score", "trend_yoy", "trend_growth", "seasonal_amplitude",
                          "in_season", "need_label", "need_score", "evidence_stores", "competitors"):
                    m[k] = pe.metrics.get(k)
            angles.append(evaluate(Entity("angle", f"{prod}|{d['id']}", d["name"], m, info={
                "product": prod, "generic": doc.get("generic") or prod, "persona": d["persona"],
                "pain": d["pain_point"], "promise": d["promise"], "hook": d["hook_type"],
                "status_vi": a["status_vi"], "history": a.get("history") or [], "stores": a.get("stores") or [],
                "sample": (a.get("samples") or [""])[0],
                "pain_keywords": d.get("pain_keywords") or [], "persona_result": per,
            })))

    order = lambda e: e.rank(None)  # noqa: E731
    return Model(latest.get("as_of") or winners.get("as_of") or str(date.today()),
                 sorted(products, key=order), sorted(stores, key=order), sorted(angles, key=order),
                 profiles, evidence)


# ------------------------------------------------------------------ hiển thị
CSS = """
.lbl{display:inline-block;color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap}
.pchips{display:flex;flex-wrap:wrap;gap:6px}
.pchip{border:1px solid var(--line);border-radius:8px;padding:4px 8px;font-size:12px;min-width:118px}
.pchip b{font-size:16px;font-variant-numeric:tabular-nums;margin-right:4px}
.pchip.best{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.bar{display:block;height:4px;border-radius:2px;margin-top:3px;background:var(--chip)}
.bar i{display:block;height:4px;border-radius:2px}
.why{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));margin-top:8px}
.why h4{margin:0 0 4px;font-size:13px}
.why table{min-width:0} .why td,.why th{padding:3px 6px;font-size:12px;border-bottom:1px solid var(--line)}
.g-pass{color:#1a7f4b} .g-fail{color:#c0392b} .g-unknown{color:#a86b00}
.evid{margin:6px 0 0;padding-left:18px;text-align:left;min-width:220px} .evid li{margin:2px 0}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px;font-size:13px}
.tools a.on{font-weight:700;text-decoration:underline}
.labelbox{display:inline-flex;gap:4px;margin:4px 0 0;flex-wrap:wrap}
.labelbox button{font-size:11px;padding:2px 8px;border-radius:6px;border:1px solid var(--line);background:transparent;
 color:var(--fg);cursor:pointer}
.sub{color:var(--muted);font-size:12px}
.sp{color:var(--accent)}
"""


def _spark(values: list[float], w: int = 110, h: int = 26) -> str:
    vals = list(values)[-12:]
    if not vals:
        return ""
    top = max(vals) or 1
    step = w / max(len(vals) - 1, 1)
    p = " ".join(f"{i * step:.1f},{h - 2 - (v / top) * (h - 4):.1f}" for i, v in enumerate(vals))
    return (f'<svg class="sp" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-label="Ad đang sống 12 tuần: {", ".join(map(str, vals))}">'
            f'<polyline points="{p}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>')


def _chips(ent: Entity) -> str:
    best = ent.best
    out = []
    for e in ent.evals:
        c = DECISION_COLORS[e.decision]
        cov = "" if e.coverage >= 0.8 else f' · dữ liệu {e.coverage:.0%}'
        out.append(
            f'<div class="pchip{" best" if best and e.profile == best.profile else ""}" title="{_e(e.decision_vi)}">'
            f'<b>{e.score:.0f}</b>{_e(e.label)}<div class="sub" style="color:{c}">{_e(e.decision_vi)}{cov}</div>'
            f'<span class="bar"><i style="width:{e.score:.0f}%;background:{c}"></i></span></div>')
    return f'<div class="pchips">{"".join(out)}</div>'


def _why(ent: Entity) -> str:
    blocks = []
    for e in ent.evals:
        gates = "".join(
            f'<tr><td class="g-{g.status}">{ {"pass": "✓", "fail": "✗", "unknown": "?"}[g.status]}</td>'
            f'<td>{_e(g.gate)}</td><td class="num">{_e(g.actual)}</td></tr>' for g in e.gates)
        rows = "".join(
            f'<tr><td>{_e(c.label)}</td><td class="num">{_e(c.value)}</td>'
            f'<td class="num">{"–" if c.score is None else f"{c.points:.1f}"}</td></tr>'
            for c in sorted(e.contributions, key=lambda c: -c.points))
        blocks.append(
            f'<div><h4>{_e(e.label)} — {e.score:.0f} điểm ({_e(e.decision_vi)})</h4>'
            f'<table><tbody>{gates or "<tr><td colspan=3 class=muted>không có điều kiện</td></tr>"}</tbody></table>'
            f'<table style="margin-top:6px"><thead><tr><th>Chỉ số</th><th class="num">Giá trị</th>'
            f'<th class="num">Điểm</th></tr></thead><tbody>{rows}</tbody></table>'
            f'<div class="sub">độ phủ dữ liệu {e.coverage:.0%} — chỉ số “–” không được tính</div></div>')
    return f'<details><summary>Vì sao?</summary><div class="why">{"".join(blocks)}</div></details>'


def _label(ent: Entity, token: str | None) -> str:
    cur = ent.info.get("label")
    badge = (f' <span class="lbl" style="background:{LABEL_COLORS[cur]}">{LABEL_VI[cur]}</span>' if cur else "")
    if not token:
        return badge
    btn_text = {"win": "✓ win", "fail": "✗ fail", "unsure": "? chưa rõ"}
    btns = "".join(
        f'<button class="ghost" name="label" value="{k}" type="submit" title="Đánh dấu thực tế: {LABEL_VI[k]}'
        f' (bấm lại để bỏ)">{btn_text[k]}</button>' for k in LABEL_VI)
    return (f'{badge}<form class="labelbox" method="post" action="/label">'
            f'<input type="hidden" name="token" value="{token}"><input type="hidden" name="kind" value="{ent.kind}">'
            f'<input type="hidden" name="key" value="{_e(ent.key)}">{btns}</form>')


def _tools(model: Model, current: str | None, page_name: str, interactive: bool) -> str:
    if not interactive:
        return ""
    links = [f'<a href="{page_name}" class="{"on" if not current else ""}">Hợp nhất</a>'] + [
        f'<a href="{page_name}?sort={p.key}" class="{"on" if current == p.key else ""}">{_e(p.label)}</a>'
        for p in model.profiles]
    return f'<div class="tools">Xếp theo: {" · ".join(links)} · <a href="/profiles">Sửa hồ sơ</a></div>'


def _sorted(items: list[Entity], sort: str | None) -> list[Entity]:
    return sorted(items, key=lambda e: e.rank(sort))


def _store_link(model: Model, key: str) -> str:
    s = model.store(key)
    if not s:
        return _e(key)
    return f'<a href="stores.html#{s.anchor}">{_e(s.name)}</a>'


def _intro(model: Model) -> str:
    prof = " · ".join(f"<b>{_e(p.label)}</b>: {_e(p.description)}" for p in model.profiles)
    ev = "; ".join(g.describe() for g in model.evidence.gates) or "mọi store"
    return (f'<div class="muted">Dữ liệu đến {_e(model.as_of)} · {prof}</div>'
            f'<div class="muted">Store bằng chứng: {_e(ev)}</div>')


def render_products(model: Model, token: str | None = None, sort: str | None = None) -> str:
    rows = []
    for p in _sorted(model.products, sort):
        m, i = p.metrics, p.info
        ev = "".join(f"<li>✓ {_store_link(model, k)} <span class='sub'>{_e(model.store(k).info['archetype_vi'] or '')}"
                     f"</span></li>" for k in i["evidence"])
        others = "".join(
            f"<li class='sub'>✗ {_store_link(model, k)} — "
            f"{_e('; '.join(g.gate + ' (' + g.actual + ')' for g in model.store(k).info['evidence_gates'] if g.status != 'pass'))}"
            f"</li>" for k in i["others"])
        angles = ", ".join(f'<a href="opportunities.html#a-{slug(p.key)}">{_e(a)}</a>' for a in i["angles"][:4])
        basis = "" if i["basis"] == "evidence" else ' <span class="lbl" style="background:#a86b00">chưa có store bằng chứng</span>'
        rows.append(f"""<tr id="{p.anchor}">
<td><div class="store">{_e(p.name)}</div><div class="sub">keyword “{_e(i['keyword'])}”{(' · nhu cầu “' + _e(i['problem']) + '”') if i['problem'] else ''}</div>
{_label(p, token)}{basis}</td>
<td>{_chips(p)}{_why(p)}</td>
<td class="num"><b>{m['evidence_stores']}</b> / {m['stores']}
<details><summary>store</summary><ul class="evid">{ev}{others}</ul></details></td>
<td>{_spark(i['history'])}<div class="sub">{m.get('active_now') or 0} ad đang sống · đà {pf.METRICS['active_growth'].show(m.get('active_growth'))}</div></td>
<td>{_e(pf.METRICS['trend_label'].show(m.get('trend_label')))}<div class="sub">nhu cầu gốc: {_e(pf.METRICS['need_label'].show(m.get('need_label')))}</div></td>
<td class="sub">{angles or '–'}</td>
</tr>""")
    body = (f"<style>{CSS}</style>{_intro(model)}{_tools(model, sort, 'products.html', bool(token))}"
            '<div class="wrap"><table><thead><tr><th>Sản phẩm</th><th>Hợp chiến lược nào</th>'
            '<th class="num">Store bằng chứng</th><th>Ad đang sống (store bằng chứng)</th><th>Nhu cầu</th>'
            f'<th>Angle</th></tr></thead><tbody>{"".join(rows) or "<tr><td colspan=6 class=muted>Chưa có dữ liệu — chạy quét trước.</td></tr>"}</tbody></table></div>')
    return page("Sản phẩm", body, active="products.html")


def render_stores(model: Model, token: str | None = None, sort: str | None = None) -> str:
    rows = []
    for s in _sorted(model.stores, sort):
        m, i = s.metrics, s.info
        prod = (f'<a href="products.html#p-{slug(i["product"])}">{_e(i["generic"] or i["product"])}</a>'
                if i["product"] else '<span class="muted">chưa nối sản phẩm</span>')
        ev = ('<span class="lbl" style="background:#1a7f4b">bằng chứng</span>' if i["evidence"] else "")
        landing = " · ".join(f"{k} {v}" for k, v in i["landing_mix"].items())
        rows.append(f"""<tr id="{s.anchor}">
<td><div class="store">{_e(s.name)}</div><div class="sub">{m.get('pages') or 1} page · {_e(landing)}
{(' · qua ' + _e(', '.join(i['via_domains']))) if i['via_domains'] else ''}</div>{ev}{_label(s, token)}</td>
<td>{prod}<div class="sub">{_e(', '.join(i['angles'][:3]))}</div></td>
<td>{_chips(s)}{_why(s)}</td>
<td>{_spark(i['history'])}<div class="sub">{_e(i['archetype_vi'] or '')}</div></td>
<td class="num">{pf.METRICS['survival_30'].show(m.get('survival_30'))}<div class="sub">{m.get('alive_30') or 0} ad ≥30 ngày</div></td>
<td>{_e(pf.METRICS['traffic'].show(m.get('traffic')))}</td>
</tr>""")
    body = (f"<style>{CSS}</style>{_intro(model)}{_tools(model, sort, 'stores.html', bool(token))}"
            '<div class="wrap"><table><thead><tr><th>Store</th><th>Sản phẩm · angle</th><th>Hợp chiến lược nào</th>'
            '<th>Ad đang sống</th><th class="num">Sống sót 30 ngày</th><th>Traffic</th></tr></thead>'
            f'<tbody>{"".join(rows) or "<tr><td colspan=6 class=muted>Chưa có dữ liệu.</td></tr>"}</tbody></table></div>')
    return page("Store", body, active="stores.html")


SOCIAL_COLORS = {"confirmed": "#1a7f4b", "partial": "#a86b00", "weak": "#8a8a85", "not_found": "#c0392b",
                 "unverified": "#8a8a85"}


def _persona_need(a: Entity) -> str:
    per = a.info.get("persona_result") or {}
    need = per.get("need") or {}
    kws = ", ".join(a.info.get("pain_keywords") or [])
    if not need:
        return f'<div class="sub">nhu cầu persona: chưa đo{(" (" + _e(kws) + ")") if kws else ""}</div>'
    return (f'<div class="sub">nhu cầu persona “{_e(kws)}”: <b>{_e(need.get("label_vi"))}</b>'
            f' · {pf.METRICS["persona_need_yoy"].show(need.get("yoy"))} so cùng kỳ</div>')


def _social(a: Entity) -> str:
    per = a.info.get("persona_result") or {}
    ver, soc = per.get("verification") or {}, per.get("social") or {}
    if not ver:
        return '<span class="muted">chưa xác minh — chạy bước Persona</span>'
    v = ver.get("verdict", "unverified")
    head = (f'<span class="lbl" style="background:{SOCIAL_COLORS.get(v, "#666")}">{_e(ver.get("verdict_vi", v).split(" — ")[0])}'
            f'</span> <span class="sub">{"Claude đọc bài" if ver.get("method") == "claude" else "chỉ đếm từ khoá"}'
            f'{" · tin cậy " + _e(ver.get("confidence")) if ver.get("method") == "claude" else ""}</span>')
    nums = ""
    if soc:
        comm = ", ".join(f"{c}" for c, _ in (soc.get("communities") or [])[:3])
        nums = (f'<div class="sub">{soc.get("volume_90", 0)} bài / 90 ngày · đà {soc.get("growth") or "–"}× · '
                f'{_e(", ".join(f"{k} {n}" for k, n in (soc.get("by_source") or {}).items()))}'
                f'{(" · " + _e(comm)) if comm else ""}</div>')
    quotes = "".join(
        f'<li>“{_e(e["quote"])}” <a class="sub" href="{_e(e["url"])}" target="_blank" rel="noopener">'
        f'{_e(e.get("source"))}{(" r/" + _e(e["community"])) if e.get("source") == "reddit" and e.get("community") else ""}'
        f' · {_e(e.get("date"))}</a></li>' for e in (ver.get("evidence") or [])[:3])
    phrases = ", ".join(f"“{_e(x)}”" for x in (ver.get("customer_phrases") or [])[:6])
    notes = " ".join(x for x in (ver.get("persona_notes"), ver.get("risks")) if x)
    detail = ""
    if quotes or phrases or notes or per.get("notes"):
        detail = (f'<details><summary>Bằng chứng</summary>{("<ul class=evid>" + quotes + "</ul>") if quotes else ""}'
                  f'{("<div class=sub><b>Lời khách hàng:</b> " + phrases + "</div>") if phrases else ""}'
                  f'{("<div class=sub>" + _e(notes) + "</div>") if notes else ""}'
                  f'{("<div class=sub>" + _e("; ".join(per.get("notes") or [])) + "</div>") if per.get("notes") else ""}'
                  f'</details>')
    return head + nums + detail


def render_angles(model: Model, token: str | None = None, sort: str | None = None) -> str:
    rows = []
    seen_products = set()
    for a in _sorted(model.angles, sort):
        i, m = a.info, a.metrics
        anchor_p = "" if i["product"] in seen_products else f' id="a-{slug(i["product"])}"'
        seen_products.add(i["product"])
        stores = ", ".join(_store_link(model, k) for k in i["stores"][:4])
        rows.append(f"""<tr{anchor_p}><td id="{a.anchor}"><div class="store">{_e(a.name)}</div>
<div class="sub">{_e(i['hook'])} · <a href="products.html#p-{slug(i['product'])}">{_e(i['generic'])}</a></div>{_label(a, token)}</td>
<td>{_e(i['persona'])}<div class="sub">{_e(i['pain'])} → {_e(i['promise'])}</div>{_persona_need(a)}</td>
<td>{_chips(a)}{_why(a)}</td>
<td>{_spark(i['history'])}<div class="sub">{_e(i['status_vi'])} · {pf.METRICS['angle_share'].show(m.get('angle_share'))} ad đang sống</div>
<div class="sub">{stores}</div></td>
<td style="max-width:340px">{_social(a)}</td></tr>""")
    body = (f"<style>{CSS}</style>{_intro(model)}"
            '<div class="muted">Cơ hội = angle/persona đang được nhiều store dồn ad (ad đang sống tăng), nhu cầu của '
            'chính persona đó đang tăng (Trends theo nỗi đau), và người thật đang nói về nỗi đau này (mạng xã hội).</div>'
            f"{_tools(model, sort, 'opportunities.html', bool(token))}"
            '<div class="wrap"><table><thead><tr><th>Angle</th><th>Persona · nỗi đau → lời hứa · nhu cầu persona</th>'
            '<th>Hợp chiến lược nào</th><th>Ad đang sống · store</th><th>Xác minh trên mạng xã hội</th></tr></thead>'
            f'<tbody>{"".join(rows) or "<tr><td colspan=5 class=muted>Chưa có phân tích angle.</td></tr>"}</tbody></table></div>')
    return page("Cơ hội angle", body, active="opportunities.html")


def write_static(model: Model, report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "products.html").write_text(render_products(model), encoding="utf-8")
    (report_dir / "stores.html").write_text(render_stores(model), encoding="utf-8")
    (report_dir / "opportunities.html").write_text(render_angles(model), encoding="utf-8")
    (report_dir / "evaluation.json").write_text(json.dumps({
        "as_of": model.as_of,
        **{k: [{"key": e.key, "name": e.name, "metrics": e.metrics, "evals": [x.to_dict() for x in e.evals]}
               for e in items]
           for k, items in (("products", model.products), ("stores", model.stores), ("angles", model.angles))},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def label_stats(model: Model) -> dict:
    """Hiệu chỉnh: chỉ số nào / hồ sơ nào phân biệt được thứ đã win với thứ đã fail."""
    ents = [e for e in model.products + model.stores + model.angles if e.info.get("label") in ("win", "fail")]
    counts = Counter(e.info["label"] for e in ents)
    metrics = []
    for key, m in pf.METRICS.items():
        if m.norm is None:
            continue
        win = [m.norm(e.metrics[key]) for e in ents if e.info["label"] == "win" and e.metrics.get(key) is not None]
        fail = [m.norm(e.metrics[key]) for e in ents if e.info["label"] == "fail" and e.metrics.get(key) is not None]
        if win and fail:
            mw, mf = sum(win) / len(win), sum(fail) / len(fail)
            metrics.append({"metric": key, "label": m.label, "win": round(mw, 2), "fail": round(mf, 2),
                            "gap": round(mw - mf, 2), "n": len(win) + len(fail)})
    metrics.sort(key=lambda x: -abs(x["gap"]))
    profs = []
    for p in model.profiles:
        sw = [e.score(p.key) for e in ents if e.info["label"] == "win"]
        sf = [e.score(p.key) for e in ents if e.info["label"] == "fail"]
        hit = sum(1 for e in ents if (e.score(p.key) >= p.fit) == (e.info["label"] == "win"))
        profs.append({"profile": p.label, "win": round(sum(sw) / len(sw), 1) if sw else None,
                      "fail": round(sum(sf) / len(sf), 1) if sf else None,
                      "accuracy": round(hit / len(ents), 2) if ents else None})
    return {"counts": dict(counts), "metrics": metrics, "profiles": profs}

