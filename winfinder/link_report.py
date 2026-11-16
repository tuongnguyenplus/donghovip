"""Trang 'Ứng viên': store + sản phẩm + nhu cầu + kết luận."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from winfinder.link import LinkedStore, to_dicts
from winfinder.angles_report import anchor
from winfinder.report import _e, _sparkline, page
from winfinder.trends_report import BADGE_COLORS

VERDICT_COLORS = {
    "strong": "#1a7f4b",
    "watch": "#6b4fd8",
    "seasonal_now": "#a86b00",
    "seasonal_off": "#8a6d3b",
    "ad_driven": "#2c6fb7",
    "saturated": "#6b6b66",
    "risky": "#c0392b",
    "no_keyword": "#8a8a85",
}

CSS = """
.lbl{display:inline-block;color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px}
.verdict{border-left:3px solid;padding-left:8px;font-size:13px}
.prod{font-weight:600} .kw{color:var(--muted);font-size:12px}
.what{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--accent);font-weight:700}
.need{margin-top:6px;font-size:12px}
.win{font-size:20px;font-weight:700}
.legend-v{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:10px;font-size:12px;color:var(--muted)}
.legend-v span::before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;background:var(--c)}
"""


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:+.0f}%"


TRAFFIC_COLORS = {"confirmed": "#1a7f4b", "organic": "#2c6fb7", "unconfirmed": "#a86b00", "declining": "#c0392b"}
TRAFFIC_SHORT = {"confirmed": "Tăng cùng ad", "unconfirmed": "Chưa tăng theo ad", "organic": "Tăng tự nhiên",
                 "flat": "Đi ngang", "declining": "Đang giảm", "too_small": "Chưa đủ lớn", "ambiguous": "Tên mơ hồ",
                 "no_data": "Không có dữ liệu"}


def _traffic_cell(x: LinkedStore) -> str:
    t = x.traffic
    if not t:
        return '<span class="muted">–</span>'
    v = t["verdict"]
    color = TRAFFIC_COLORS.get(v)
    badge = (f'<span class="lbl" style="background:{color}">{_e(TRAFFIC_SHORT[v])}</span>' if color
             else f'<span class="muted">{_e(TRAFFIC_SHORT.get(v, v))}</span>')
    spark = _sparkline([round(w) for w in t["brand_weeks"]]) if v not in ("too_small", "ambiguous", "no_data") else ""
    detail = f'brand “{_e(t["brand_term"])}”'
    if t.get("brand_ratio") is not None:
        detail += f' · {t["brand_ratio"]}× vs ad {t["ad_ratio"]}×'
    return f'{badge}{spark}<div class="muted" title="{_e(t["verdict_text"])}">{detail}</div>'


def _row(i: int, x: LinkedStore) -> str:
    name = x.domain or (x.page_names[0] if x.page_names else x.key)
    link = f'<a href="https://{_e(x.domain)}" target="_blank" rel="noopener">{_e(name)}</a>' if x.domain else _e(name)
    badge = '<span class="badge">SCALE</span>' if x.scaling else ""
    if x.trend and x.trend["label"] == "insufficient":
        trend_cell = '<span class="muted">Gần như không có lượt tìm kiếm trên Google</span>'
    elif x.trend:
        t = x.trend
        trend_cell = (
            f'<span class="lbl" style="background:{BADGE_COLORS.get(t["label"], "#666")}">{_e(t["label_vi"])}</span>'
            f'{_sparkline([round(v) for v in x.trend_series])}'
            f'<div class="muted">YoY {_pct(t["yoy"])} · xu hướng {_pct(t["trend_growth"])}/năm'
            f'{" · đang vào mùa" if t.get("in_season") else ""}</div>'
        )
    else:
        trend_cell = '<span class="muted">không có dữ liệu tìm kiếm</span>'
    if x.need_trend:
        n = x.need_trend
        trend_cell += (
            f'<div class="need"><span class="muted">Nhu cầu “{_e(x.problem_keywords[0])}”:</span> '
            f'<span class="lbl" style="background:{BADGE_COLORS.get(n["label"], "#666")}">{_e(n["label_vi"])}</span>'
            f' <span class="muted">YoY {_pct(n["yoy"])}</span></div>'
        )
    c = VERDICT_COLORS.get(x.verdict, "#666")
    src = {"claude": "Claude đọc trang SP", "config": "đặt tay", "page+ads": "trang SP + ad",
           "shopify+ads": "Shopify + ad", "ads": "chỉ từ ad"}.get(x.keyword_source, x.keyword_source)
    what = f'<div class="what">{_e(x.generic_product)}</div>' if x.generic_product and x.generic_product != x.product else ""
    persona = f'<div class="muted">tệp: {_e(x.persona)}</div>' if x.persona else ""
    page_link = (f' · <a href="{_e(x.page_url)}" target="_blank" rel="noopener">trang SP</a>' if x.page_url else "")
    low = ' · <b>độ tin cậy thấp</b>' if x.confidence == "low" else ""
    basis = (f'<div class="muted">dựa trên nhu cầu “{_e(x.problem_keywords[0])}” vì tên sản phẩm còn ít người tìm</div>'
             if x.verdict_basis == "need" and x.problem_keywords else "")
    return f"""<tr>
