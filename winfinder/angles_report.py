"""Trang 'Angle': mỗi sản phẩm → các angle đang chạy, angle nào đang lên, angle chưa ai khai thác."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from winfinder.angles_pipeline import ProductAngles
from winfinder.report import _e, _sparkline, page

STATUS_COLORS = {
    "rising": "#1a7f4b",
    "new": "#6b4fd8",
    "steady": "#6b6b66",
    "churn": "#d35400",
    "observing": "#6b6b66",
    "declining": "#c0392b",
    "stopped": "#8a8a85",
}

CSS = """
.cards{display:grid;gap:16px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;overflow-x:auto}
.card h2{font-size:17px;margin:0;display:inline}
.lbl{display:inline-block;color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap}
.card table{min-width:860px;margin-top:10px}
.aname{font-weight:600} .hook{font-size:11px;color:var(--muted)}
.share{font-variant-numeric:tabular-nums}
.bar{height:6px;background:var(--chip);border-radius:3px;margin-top:4px;width:90px}
.bar i{display:block;height:6px;border-radius:3px;background:var(--accent)}
.sample{font-size:12px;color:var(--muted);max-width:380px}
.untapped{margin-top:12px;padding:10px 12px;border:1px dashed var(--line);border-radius:8px}
.untapped h3{font-size:13px;margin:0 0 6px}
.untapped li{margin-bottom:4px;font-size:13px}
"""


def anchor(product: str) -> str:
    return "p-" + re.sub(r"[^a-z0-9]+", "-", product.lower()).strip("-")


def _card(p: ProductAngles) -> str:
    rows = "".join(
        f"""<tr>
<td><div class="aname">{_e(a.angle.name)}</div><div class="hook">{_e(a.angle.hook_type)}</div></td>
<td>{_e(a.angle.persona)}<div class="muted">{_e(a.angle.pain_point)}</div></td>
<td>{_e(a.angle.promise)}</td>
<td><span class="lbl" style="background:{STATUS_COLORS[a.status]}">{_e(a.status_vi)}</span></td>
<td>{_sparkline(a.history)}<div class="muted">đà {a.growth_ratio}× · {a.alive_30} ad ≥30 ngày</div></td>
<td class="share">{a.recent_share * 100:.0f}%<div class="bar"><i style="width:{a.recent_share * 100:.0f}%"></i></div></td>
<td class="num">{a.ads}<div class="muted">{len(a.stores)} store</div></td>
<td class="sample">{_e(a.samples[0] if a.samples else "")}</td>
</tr>"""
        for a in p.angles
    )
    untapped = "".join(
        f"<li><b>{_e(u.get('name'))}</b> — {_e(u.get('persona'))}. <span class='muted'>{_e(u.get('why'))}</span></li>"
        for u in p.untapped
    )
    rising = [a.angle.name for a in p.angles if a.status == "rising"]
    testing = [a.angle.name for a in p.angles if a.status == "new"]
    parts = []
    if rising:
        parts.append("Đang lên: " + ", ".join(rising))
    if testing:
        parts.append("Đang được test: " + ", ".join(testing))
    summary = " · ".join(parts) or "Chưa có angle nào tăng rõ"
    return f"""<section class="card" id="{anchor(p.product)}">
<h2>{_e(p.generic or p.product)}</h2> <span class="muted">keyword “{_e(p.product)}” · {len(p.stores)} store ·
{p.units} nội dung ad, đã phân loại {p.assigned}</span>
<p style="margin:6px 0 0">{_e(summary)}</p>
<table><thead><tr><th>Angle</th><th>Persona · nỗi đau</th><th>Lời hứa</th><th>Trạng thái</th>
<th>Ad đang sống / tuần (12 tuần)</th><th>Tỉ trọng ad đang sống</th><th class="num">Ad</th><th>Ví dụ</th></tr></thead>
<tbody>{rows}</tbody></table>
{f'<div class="untapped"><h3>Angle chưa ai khai thác</h3><ul>{untapped}</ul></div>' if untapped else ""}
</section>"""


def render(products: list[ProductAngles], as_of: date, note: str = "") -> str:
    body = (
        f"<style>{CSS}</style>"
        f'<div class="muted">Dữ liệu đến {as_of.isoformat()} · {len(products)} sản phẩm · '
        "angle do Claude phân loại từ nội dung ad của các store đang bán; “Đang lên” = số ad ĐANG SỐNG của angle tăng; "
        f"“Creative chết nhanh” = ra nhiều nhưng không ad nào sống lâu{(' · ' + _e(note)) if note else ''}</div>"
        f'<div class="cards">{"".join(_card(p) for p in products) or "<p>Chưa có sản phẩm nào đủ dữ liệu.</p>"}</div>'
    )
    return page("Angle", body, active="angles.html")


def write(products: list[ProductAngles], report_dir: Path, as_of: date, note: str = "") -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "angles.json").write_text(
        json.dumps({"as_of": as_of.isoformat(), "products": [p.to_dict() for p in products]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (report_dir / "angles.html").write_text(render(products, as_of, note), encoding="utf-8")
    from winfinder import gaps_report  # trang Khoảng trống + Lời khách hàng dùng cùng dữ liệu
    gaps_report.write([p.to_dict() for p in products], report_dir, as_of)
