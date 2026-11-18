"""Trang 'Khoảng trống' (cầu organic vs cung ad) và 'Lời khách hàng' (customer language bank)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from winfinder.analysis.gaps import VERDICTS
from winfinder.report import _e, page
from winfinder.views import slug

GAP_COLORS = {"gap": "#1a7f4b", "fumbled": "#d35400", "proven": "#2c6fb7", "balanced": "#6b6b66",
              "oversupplied": "#c0392b", "minor": "#8a8a85", "generic": "#8a8a85"}
GAP_SHORT = {"gap": "Khoảng trống", "fumbled": "Tranh nhau sai cách", "proven": "Đã kiểm chứng",
             "balanced": "Cân bằng", "oversupplied": "Bão hoà", "minor": "Nhỏ", "generic": "Ad chung chung"}
KIND_VI = {"pain": "Nỗi đau", "desire": "Mong muốn", "objection": "Phản đối / lo ngại", "trigger": "Thời điểm kích hoạt",
           "result": "Kết quả mong đợi"}

CSS = """
.cards{display:grid;gap:16px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;overflow-x:auto}
.card h2{font-size:17px;margin:0;display:inline}
.lbl{display:inline-block;color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap}
.sum{margin:8px 0 0;font-size:13px} .sum b{display:inline-block;min-width:150px}
.card table{min-width:900px;margin-top:10px}
.bars{min-width:150px} .bar{display:flex;align-items:center;gap:6px;font-size:12px;margin:2px 0}
.bar span{display:inline-block;width:36px;color:var(--muted)} .bar i{display:block;height:8px;border-radius:4px}
.bar em{font-style:normal;font-variant-numeric:tabular-nums}
.ph{font-size:12px;color:var(--muted)} .ph q{color:var(--fg)}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.filters input,.filters select{padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);
 color:var(--fg);font:inherit;font-size:13px}
.filters button{padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);
 cursor:pointer;font:inherit;font-size:13px}
"""


def _pct(x) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"


def _bars(g: dict) -> str:
    d, s = g["demand_share"], g["supply_share"]
    return (f'<div class="bars"><div class="bar"><span>Cầu</span><i style="width:{max(2, d * 140):.0f}px;'
            f'background:#2c6fb7"></i><em>{_pct(d)}</em></div>'
            f'<div class="bar"><span>Cung</span><i style="width:{max(2, s * 140):.0f}px;background:#a86b00"></i>'
            f'<em>{_pct(s)}</em></div></div>')


def _card(p: dict) -> str:
    gaps = p.get("gaps") or []
    corpus = p.get("corpus") or {}
    if not gaps:
        why = ("chưa có dữ liệu organic — cần nguồn TikTok/Reddit/YouTube (APIFY_TOKEN, REDDIT_*, YOUTUBE_API_KEY)"
               if not corpus.get("posts") else "chưa phân loại organic (cần ANTHROPIC_API_KEY)")
        return (f'<section class="card"><h2>{_e(p.get("generic") or p["product"])}</h2>'
                f'<p class="muted">{_e(why)}</p></section>')
    by = {}
    for g in gaps:
        by.setdefault(g["verdict"], []).append(g["name"])
    def words(t: str) -> set[str]:
        return set((t or "").lower().split())

    # bỏ đề xuất trùng với angle đã có trên bản đồ (≥ 50% từ trùng)
    untapped = [u.get("name") for u in p.get("untapped") or []
                if not any(len(words(u.get("name")) & words(g["name"])) >= 0.5 * max(1, len(words(u.get("name"))))
                           for g in gaps)]
    summary = (
        f'<div class="sum"><b>🟢 Khoảng trống</b>{_e(", ".join(by.get("gap", [])) or "–")}</div>'
        f'<div class="sum"><b>✓ Đối thủ làm đúng</b>{_e(", ".join(by.get("proven", [])) or "–")}</div>'
        f'<div class="sum"><b>✗ Họ làm chưa tốt</b>{_e(", ".join(by.get("fumbled", [])) or "–")}'
        f'{" (có cầu, ad chết nhanh)" if by.get("fumbled") else ""}</div>'
        f'<div class="sum"><b>○ Chưa ai đụng tới</b>{_e(", ".join(untapped) or "–")} '
        f'<span class="muted">(Claude đề xuất, chưa có trong ad lẫn organic)</span></div>'
        f'<div class="sum"><b>⚠ Bão hoà</b>{_e(", ".join(by.get("oversupplied", [])) or "–")}</div>')
    rows = "".join(f"""<tr>