<td class="num">{i}</td>
<td><div class="store">{link} {badge}</div><div class="muted">{_e(" ".join(x.countries))}</div></td>
<td>{what}<div class="prod">{_e(x.product or "–")}</div>
<div class="kw">{_e(", ".join(k for k in x.keywords if k != x.product))}</div>{persona}
<div class="muted">nguồn: {_e(src)}{page_link}{low} · {x.competitors} store khác cùng bán</div>
{f'<a class="kw" href="angles.html#{anchor(x.product)}">xem angle →</a>' if x.product else ""}</td>
<td class="num win">{x.win_score:.0f}</td>
<td>{_sparkline(x.ad_history)}<div class="muted">điểm ad {x.ad_score:.0f}</div></td>
<td>{trend_cell}</td>
<td>{_traffic_cell(x)}</td>
<td class="muted">{_e(x.spread.get("stage", "–"))}</td>
<td><div class="verdict" style="border-color:{c}">{_e(x.verdict_text)}</div>{basis}</td>
</tr>"""


def render(linked: list[LinkedStore], as_of: date, provider: str) -> str:
    from winfinder.link import VERDICTS

    counts = {v: sum(1 for x in linked if x.verdict == v) for v in VERDICTS}
    legend = "".join(
        f'<span style="--c:{VERDICT_COLORS[v]}">{_e(VERDICTS[v].split(" — ")[0])}: {n}</span>'
        for v, n in counts.items() if n
    )
    body = f"""<style>{CSS}</style>
<div class="muted">Dữ liệu đến {as_of.isoformat()} · {len(linked)} store đã nối với Trends ({_e(provider)}) ·
điểm tổng = 55% điểm ad + 45% điểm trends, ×1.1 nếu traffic tăng cùng ad, ×0.9 nếu chưa</div>
<div class="legend-v">{legend}</div>
<div class="wrap"><table>
<thead><tr><th class="num">#</th><th>Store</th><th>Sản phẩm (keyword tìm kiếm)</th><th class="num">Điểm tổng</th>
<th>Creative mới / tuần</th><th>Nhu cầu tìm kiếm (2 năm, khử mùa vụ)</th><th>Traffic (lượt tìm brand, 12 tuần)</th>
<th>Lan rộng</th><th>Kết luận</th></tr></thead>
<tbody>{"".join(_row(i, x) for i, x in enumerate(linked, 1))}</tbody></table></div>"""
    return page("Ứng viên", body, active="winners.html")


def write(linked: list[LinkedStore], report_dir: Path, as_of: date, provider: str) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "winners.json").write_text(
        json.dumps({"as_of": as_of.isoformat(), "provider": provider, "stores": to_dicts(linked)},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (report_dir / "winners.html").write_text(render(linked, as_of, provider), encoding="utf-8")
