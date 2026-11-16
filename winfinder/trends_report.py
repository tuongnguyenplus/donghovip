"""Báo cáo Trends: JSON + HTML (dùng chung khung trang với báo cáo store)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from winfinder.report import _e, page
from winfinder.trends_pipeline import TopicResult

BADGE_COLORS = {
    "rising": "#1a7f4b",
    "breakout": "#6b4fd8",
    "seasonal": "#a86b00",
    "stable": "#6b6b66",
    "declining": "#c0392b",
    "spike": "#d35400",
    "insufficient": "#8a8a85",
}

EXTRA_CSS = """
.cards{display:grid;gap:16px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;overflow-x:auto}
.card h2{font-size:17px;margin:0 8px 0 0;display:inline}
.lbl{display:inline-block;color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;vertical-align:2px}
.kpis{display:flex;flex-wrap:wrap;gap:8px 24px;margin:12px 0}
.kpi b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
.chart{color:var(--accent);width:100%;height:auto;display:block}
.chart .raw{stroke:var(--muted);opacity:.55}
.chart text{fill:var(--muted);font-size:10px}
.legend{font-size:12px;color:var(--muted)} .legend i{display:inline-block;width:14px;height:2px;vertical-align:3px;margin:0 4px 0 10px}
.card table{min-width:640px;margin-top:8px}
.stage{margin:4px 0 0;font-size:13px}
"""


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:+.0f}%"


def _badge(stats: dict) -> str:
    return f'<span class="lbl" style="background:{BADGE_COLORS.get(stats["label"], "#666")}">{_e(stats["label_vi"])}</span>'


def _week_to_month(week: int | None) -> str:
    if not week:
        return "–"
    return f"tháng {(date(2025, 1, 1) + timedelta(weeks=week - 1)).month}"


def _chart(weeks: list[str], raw: list[float], trend: list[float], w: int = 760, h: int = 150) -> str:
    if not raw:
        return ""
    top = max(max(raw), max(trend or [0])) or 1
    n = len(raw)
    pad_b = 16

    def pts(vals: list[float]) -> str:
        step = w / max(n - 1, 1)
        return " ".join(f"{i * step:.1f},{(h - pad_b) - (v / top) * (h - pad_b - 6):.1f}" for i, v in enumerate(vals))

    ticks = []
    seen = set()
    for i, wk in enumerate(weeks):
        y = wk[:4]
        if y not in seen and wk[5:7] == "01":
            seen.add(y)
            x = i * w / max(n - 1, 1)
            ticks.append(f'<line x1="{x:.0f}" x2="{x:.0f}" y1="0" y2="{h - pad_b}" stroke="currentColor" opacity=".12"/>'
                         f'<text x="{x + 3:.0f}" y="{h - 3}">{y}</text>')
    return (
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Chỉ số tìm kiếm theo tuần">'
        + "".join(ticks)
        + f'<polyline class="raw" points="{pts(raw)}" fill="none" stroke-width="1"/>'
        + (f'<polyline points="{pts(trend)}" fill="none" stroke="currentColor" stroke-width="2" '
           f'stroke-linejoin="round"/>' if trend else "")
        + "</svg>"
    )


def _mini(values: list[float], w: int = 110, h: int = 26) -> str:
    vals = values[-104:]
    if not vals:
        return ""
    top = max(vals) or 1
    step = w / max(len(vals) - 1, 1)
    p = " ".join(f"{i * step:.1f},{h - 2 - (v / top) * (h - 4):.1f}" for i, v in enumerate(vals))
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">'
            f'<polyline points="{p}" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>')


def _topic_card(t: TopicResult) -> str:
    home = t.home
    if not home:
        return f'<section class="card"><h2>{_e(t.name)}</h2><p class="muted">Không lấy được dữ liệu.</p></section>'
    s = home.stats
    vol = (f'<div class="kpi"><span class="muted">Search/tháng ước tính ({_e(home.geo)})</span>'
           f'<b>{home.est_monthly_volume:,}</b></div>') if home.est_monthly_volume else ""
    geo_rows = "".join(
        f"<tr><td>{_e(g.geo)}</td><td>{_badge(g.stats)}</td><td class='num'>{g.score:.0f}</td>"
        f"<td class='num'>{_pct(g.stats['yoy'])}</td><td class='num'>{_pct(g.stats['trend_growth'])}</td>"
        f"<td>{_mini(g.deseasonalized)}</td></tr>"
        for g in t.geos
    )
    kw_rows = "".join(
        f"<tr><td>{_e(k.keyword)}</td><td class='num'>{k.share * 100:.0f}%</td><td>{_badge(k.stats)}</td>"
        f"<td class='num'>{k.score:.0f}</td><td class='num'>{_pct(k.stats['yoy'])}</td>"
        f"<td class='num'>{_pct(k.stats['trend_growth'])}</td>"
        f"<td class='num'>{f'{k.est_monthly_volume:,}' if k.est_monthly_volume else '–'}</td></tr>"
        for k in home.keywords
    )
    if s["seasonality"] is None:
        season = "–"
    elif s["label"] == "seasonal" or s["in_season"]:
        season = (f"dao động {s['seasonal_amplitude'] * 100:.0f}% · đỉnh {_week_to_month(s['peak_week'])}"
                  + (" · đang vào mùa" if s["in_season"] else ""))
    else:
        season = "không rõ rệt"
    return f"""<section class="card">