<td><div class="store">{_e(g['name'])}</div><div class="muted">{_e(g['hook'])}</div></td>
<td>{_e(g['persona'])}<div class="muted">{_e(g['pain'])} → {_e(g['promise'])}</div></td>
<td>{_bars(g)}</td>
<td class="muted">{g['demand_posts']} bài{(' · đà ' + str(g['demand_growth']) + '×') if g['demand_growth'] else ''}
<br>{_e(', '.join(f'{k} {v}' for k, v in g['demand_sources'].items()))}</td>
<td class="muted">{g['supply_ads']} ad · {g['supply_stores']} store{('<br>' + str(g['supply_alive_30']) + ' ad ≥30 ngày') if g['supply_ads'] else ''}</td>
<td><span class="lbl" style="background:{GAP_COLORS[g['verdict']]}">{_e(GAP_SHORT[g['verdict']])}</span>
<div class="muted">cơ hội {g['opportunity']:.0f}</div></td>
<td class="ph">{''.join(f'<div><q>{_e(x["phrase"])}</q></div>' for x in g['phrases'][:2])}</td></tr>"""
                   for g in gaps)
    src = ", ".join(f"{k} {v}" for k, v in (corpus.get("by_source") or {}).items())
    return f"""<section class="card" id="g-{slug(p['product'])}">
<h2>{_e(p.get('generic') or p['product'])}</h2> <span class="muted">{len(p['stores'])} store · {p['units']} nội dung ad ·
{corpus.get('posts', 0)} bài organic ({_e(src)}) · tìm theo: {_e(', '.join(corpus.get('queries') or []))} ·
<a href="products.html#p-{slug(p['product'])}">sản phẩm</a> · <a href="opportunities.html#a-{slug(p['product'])}">angle</a></span>
{summary}
<table><thead><tr><th>Angle</th><th>Persona · nỗi đau → lời hứa</th><th>Cầu vs cung</th><th>Cầu (organic)</th>
<th>Cung (ad)</th><th>Kết luận</th><th>Lời khách</th></tr></thead><tbody>{rows}</tbody></table>
</section>"""


def render_gaps(products: list[dict], as_of: str) -> str:
    legend = " · ".join(f"<b>{_e(GAP_SHORT[k])}</b>: {_e(v.split(' — ')[1])}" for k, v in VERDICTS.items())
    body = (f"<style>{CSS}</style>"
            f'<div class="muted">Dữ liệu đến {_e(as_of)}. <b>Cầu</b> = tỉ trọng thảo luận organic của angle '
            '(có trọng số tương tác) — tỉ trọng của người lên mạng nói, không phải tỉ trọng người mua; dùng để so '
            '<i>tương đối</i>. <b>Cung</b> = tỉ trọng ad ĐANG SỐNG của đối thủ. Ad và organic được Claude phân loại '
            'CÙNG MỘT LƯỢT nên dùng chung bộ angle.</div>'
            f'<div class="muted" style="margin-top:6px">{legend}</div>'
            f'<div class="cards">{"".join(_card(p) for p in products) or "<p>Chưa có dữ liệu.</p>"}</div>')
    return page("Khoảng trống", body, active="gaps.html")


def language_rows(products: list[dict]) -> list[dict]:
    rows = []
    for p in products:
        names = {g["angle_id"]: g for g in p.get("gaps") or []}
        for x in p.get("phrases") or []:
            g = names.get(x["angle"]) or {}
            rows.append({"product": p.get("generic") or p["product"], "angle": g.get("name", x["angle"]),
                         "persona": g.get("persona", ""), "kind": x.get("kind", "pain"), "phrase": x["phrase"],
                         "count": x.get("count"), "source": x.get("source", ""), "url": x.get("url", ""),
                         "date": x.get("date", ""), "community": x.get("community", "")})
    rows.sort(key=lambda r: (r["product"], r["angle"], -(r["count"] or 0)))
    return rows


def render_language(products: list[dict], as_of: str) -> str:
    rows = language_rows(products)
    prods = sorted({r["product"] for r in rows})
    trs = "".join(
        f'<tr data-p="{_e(r["product"])}" data-k="{_e(r["kind"])}"><td><q>{_e(r["phrase"])}</q></td>'
        f'<td>{_e(KIND_VI.get(r["kind"], r["kind"]))}</td><td class="num">{_e(r["count"] or "–")}</td>'
        f'<td>{_e(r["angle"])}<div class="muted">{_e(r["persona"])}</div></td><td>{_e(r["product"])}</td>'
        f'<td class="muted"><a href="{_e(r["url"])}" target="_blank" rel="noopener">{_e(r["source"])}'
        f'{(" r/" + _e(r["community"])) if r["source"] == "reddit" and r["community"] else ""}</a> · {_e(r["date"])}</td></tr>'
        for r in rows)
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    body = f"""<style>{CSS}</style>
<div class="muted">Dữ liệu đến {_e(as_of)} · {len(rows)} cụm từ khách hàng dùng, chép NGUYÊN VĂN từ bài/bình luận thật
(cụm nào không có nguyên văn trong bài gốc đã bị loại). Tần suất = số bài trong kho organic có chứa cụm từ.
Dùng làm hook, tiêu đề, copy.</div>
<div class="filters">
 <input id="q" type="search" placeholder="Tìm cụm từ, angle, persona…" aria-label="Tìm">
 <select id="fp" aria-label="Sản phẩm"><option value="">Mọi sản phẩm</option>{"".join(f'<option>{_e(x)}</option>' for x in prods)}</select>
 <select id="fk" aria-label="Loại"><option value="">Mọi loại</option>{"".join(f'<option value="{k}">{_e(v)}</option>' for k, v in KIND_VI.items())}</select>
 <button id="csv" type="button">Tải CSV</button> <span class="muted" id="n"></span>
