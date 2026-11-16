"""Giao diện web trên máy: quản lý ngách, bấm quét, xem báo cáo.

    winfinder serve            # mở http://127.0.0.1:8765

Chỉ dùng thư viện chuẩn. Chỉ nghe trên 127.0.0.1; mọi thao tác ghi cần token của phiên (chống request giả
từ trang web khác); kiểm tra Host header (chống DNS rebinding). API key lưu trong `.env` trên máy
(đã gitignore), không bao giờ hiển thị lại giá trị.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from winfinder import niches as nm
from winfinder.config import load_config
from winfinder.report import BASE_CSS, NAV
from winfinder.runner import STEP_LABELS, STEPS, ScanResult, only_niche, run_scan

log = logging.getLogger(__name__)

KEYS = {
    "META_AD_LIBRARY_TOKEN": "Meta Ad Library — bắt buộc để lấy ad",
    "ANTHROPIC_API_KEY": "Claude — hiểu sản phẩm, phân tích angle",
    "SERPAPI_API_KEY": "SerpApi — Google Trends ổn định (không có: gọi Google trực tiếp)",
}
REPORT_RE = re.compile(r"^/([a-z_]+\.(?:html|json))$")


def _e(x) -> str:
    return html.escape(str(x), quote=True) if x is not None else ""


# ------------------------------------------------------------------ .env
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k in KEYS and v.strip() and not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")


def save_env(path: Path, updates: dict[str, str]) -> None:
    current: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                current[k.strip()] = v.strip()
    for k, v in updates.items():
        if k not in KEYS:
            continue
        if v:
            current[k] = v
            os.environ[k] = v
        else:
            current.pop(k, None)
            os.environ.pop(k, None)
    body = "# API key của WinFinder — chỉ nằm trên máy này, không commit lên GitHub\n"
    body += "".join(f"{k}={v}\n" for k, v in current.items())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)


# ------------------------------------------------------------------ job quét chạy nền
class _ListHandler(logging.Handler):
    def __init__(self, sink: deque):
        super().__init__(logging.INFO)
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.sink.append(self.format(record).split("\nTraceback")[0])
        except Exception:
            pass


class ScanJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.lines: deque[str] = deque(maxlen=400)
        self.started: str | None = None
        self.finished: str | None = None
        self.label = ""
        self.result: ScanResult | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, config_path: Path, niche: str | None, steps: list[str], runner=run_scan) -> bool:
        with self.lock:
            if self.running:
                return False
            self.lines.clear()
            self.result, self.error, self.finished = None, None, None
            self.started = datetime.now().strftime("%H:%M:%S %d/%m")
            self.label = f"ngách “{niche}”" if niche else "tất cả ngách đang bật"
            self.thread = threading.Thread(target=self._run, args=(config_path, niche, steps, runner), daemon=True)
            self.thread.start()
            return True

    def _run(self, config_path: Path, niche: str | None, steps: list[str], runner) -> None:
        handler = _ListHandler(self.lines)
        root = logging.getLogger("winfinder")
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.INFO)
        try:
            cfg = only_niche(load_config(config_path), niche)
            self.result = runner(cfg, steps)
        except Exception as exc:
            log.exception("Lần quét lỗi")
            self.error = str(exc)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
            self.finished = datetime.now().strftime("%H:%M:%S %d/%m")

    def status(self) -> dict:
        r = self.result
        return {
            "running": self.running, "label": self.label, "started": self.started, "finished": self.finished,
            "log": list(self.lines)[-200:], "error": self.error,
            "result": None if r is None else {"done": r.done, "skipped": r.skipped, "failed": r.failed},
        }


# ------------------------------------------------------------------ HTML
APP_CSS = """
section.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-top:16px}
section.box h2{font-size:16px;margin:0 0 10px}
label{display:block;font-size:12px;color:var(--muted);margin:8px 0 4px}
input[type=text],input[type=password],textarea,select{width:100%;padding:8px 10px;border:1px solid var(--line);
 border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
textarea{min-height:72px;resize:vertical}
.row{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
button,.btn{display:inline-block;border:0;border-radius:8px;padding:8px 14px;font:inherit;font-weight:600;cursor:pointer;
 background:var(--accent);color:var(--card);text-decoration:none}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
button.danger{background:transparent;color:#c0392b;border:1px solid #c0392b55}
button:disabled{opacity:.5;cursor:not-allowed}
.inline{display:inline}
.flash{padding:10px 12px;border-radius:8px;margin-top:12px;font-size:13px}
.flash.ok{background:#1a7f4b22;border:1px solid #1a7f4b55} .flash.err{background:#c0392b22;border:1px solid #c0392b55}
.chips .chip{margin-right:4px}
.off{opacity:.5}
pre.log{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:320px;overflow:auto;
 font-size:12px;white-space:pre-wrap;margin:10px 0 0}
.steps label{display:inline-flex;gap:6px;align-items:center;margin-right:14px;color:var(--fg);font-size:13px}
.keyok{color:#1a7f4b;font-weight:700} .keymiss{color:#c0392b;font-weight:700}
td .actions{display:flex;gap:6px;flex-wrap:wrap}
details.edit summary{color:var(--accent);font-size:13px;margin:0}
"""


def _nav(active: str) -> str:
    items = [("/", "⚙ Ngách & quét")] + [("/" + h, label) for h, label in NAV]
    return "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{_e(t)}</a>' for h, t in items)


def _niche_form(token: str, n: nm.Niche | None = None) -> str:
    return f"""<form method="post" action="/niches">
<input type="hidden" name="token" value="{token}">
<input type="hidden" name="original_name" value="{_e(n.name if n else '')}">
<div class="row">
 <div><label>Tên ngách</label><input type="text" name="name" required maxlength="60" value="{_e(n.name if n else '')}"
   placeholder="vd: eye care"></div>
 <div><label>Ngôn ngữ ad (mã 2 chữ, cách nhau dấu phẩy; trống = mọi ngôn ngữ)</label>
   <input type="text" name="languages" value="{_e(', '.join(n.languages) if n else 'en')}" placeholder="en"></div>
</div>
<label>Keyword tìm ad (mỗi dòng một keyword, hoặc cách nhau dấu phẩy) — keyword đầu tiên là keyword chính cho Trends</label>
<textarea name="keywords" required placeholder="red light therapy&#10;eye bags">{_e(chr(10).join(n.keywords) if n else '')}</textarea>
<label>Ghi chú</label><input type="text" name="note" maxlength="200" value="{_e(n.note if n else '')}">
<label class="steps"><input type="checkbox" name="enabled" value="1" {"checked" if (n is None or n.enabled) else ""}> Bật (được quét)</label>
<p><button type="submit">{"Lưu thay đổi" if n else "Thêm ngách"}</button></p>
</form>"""


def render_app(niches: list[nm.Niche], token: str, job: ScanJob, msg: str = "", err: str = "",
               reports: list[str] | None = None) -> str:
    flash = ""
    if msg:
        flash += f'<div class="flash ok">{_e(msg)}</div>'
    if err:
        flash += f'<div class="flash err">{_e(err)}</div>'
    rows = ""
    for n in niches:
        rows += f"""<tr class="{'' if n.enabled else 'off'}">
<td><b>{_e(n.name)}</b>{'' if n.enabled else ' <span class="muted">(tắt)</span>'}<div class="muted">{_e(n.note)}</div></td>
<td class="chips">{''.join(f'<span class="chip">{_e(k)}</span>' for k in n.keywords)}</td>
<td>{_e(', '.join(n.languages) or 'mọi')}</td>
<td><div class="actions">
 <form class="inline" method="post" action="/scan"><input type="hidden" name="token" value="{token}">
  <input type="hidden" name="niche" value="{_e(n.name)}">{''.join(f'<input type="hidden" name="steps" value="{s}">' for s in STEPS)}
  <button class="ghost" type="submit" {'disabled' if job.running or not n.enabled else ''}>Quét</button></form>
 <form class="inline" method="post" action="/niches/toggle"><input type="hidden" name="token" value="{token}">
  <input type="hidden" name="name" value="{_e(n.name)}"><button class="ghost" type="submit">{'Tắt' if n.enabled else 'Bật'}</button></form>
 <form class="inline" method="post" action="/niches/delete" onsubmit="return confirm('Xoá ngách {_e(n.name)}?')">
  <input type="hidden" name="token" value="{token}"><input type="hidden" name="name" value="{_e(n.name)}">
  <button class="danger" type="submit">Xoá</button></form>
</div>
<details class="edit"><summary>Sửa</summary>{_niche_form(token, n)}</details></td>
</tr>"""
    if not rows:
        rows = '<tr><td colspan="4" class="muted">Chưa có ngách nào — thêm ngách ở bên dưới.</td></tr>'

    keys = "".join(
        f'<tr><td><code>{k}</code></td><td>{_e(desc)}</td><td>'
        f'{"<span class=keyok>✓ đã có</span>" if os.environ.get(k) else "<span class=keymiss>✗ chưa có</span>"}</td>'
        f'<td><input type="password" name="{k}" autocomplete="off" placeholder="dán key mới để thay"></td></tr>'
        for k, desc in KEYS.items()
    )
    steps = "".join(
        f'<label><input type="checkbox" name="steps" value="{s}" checked> {_e(STEP_LABELS[s])}</label>' for s in STEPS
    )
    report_links = " · ".join(f'<a href="/{r}">{_e(dict(NAV).get(r, r))}</a>' for r in (reports or [])) or \
        '<span class="muted">chưa có báo cáo — bấm Quét</span>'
    enabled = sum(1 for n in niches if n.enabled)
    status = json.dumps(job.status(), ensure_ascii=False).replace("</", "<\\/")

    body = f"""{flash}
<section class="box"><h2>Quét</h2>
<form method="post" action="/scan" id="scanform"><input type="hidden" name="token" value="{token}">
<div class="steps">{steps}</div>
<p><button type="submit" id="scanbtn" {'disabled' if job.running or not enabled else ''}>Quét {enabled} ngách đang bật</button>
<span class="muted" id="scanstate"></span></p></form>
<div class="muted">Báo cáo: {report_links}</div>
<pre class="log" id="log" hidden></pre>
</section>

<section class="box"><h2>Ngách ({len(niches)})</h2>
<div class="wrap" style="margin-top:0"><table>
<thead><tr><th>Ngách</th><th>Keyword</th><th>Ngôn ngữ</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>
<details style="margin-top:12px" {'open' if not niches else ''}><summary style="font-size:14px;color:var(--accent)">+ Thêm ngách</summary>
{_niche_form(token)}</details>
</section>

<section class="box"><h2>API key</h2>
<form method="post" action="/keys"><input type="hidden" name="token" value="{token}">
<div class="wrap" style="margin-top:0"><table><tbody>{keys}</tbody></table></div>
<p class="muted">Key lưu trong file <code>.env</code> trên máy này (không đưa lên GitHub). Để trống = giữ nguyên.</p>
<p><button type="submit" class="ghost">Lưu key</button></p></form>
</section>

<script>
const initial = {status};
const logEl = document.getElementById('log'), stateEl = document.getElementById('scanstate'),
      btn = document.getElementById('scanbtn');
function show(s) {{
  if (s.log && s.log.length) {{ logEl.hidden = false; logEl.textContent = s.log.join('\\n'); logEl.scrollTop = logEl.scrollHeight; }}
  if (s.running) {{ stateEl.textContent = 'Đang quét ' + s.label + ' (bắt đầu ' + s.started + ')…'; btn.disabled = true; }}
  else if (s.finished) {{
    const r = s.result || {{}};
    const sk = Object.keys(r.skipped || {{}}).length, fl = Object.keys(r.failed || {{}}).length;
    stateEl.textContent = 'Xong lúc ' + s.finished + (s.error ? ' — lỗi: ' + s.error : '') +
      (fl ? ' — ' + fl + ' bước lỗi' : '') + (sk ? ' — ' + sk + ' bước bỏ qua' : '');
  }}
  return s.running;
}}
async function poll() {{
  try {{
    const s = await (await fetch('/api/status', {{cache: 'no-store'}})).json();
    if (show(s)) setTimeout(poll, 2000); else if (initial.running) location.replace('/');
  }} catch (e) {{ setTimeout(poll, 4000); }}
}}
if (show(initial)) setTimeout(poll, 2000);
</script>"""
    return (
        '<!doctype html><html lang="vi"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>WinFinder</title>'
        f"<style>{BASE_CSS}{APP_CSS}</style></head><body><main>"
        f'<nav class="nav">{_nav("/")}</nav><h1>Ngách &amp; quét</h1>{body}</main></body></html>'
    )


# ------------------------------------------------------------------ HTTP
class App:
    def __init__(self, config_path: Path, runner=run_scan):
        self.config_path = Path(config_path)
        self.base = self.config_path.parent
        self.env_path = self.base / ".env"
        self.token = secrets.token_urlsafe(24)
        self.job = ScanJob()
        self.runner = runner
        load_env(self.env_path)

    @property
    def cfg(self):
        return load_config(self.config_path)

    def niches_path(self) -> Path:
        return self.cfg.niches_file

    def reports(self) -> list[str]:
        d = self.cfg.report_dir
        return [h for h, _ in NAV if (d / h).exists()]


def make_handler(app: App, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WinFinder"

        def log_message(self, fmt, *args):  # yên lặng, log qua logging
            log.debug("%s " + fmt, self.address_string(), *args)

        # -------------------------------------------------- tiện ích
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in allowed_hosts:
                self._send(HTTPStatus.FORBIDDEN, "Host không hợp lệ", "text/plain")
                return False
            return True

        def _send(self, status, body: str | bytes, ctype="text/html; charset=utf-8"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, msg: str = "", err: str = ""):
            q = "&".join(f"{k}={quote(v)}" for k, v in (("msg", msg), ("err", err)) if v)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/" + (f"?{q}" if q else ""))
            self.end_headers()

        def _form(self) -> dict[str, list[str]] | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 100_000:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Quá lớn", "text/plain")
                return None
            form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            if not secrets.compare_digest((form.get("token") or [""])[0], app.token):
                self._send(HTTPStatus.FORBIDDEN, "Phiên hết hạn — tải lại trang rồi thử lại", "text/plain; charset=utf-8")
                return None
            return form

        # -------------------------------------------------- GET
        def do_GET(self):
            if not self._host_ok():
                return
            url = urlparse(self.path)
            if url.path == "/":
                q = parse_qs(url.query)
                self._send(HTTPStatus.OK, render_app(
                    nm.load_niches(app.niches_path()), app.token, app.job,
                    (q.get("msg") or [""])[0], (q.get("err") or [""])[0], app.reports()))
                return
            if url.path == "/api/status":
                self._send(HTTPStatus.OK, json.dumps(app.job.status(), ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            m = REPORT_RE.match(url.path)
            if m:
                f = app.cfg.report_dir / m.group(1)
                if f.is_file():
                    data = f.read_text(encoding="utf-8")
                    if f.suffix == ".html":  # thêm link về trang quản lý vào thanh điều hướng
                        data = data.replace('<nav class="nav">', '<nav class="nav"><a href="/">⚙ Ngách &amp; quét</a>', 1)
                        self._send(HTTPStatus.OK, data)
                    else:
                        self._send(HTTPStatus.OK, data, "application/json; charset=utf-8")
                    return
            self._send(HTTPStatus.NOT_FOUND, "Không tìm thấy", "text/plain; charset=utf-8")

        # -------------------------------------------------- POST
        def do_POST(self):
            if not self._host_ok():
                return
            form = self._form()
            if form is None:
                return

            def one(k: str) -> str:
                return (form.get(k) or [""])[0]

            path = urlparse(self.path).path
            try:
                if path == "/niches":
                    niche = nm.Niche(one("name"), nm.split_list(one("keywords")), nm.split_list(one("languages")),
                                     one("enabled") == "1", one("note"))
                    original = one("original_name") or None
                    nm.save_niches(app.niches_path(), nm.upsert(nm.load_niches(app.niches_path()), niche, original))
                    self._redirect(msg=f"Đã lưu ngách “{niche.name.strip()}”")
                elif path == "/niches/delete":
                    nm.save_niches(app.niches_path(), nm.delete(nm.load_niches(app.niches_path()), one("name")))
                    self._redirect(msg=f"Đã xoá ngách “{one('name')}”")
                elif path == "/niches/toggle":
                    items = nm.load_niches(app.niches_path())
                    for n in items:
                        if n.name.lower() == one("name").lower():
                            n.enabled = not n.enabled
                            break
                    else:
                        raise nm.NicheError(f"Không tìm thấy ngách “{one('name')}”")
                    nm.save_niches(app.niches_path(), items)
                    self._redirect()
                elif path == "/scan":
                    steps = [s for s in form.get("steps", []) if s in STEPS] or STEPS
                    niche = one("niche") or None
                    if niche and not any(n.name.lower() == niche.lower() and n.enabled
                                         for n in nm.load_niches(app.niches_path())):
                        raise nm.NicheError(f"Ngách “{niche}” không tồn tại hoặc đang tắt")
                    if app.job.start(app.config_path, niche, steps, app.runner):
                        self._redirect(msg="Đã bắt đầu quét — theo dõi log bên dưới")
                    else:
                        self._redirect(err="Đang có một lần quét chạy, đợi xong rồi bấm lại")
                elif path == "/keys":
                    updates = {k: one(k).strip() for k in KEYS if one(k).strip()}
                    if updates:
                        save_env(app.env_path, updates)
                    self._redirect(msg=f"Đã lưu {len(updates)} key" if updates else "Không có key nào thay đổi")
                else:
                    self._send(HTTPStatus.NOT_FOUND, "Không tìm thấy", "text/plain; charset=utf-8")
            except nm.NicheError as exc:
                self._redirect(err=str(exc))

    return Handler


def serve(config_path: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    app = App(config_path)
    allowed = {"127.0.0.1", "localhost", "::1", host.lower()}
    httpd = ThreadingHTTPServer((host, port), make_handler(app, allowed))
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{httpd.server_address[1]}/"
    print(f"WinFinder đang chạy: {url}  (Ctrl+C để dừng)")
    if open_browser:
        import webbrowser

        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        time.sleep(0)
