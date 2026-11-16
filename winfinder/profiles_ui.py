"""Trình sửa hồ sơ chiến lược + trang hiệu chỉnh (dùng trong `winfinder serve`)."""

from __future__ import annotations

import html
from collections import Counter

from winfinder import profiles as pf
from winfinder.report import page

MAX_GATES = 6

CSS = """
.pcard{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-top:16px}
.pcard h2{font-size:16px;margin:0 0 8px}
.pcard input[type=text],.pcard input[type=number],.pcard select,.pcard textarea{width:100%;padding:6px 8px;
 border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit;font-size:13px}
.pcard textarea{min-height:240px;font-family:ui-monospace,monospace;font-size:12px}
.grid2{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.pcard label{display:block;font-size:12px;color:var(--muted);margin:6px 0 3px}
.pcard table{min-width:520px} .pcard td,.pcard th{padding:4px 6px;font-size:13px}
.pcard td input[type=number]{width:80px}
.grp td{background:var(--chip);font-weight:700;font-size:12px}
button,.btn{display:inline-block;border:0;border-radius:8px;padding:8px 14px;font:inherit;font-weight:600;cursor:pointer;
 background:var(--accent);color:var(--card);text-decoration:none}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
button.danger{background:transparent;color:#c0392b;border:1px solid #c0392b55}
.flash{padding:10px 12px;border-radius:8px;margin-top:12px;font-size:13px}
.flash.ok{background:#1a7f4b22;border:1px solid #1a7f4b55} .flash.err{background:#c0392b22;border:1px solid #c0392b55}
.prev{display:flex;gap:10px;flex-wrap:wrap;font-size:13px;margin-top:6px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
"""


def _e(x) -> str:
    return html.escape(str(x), quote=True) if x is not None else ""


def _codes() -> str:
    out = []
    for key, mapping in pf.CHOICE_VI.items():
        if key == "need_label":
            continue
        m = pf.METRICS[key]
        items = "".join(f"<li><code>{_e(c)}</code> — {_e(v)}</li>" for c, v in mapping.items())
        out.append(f'<div><b style="font-size:13px">{_e(m.label)}</b><ul style="margin:4px 0;padding-left:18px;'
                   f'font-size:12px">{items}</ul></div>')
    return "".join(out)