<h2>{_e(t.name)}</h2>{_badge(s)} <span class="muted">thị trường gốc {_e(home.geo)} · điểm {home.score:.0f}</span>
<p class="stage">{_e(t.spread.get("stage", ""))}</p>
<div class="kpis">
<div class="kpi"><span class="muted">13 tuần vs cùng kỳ</span><b>{_pct(s['yoy'])}</b></div>
<div class="kpi"><span class="muted">Xu hướng/năm (khử mùa vụ)</span><b>{_pct(s['trend_growth'])}</b></div>
<div class="kpi"><span class="muted">Đà 4 tuần</span><b>{_pct(s['momentum'])}</b></div>
<div class="kpi"><span class="muted">Mùa vụ</span><b style="font-size:14px">{season}</b></div>
{vol}
</div>
{_chart(home.weeks, home.values, home.deseasonalized)}
<div class="legend"><i style="background:var(--muted)"></i>chỉ số tìm kiếm (tổng bộ keyword)<i style="background:var(--accent)"></i>đã khử mùa vụ</div>
<table><thead><tr><th>Thị trường</th><th>Phân loại</th><th class="num">Điểm</th><th class="num">YoY</th>
<th class="num">Xu hướng/năm</th><th>2 năm (khử mùa vụ)</th></tr></thead><tbody>{geo_rows}</tbody></table>
<details><summary>Bộ keyword ({len(home.keywords)}) — {_e(home.geo)}</summary>
<table><thead><tr><th>Keyword</th><th class="num">Tỉ trọng</th><th>Phân loại</th><th class="num">Điểm</th>
<th class="num">YoY</th><th class="num">Xu hướng/năm</th><th class="num">Search/tháng</th></tr></thead>
<tbody>{kw_rows}</tbody></table></details>
</section>"""


def render_trends_html(topics: list[TopicResult], as_of: date, provider: str) -> str:
    body = (
        f"<style>{EXTRA_CSS}</style>"
        f'<div class="muted">Dữ liệu đến {as_of.isoformat()} · {len(topics)} chủ đề · nguồn: Google Trends ({_e(provider)})'
        " · chỉ số tương đối 0–100, không phải lượt tìm kiếm tuyệt đối</div>"
        f'<div class="cards">{"".join(_topic_card(t) for t in topics)}</div>'
    )
    return page("Trends", body, active="trends.html")


def write_trends(topics: list[TopicResult], report_dir: Path, as_of: date, provider: str) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "trends.json").write_text(
        json.dumps({"as_of": as_of.isoformat(), "provider": provider, "topics": [asdict(t) for t in topics]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (report_dir / "trends.html").write_text(render_trends_html(topics, as_of, provider), encoding="utf-8")
