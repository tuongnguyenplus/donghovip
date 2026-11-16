"""Xuất kết quả: JSON (cho máy đọc) và HTML tĩnh (cho người xem)."""

from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path

from winfinder.analysis.scoring import StoreResult
from winfinder.sources.landing import LANDING_VI


def write_json(results: list[StoreResult], path: Path, as_of: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"as_of": as_of.isoformat(), "stores": [r.to_dict() for r in results]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _sparkline(values: list[int], w: int = 120, h: int = 28) -> str:
    if not values:
        return ""
    top = max(values) or 1
    step = w / max(len(values) - 1, 1)
    pts = " ".join(f"{i * step:.1f},{h - 2 - (v / top) * (h - 4):.1f}" for i, v in enumerate(values))
    return (
        f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
        f'aria-label="Creative mới mỗi tuần: {", ".join(map(str, values))}">'
        f'<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


def _e(x: object) -> str:
    return html.escape(str(x)) if x is not None else ""

BASE_CSS = """:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e2dc;--card:#fff;--accent:#0f7b5f;--chip:#efefea}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--fg:#ececea;--muted:#9a9a94;--line:#2c2c2a;--card:#1c1c1b;--accent:#4cc9a0;--chip:#262624}}
:root[data-theme="dark"]{--bg:#141413;--fg:#ececea;--muted:#9a9a94;--line:#2c2c2a;--card:#1c1c1b;--accent:#4cc9a0;--chip:#262624}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px} .muted{color:var(--muted);font-size:12px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px;margin-top:16px}
table{border-collapse:collapse;width:100%;min-width:980px}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
th{font-size:12px;color:var(--muted);font-weight:600}
.num{text-align:right;font-variant-numeric:tabular-nums} .score{font-size:18px;font-weight:700}
a{color:var(--accent)} .store{font-weight:600}
.badge{background:var(--accent);color:var(--card);font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;margin-left:4px}
.spark{color:var(--accent);display:block}
.chip{display:inline-block;background:var(--chip);border-radius:4px;padding:1px 6px;margin:2px 4px 2px 0;font-size:11px}
details ul{padding-left:18px;margin:6px 0} details li{margin-bottom:6px} .body{color:var(--muted);font-size:12px}
summary{cursor:pointer;color:var(--muted);font-size:12px;margin-top:4px}
.nav{display:flex;gap:16px;margin:0 0 16px;font-size:13px} .nav a{text-decoration:none} .nav a.on{font-weight:700;text-decoration:underline}
"""

NAV = [("winners.html", "Ứng viên"), ("angles.html", "Angle"), ("index.html", "Store đang scale"), ("trends.html", "Trends")]


def page(heading: str, body: str, active: str) -> str:
    nav = "".join(
        f'<a href="{href}" class="{"on" if href == active else ""}">{_e(label)}</a>' for href, label in NAV
    )
    return (
        '<!doctype html><html lang="vi"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>WinFinder</title>'
        f"<style>{BASE_CSS}</style></head><body><main>"
        f'<nav class="nav">{nav}</nav><h1>{_e(heading)}</h1>{body}</main></body></html>'
    )


ARCH_SHORT = {"scaling": "Đang scale", "early": "Tăng sớm", "evergreen": "Evergreen", "steady": "Ổn định",
              "testing": "Đang test", "treadmill": "Máy xay creative", "declining": "Đang giảm", "stopped": "Đã dừng"}
ARCH_COLORS = {"scaling": "#1a7f4b", "early": "#6b4fd8", "evergreen": "#2c6fb7", "steady": "#6b6b66",
               "testing": "#8a8a85", "treadmill": "#d35400", "declining": "#c0392b", "stopped": "#8a8a85"}


def render_html(results: list[StoreResult], as_of: date, limit: int = 200) -> str:
    rows = []
    for i, s in enumerate(results[:limit], 1):
        name = s.domain or (s.page_names[0] if s.page_names else s.key)
        link = f'<a href="https://{_e(s.domain)}" target="_blank" rel="noopener">{_e(name)}</a>' if s.domain else _e(name)
        samples = "".join(
            f'<li><a href="{_e(a["snapshot_url"])}" target="_blank" rel="noopener">{_e(a["title"] or "(ad)")}</a>'
            f' <span class="muted">· {_e(a["start_date"])} · reach {_e(a["eu_total_reach"] or "–")}</span>'
            f'<div class="body">{_e(a["body"])}</div></li>'
            for a in s.sample_ads
        )
        comps = " ".join(f'<span class="chip">{_e(k)} {v:.2f}</span>' for k, v in s.components.items())
        d = s.durability
        landing = " · ".join(f"{LANDING_VI.get(k, k)} {n}" for k, n in s.landing_mix.items())
        if s.via_domains:
            landing += " (qua " + ", ".join(s.via_domains[:2]) + ")"
        color = ARCH_COLORS.get(s.archetype, "#6b6b66")
        badge = f'<span class="lbl" style="background:{color}">{_e(ARCH_SHORT.get(s.archetype, s.archetype))}</span>'
        surv = "–" if d["survival_30"] is None else f'{d["survival_30"] * 100:.0f}%'
        rows.append(
            f"""<tr>
<td class="num">{i}</td>
<td><div class="store">{link}</div><div class="muted">{s.pages} page: {_e(", ".join(s.page_names[:3]))}</div>
<div class="muted">{_e(landing)}</div>
<details><summary>Chi tiết</summary><div class="chips">{comps}</div>
<div class="muted">{_e("; ".join(d["notes"]))}</div><ul>{samples}</ul></details></td>
<td>{badge}<div class="muted" style="max-width:220px">{_e(s.archetype_vi.split(" — ")[-1])}</div></td>
<td class="num score">{s.score:.0f}</td>
<td class="trend">{_sparkline(s.history)}<div class="muted">{d["active_now"]} đang chạy · đà {d["active_growth"]}×</div></td>
<td class="num">{surv}<div class="muted">{d["aged_ads"]} ad đủ tuổi</div></td>
<td class="num">{d["alive_30"]} / {d["alive_60"]}<div class="muted">cũ nhất {_e(d["oldest_active_days"] or "–")} ngày</div></td>
<td class="num">{d["dup_share"] * 100:.0f}%<div class="muted">{d["duplicated_creatives"]}/{d["creatives"]} creative</div></td>
<td class="num">{d["turnover"]}<div class="muted">{d["launches_4w"]} ad mới/4 tuần</div></td>
<td class="num">{"≥" if s.age_source == "first_ad" else ""}{_e(s.age_days)}d<div class="muted">{_e(s.age_source)}</div></td>
<td>{_e(" ".join(s.countries))}<div class="muted">{_e(", ".join(s.searches))}</div></td>
</tr>"""
        )
    counts = {a: sum(1 for s in results if s.archetype == a) for a in ARCH_SHORT}
    legend = " · ".join(f"{ARCH_SHORT[a]}: {n}" for a, n in counts.items() if n)
    body = f"""<style>.lbl{{display:inline-block;color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap}}</style>
<div class="muted">Dữ liệu đến {as_of.isoformat()} · {len(results)} store · {_e(legend)} · nguồn: Meta Ad Library (EU)</div>
<div class="muted">Tăng trưởng thật = số ad ĐANG SỐNG tăng. Sống sót = % ad đủ 30 ngày tuổi còn sống ≥ 30 ngày.
Thay creative = ad mới 4 tuần / ad đang sống (cao = phải đẻ creative liên tục, khó scale).</div>
<div class="wrap"><table>
<thead><tr><th class="num">#</th><th>Store</th><th>Dạng</th><th class="num">Điểm</th><th>Ad đang sống / tuần (12 tuần)</th>
<th class="num">Sống sót 30 ngày</th><th class="num">Ad ≥30 / ≥60 ngày</th><th class="num">Nhân bản</th>
<th class="num">Thay creative</th><th class="num">Tuổi</th><th>Thị trường · ngách</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>"""
    return page("Store đang scale", body, active="index.html")


def write_html(results: list[StoreResult], path: Path, as_of: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(results, as_of), encoding="utf-8")