def _gate_rows(prefix: str, gates: list[pf.Gate]) -> str:
    rows = []
    metric_opts = list(pf.METRICS.values())
    for i in range(MAX_GATES):
        g = gates[i] if i < len(gates) else None
        mopts = '<option value="">— không dùng —</option>' + "".join(
            f'<option value="{m.key}" {"selected" if g and g.metric == m.key else ""}>{_e(m.group)} · {_e(m.label)}</option>'
            for m in metric_opts)
        oopts = "".join(f'<option value="{k}" {"selected" if g and g.op == k else ""}>{_e(v)}</option>'
                        for k, v in pf.OPS.items())
        val = "" if g is None else (", ".join(g.value) if isinstance(g.value, list) else
                                    ("true" if g.value is True else "false" if g.value is False else f"{g.value:g}"))
        rows.append(f'<tr><td><select name="{prefix}.g{i}.metric">{mopts}</select></td>'
                    f'<td><select name="{prefix}.g{i}.op">{oopts}</select></td>'
                    f'<td><input type="text" name="{prefix}.g{i}.value" value="{_e(val)}" '
                    f'placeholder="số, hoặc danh sách: scaling, early"></td></tr>')
    return ("<table><thead><tr><th>Chỉ số</th><th>Phép so sánh</th><th>Giá trị</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def _weight_rows(p: pf.Profile) -> str:
    cur = {w.metric: w for w in p.weights}
    rows, group = [], None
    for m in pf.METRICS.values():
        if m.norm is None:
            continue
        if m.group != group:
            group = m.group
            rows.append(f'<tr class="grp"><td colspan="3">{_e(group)}</td></tr>')
        w = cur.get(m.key)
        rows.append(
            f'<tr><td title="{_e(m.help)}">{_e(m.label)}</td>'
            f'<td><input type="number" step="0.5" min="0" name="{p.key}.w.{m.key}" value="{w.weight:g}" '
            f'aria-label="trọng số {_e(m.label)}"></td>' if w else
            f'<tr><td title="{_e(m.help)}">{_e(m.label)}</td>'
            f'<td><input type="number" step="0.5" min="0" name="{p.key}.w.{m.key}" value="0" '
            f'aria-label="trọng số {_e(m.label)}"></td>')
        prefer = w.prefer if w else "high"
        rows[-1] += (f'<td><select name="{p.key}.p.{m.key}"><option value="high" {"selected" if prefer == "high" else ""}>'
                     f'càng cao càng tốt</option><option value="low" {"selected" if prefer == "low" else ""}>'
                     f'càng thấp càng tốt</option></select></td></tr>')
    return ('<table><thead><tr><th>Chỉ số</th><th>Trọng số (0 = bỏ)</th><th>Chiều</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def render_editor(profiles: list[pf.Profile], evidence: pf.EvidenceRule, token: str, yaml_text: str,
                  msg: str = "", err: str = "", preview: dict | None = None) -> str:
    flash = (f'<div class="flash ok">{_e(msg)}</div>' if msg else "") + (f'<div class="flash err">{_e(err)}</div>' if err else "")
    cards = []
    for p in profiles:
        pv = (preview or {}).get(p.key) or {}
        prev = " · ".join(f"{pf.DECISION_VI[k]}: {pv.get(k, 0)}" for k in ("strong", "fit", "weak", "blocked"))
        cards.append(f"""<section class="pcard"><h2>{_e(p.label)} <span class="muted">({_e(p.key)})</span></h2>
<div class="prev muted">Sản phẩm hiện có — {_e(prev) if pv else "chưa có dữ liệu"}</div>
<div class="grid2">
 <div><label>Tên hiển thị</label><input type="text" name="{p.key}.label" value="{_e(p.label)}" maxlength="40"></div>
 <div><label>“Rất hợp” khi điểm ≥</label><input type="number" name="{p.key}.strong" value="{p.strong:g}" min="0" max="100"></div>
 <div><label>“Hợp” khi điểm ≥</label><input type="number" name="{p.key}.fit" value="{p.fit:g}" min="0" max="100"></div>
</div>
<label>Mô tả</label><input type="text" name="{p.key}.description" value="{_e(p.description)}" maxlength="200">
<h3 style="font-size:14px;margin:14px 0 4px">Điều kiện bắt buộc</h3>
<div class="muted">Trượt điều kiện → “Không đạt”. Thiếu dữ liệu → “cần kiểm tra”, không tính là trượt. Nên giữ 2–4 điều kiện.</div>
<div class="wrap" style="margin-top:6px">{_gate_rows(p.key, p.gates)}</div>
<h3 style="font-size:14px;margin:14px 0 4px">Trọng số</h3>
<div class="muted">Trọng số tương đối. Chỉ số thiếu dữ liệu bị bỏ ra và trọng số được chia lại.</div>
<div class="wrap" style="margin-top:6px">{_weight_rows(p)}</div>
<div class="actions"><button type="submit" name="action" value="save">Lưu tất cả</button>
<button class="ghost" type="submit" name="action" value="dup:{p.key}">Nhân bản hồ sơ này</button>
<button class="danger" type="submit" name="action" value="del:{p.key}" onclick="return confirm('Xoá hồ sơ {_e(p.label)}?')">Xoá</button></div>
</section>""")
    body = f"""<style>{CSS}</style>{flash}
<div class="muted">Mỗi sản phẩm / store / angle được chấm theo MỌI hồ sơ cùng lúc. Lưu xong, các trang Sản phẩm, Store và Cơ hội angle
tính lại ngay, không cần quét lại.</div>
<details class="pcard"><summary><b>Bảng mã cho điều kiện dạng danh sách</b> (gõ mã, cách nhau dấu phẩy)</summary>
<div class="grid2" style="margin-top:8px">{_codes()}</div></details>
<form method="post" action="/profiles"><input type="hidden" name="token" value="{token}">
<section class="pcard"><h2>Store bằng chứng</h2>
<div class="muted">Store đạt MỌI điều kiện dưới đây (và có đủ dữ liệu) mới được tính là bằng chứng cho sản phẩm.</div>
<div class="wrap" style="margin-top:6px">{_gate_rows("evidence", evidence.gates)}</div>
<div class="actions"><button type="submit" name="action" value="save">Lưu tất cả</button>
<button class="ghost" type="submit" name="action" value="new">+ Thêm hồ sơ trống</button></div></section>
{"".join(cards)}
</form>
<section class="pcard"><h2>Sửa trực tiếp YAML</h2>
<form method="post" action="/profiles/yaml"><input type="hidden" name="token" value="{token}">
<textarea name="yaml" spellcheck="false">{_e(yaml_text)}</textarea>
<div class="actions"><button type="submit" class="ghost">Lưu YAML</button>
<button type="submit" class="danger" name="reset" value="1" onclick="return confirm('Khôi phục 2 hồ sơ mẫu?')">Khôi phục mặc định</button></div>
</form></section>"""
    return page("Hồ sơ chiến lược", body, active="profiles")


def _parse_gates(form: dict[str, list[str]], prefix: str) -> list[pf.Gate]:
    def one(k):
        return (form.get(k) or [""])[0].strip()

    gates = []
    for i in range(MAX_GATES):
        metric = one(f"{prefix}.g{i}.metric")
        if not metric:
            continue
        m = pf.METRICS.get(metric)
        raw = one(f"{prefix}.g{i}.value")
        value = [v.strip() for v in raw.split(",") if v.strip()] if m and m.kind == "category" else raw
        g = pf.Gate(metric, one(f"{prefix}.g{i}.op"), value)
        try:
            pf.validate_gate(g)
        except pf.ProfileError as exc:
            raise pf.ProfileError(f"{prefix}: {exc}") from None
        gates.append(g)
    return gates


def parse_form(form: dict[str, list[str]], current: list[pf.Profile]) -> tuple[list[pf.Profile], pf.EvidenceRule]:
    def one(k, default=""):
        return (form.get(k) or [default])[0].strip()

    out = []
    for p in current:
        weights = []
        for m in pf.METRICS.values():
            if m.norm is None:
                continue
            try:
                w = float(one(f"{p.key}.w.{m.key}", "0") or 0)
            except ValueError:
                raise pf.ProfileError(f"{p.label}: trọng số '{m.label}' phải là số") from None
            if w < 0:
                raise pf.ProfileError(f"{p.label}: trọng số không được âm")
            if w > 0:
                prefer = one(f"{p.key}.p.{m.key}", "high")
                weights.append(pf.Weight(m.key, w, prefer if prefer in ("high", "low") else "high"))
        try:
            strong, fit = float(one(f"{p.key}.strong", "70")), float(one(f"{p.key}.fit", "50"))
        except ValueError:
            raise pf.ProfileError(f"{p.label}: ngưỡng phải là số") from None
        if not 0 <= fit <= strong <= 100:
            raise pf.ProfileError(f"{p.label}: cần 0 ≤ “Hợp” ≤ “Rất hợp” ≤ 100")
        if not weights:
            raise pf.ProfileError(f"{p.label}: cần ít nhất 1 chỉ số có trọng số > 0")
        out.append(pf.Profile(p.key, one(f"{p.key}.label") or p.label, one(f"{p.key}.description"),
                              _parse_gates(form, p.key), weights, strong, fit))
    return out, pf.EvidenceRule(_parse_gates(form, "evidence"))


def new_key(existing: list[pf.Profile], base: str) -> str:
    keys = {p.key for p in existing}
    i = 2
    while f"{base}{i}" in keys:
        i += 1
    return f"{base}{i}"


def preview(model) -> dict:
    out: dict[str, Counter] = {}
    for e in model.products:
        for ev in e.evals:
            out.setdefault(ev.profile, Counter())[ev.decision] += 1
    return {k: dict(v) for k, v in out.items()}


def _pct(x) -> str:
    return "–" if x is None else f"{x:.0%}"


def render_calibration(stats: dict) -> str:
    c = stats["counts"]
    n = c.get("win", 0) + c.get("fail", 0)
    if n < 2 or not c.get("win") or not c.get("fail"):
        body = ('<p>Chưa đủ dữ liệu để hiệu chỉnh. Hãy đánh dấu <b>Đã win</b> / <b>Đã fail</b> cho các sản phẩm, store '
                'hoặc angle bạn đã thử thực tế (nút ở trang Sản phẩm / Store / Cơ hội angle). Cần ít nhất 1 win và 1 fail; '
                'càng nhiều càng đáng tin.</p>'
                f'<p class="muted">Hiện có: {c.get("win", 0)} win · {c.get("fail", 0)} fail</p>')
        return page("Hiệu chỉnh", body, active="calibration")
    mrows = "".join(
        f'<tr><td>{_e(m["label"])}</td><td class="num">{m["win"]:.2f}</td><td class="num">{m["fail"]:.2f}</td>'
        f'<td class="num" style="color:{"#1a7f4b" if m["gap"] > 0 else "#c0392b"}">{m["gap"]:+.2f}</td>'
        f'<td class="num">{m["n"]}</td></tr>' for m in stats["metrics"])
    prow = "".join(
        f'<tr><td>{_e(p["profile"])}</td><td class="num">{_e(p["win"])}</td><td class="num">{_e(p["fail"])}</td>'
        f'<td class="num">{_pct(p["accuracy"])}</td></tr>' for p in stats["profiles"])
    body = f"""<div class="muted">{c.get("win", 0)} win · {c.get("fail", 0)} fail đã đánh dấu.
Chênh lệch dương lớn = chỉ số phân biệt tốt (win cao hơn fail) → nên tăng trọng số. Gần 0 = không giúp phân biệt.
Âm = ngược kỳ vọng → cân nhắc đổi chiều hoặc bỏ. Ít mẫu thì chỉ tham khảo.</div>
<div class="wrap"><table><thead><tr><th>Hồ sơ</th><th class="num">Điểm TB (win)</th><th class="num">Điểm TB (fail)</th>
<th class="num">Đoán đúng</th></tr></thead><tbody>{prow}</tbody></table></div>
<div class="wrap"><table><thead><tr><th>Chỉ số (đã chuẩn hoá 0–1)</th><th class="num">TB win</th><th class="num">TB fail</th>
<th class="num">Chênh lệch</th><th class="num">Số mẫu</th></tr></thead><tbody>{mrows}</tbody></table></div>"""
    return page("Hiệu chỉnh", body, active="calibration")
