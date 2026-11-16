"""Trang 'Ứng viên': store + sản phẩm + nhu cầu + kết luận."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from winfinder.link import LinkedStore, to_dicts
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
.win{font-size:20px;font-weight:700}
.legend-v{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:10px;font-size:12px;color:var(--muted)}
.legend-v span::before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;background:var(--c)}
"""


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:+.0f}%"


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
    c = VERDICT_COLORS.get(x.verdict, "#666")
    return f"""<tr>
<td class="num">{i}</td>
<td><div class="store">{link} {badge}</div><div class="muted">{_e(" ".join(x.countries))}</div></td>
<td><div class="prod">{_e(x.product or "–")}</div>
<div class="kw">{_e(", ".join(k for k in x.keywords if k != x.product))}</div>
<div class="muted">nguồn: {_e(x.keyword_source)} · {x.competitors} store khác cùng bán</div></td>
<td class="num win">{x.win_score:.0f}</td>
<td>{_sparkline(x.ad_history)}<div class="muted">điểm ad {x.ad_score:.0f}</div></td>
<td>{trend_cell}</td>
<td class="muted">{_e(x.spread.get("stage", "–"))}</td>
<td><div class="verdict" style="border-color:{c}">{_e(x.verdict_text)}</div></td>
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
điểm tổng = 55% điểm ad + 45% điểm trends</div>
<div class="legend-v">{legend}</div>
<div class="wrap"><table>
<thead><tr><th class="num">#</th><th>Store</th><th>Sản phẩm (keyword)</th><th class="num">Điểm tổng</th>
<th>Creative mới / tuần</th><th>Nhu cầu tìm kiếm (2 năm, khử mùa vụ)</th><th>Lan rộng</th><th>Kết luận</th></tr></thead>
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