</div>
<div class="wrap"><table><thead><tr><th>Cụm từ</th><th>Loại</th><th class="num">Tần suất</th><th>Angle · persona</th>
<th>Sản phẩm</th><th>Nguồn</th></tr></thead><tbody id="tb">{trs or '<tr><td colspan="6" class="muted">Chưa có dữ liệu — cần kho organic + Claude.</td></tr>'}</tbody></table></div>
<script>
const DATA = {data};
const q = document.getElementById('q'), fp = document.getElementById('fp'), fk = document.getElementById('fk');
function apply() {{
  const t = q.value.toLowerCase(); let n = 0;
  document.querySelectorAll('#tb tr[data-p]').forEach(tr => {{
    const ok = (!fp.value || tr.dataset.p === fp.value) && (!fk.value || tr.dataset.k === fk.value)
      && (!t || tr.textContent.toLowerCase().includes(t));
    tr.hidden = !ok; if (ok) n++;
  }});
  document.getElementById('n').textContent = n + ' cụm từ';
}}
[q, fp, fk].forEach(el => el.addEventListener('input', apply));
document.getElementById('csv').addEventListener('click', () => {{
  const cols = ['phrase', 'kind', 'count', 'angle', 'persona', 'product', 'source', 'date', 'url'];
  const esc = v => '"' + String(v ?? '').replace(/"/g, '""') + '"';
  const vis = DATA.filter(r => (!fp.value || r.product === fp.value) && (!fk.value || r.kind === fk.value)
    && (!q.value || JSON.stringify(r).toLowerCase().includes(q.value.toLowerCase())));
  const csv = '\\ufeff' + [cols.join(','), ...vis.map(r => cols.map(c => esc(r[c])).join(','))].join('\\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {{type: 'text/csv;charset=utf-8'}}));
  a.download = 'customer-language.csv'; a.click();
}});
apply();
</script>"""
    return page("Lời khách hàng", body, active="language.html")


def write(products: list[dict], report_dir: Path, as_of: date) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "gaps.html").write_text(render_gaps(products, as_of.isoformat()), encoding="utf-8")
    (report_dir / "language.html").write_text(render_language(products, as_of.isoformat()), encoding="utf-8")
