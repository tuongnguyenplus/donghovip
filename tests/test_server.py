import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from winfinder import niches as nm
from winfinder.config import load_config
from winfinder.runner import ScanResult, only_niche, run_scan
from winfinder.server import App, make_handler

CONFIG = """database: data/w.db
report_dir: reports
niches_file: niches.yaml
trends:
  provider: csv
  geos: [US]
  expand: {autocomplete: false}
"""


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG)
    nm.save_niches(tmp_path / "niches.yaml", [nm.Niche("posture", ["posture corrector", "back brace"], ["en"])])
    return tmp_path


# ------------------------------------------------------------------ ngách
def test_split_list():
    assert nm.split_list(" a, b\nc ;a\n\n") == ["a", "b", "c"]


@pytest.mark.parametrize("niche,msg", [
    (nm.Niche("", ["x"]), "trống"),
    (nm.Niche("a", []), "keyword"),
    (nm.Niche("a", ["x"], ["english"]), "ngôn ngữ"),
    (nm.Niche("a" * 61, ["x"]), "60"),
])
def test_validate_errors(niche, msg):
    with pytest.raises(nm.NicheError, match=msg):
        nm.validate(niche, [])


def test_upsert_rename_duplicate_delete(workdir):
    items = nm.load_niches(workdir / "niches.yaml")
    items = nm.upsert(items, nm.Niche("Eye Care", ["Eye Bags", "eye bags"], ["EN"]))
    assert items[-1].keywords == ["eye bags"] and items[-1].languages == ["en"]
    with pytest.raises(nm.NicheError, match="Đã có"):
        nm.upsert(items, nm.Niche("eye care", ["x"]))
    items = nm.upsert(items, nm.Niche("eyes", ["eye bags"]), original_name="Eye Care")
    assert [n.name for n in items] == ["posture", "eyes"]
    items = nm.delete(items, "EYES")
    assert [n.name for n in items] == ["posture"]
    with pytest.raises(nm.NicheError):
        nm.delete(items, "nope")


def test_save_load_roundtrip_and_config(workdir):
    items = nm.upsert(nm.load_niches(workdir / "niches.yaml"), nm.Niche("off", ["x y"], enabled=False))
    nm.save_niches(workdir / "niches.yaml", items)
    assert nm.load_niches(workdir / "niches.yaml") == items
    cfg = load_config(workdir / "config.yaml")
    assert [s.name for s in cfg.searches] == ["posture"]           # ngách tắt không được quét
    t = cfg.trends.topics[0]
    assert (t.name, t.seeds, t.keywords) == ("posture", ["posture corrector"], ["back brace"])
    assert not list(workdir.glob(".niches-*"))                     # không để lại file tạm


def test_only_niche(workdir):
    nm.save_niches(workdir / "niches.yaml", [nm.Niche("a", ["x"]), nm.Niche("b", ["y"])])
    cfg = only_niche(load_config(workdir / "config.yaml"), "B")
    assert [s.name for s in cfg.searches] == ["b"] and [t.name for t in cfg.trends.topics] == ["b"]


def test_run_scan_skips_steps_without_keys(workdir, monkeypatch):
    for k in ("META_AD_LIBRARY_TOKEN", "ANTHROPIC_API_KEY", "SERPAPI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    res = run_scan(load_config(workdir / "config.yaml"))
    assert "META_AD_LIBRARY_TOKEN" in res.skipped["collect"]
    assert res.ok and "link" in res.skipped  # không có store nào → bỏ qua, không lỗi


# ------------------------------------------------------------------ web server
class Client:
    def __init__(self, workdir, runner):
        self.app = App(workdir / "config.yaml", runner=runner)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app, {"127.0.0.1", "localhost"}))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def req(self, path, data=None, host=None, token=True):
        if data is not None and token:
            data = {"token": self.app.token, **data}
        body = urlencode(data, doseq=True).encode() if data is not None else None
        r = urllib.request.Request(self.base + path, data=body)
        if host:
            r.add_header("Host", host)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            resp = opener.open(r, timeout=5)
            return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def client(workdir, monkeypatch):
    for k in ("META_AD_LIBRARY_TOKEN", "ANTHROPIC_API_KEY", "SERPAPI_API_KEY"):
        monkeypatch.setenv(k, "")   # ghi nhận trạng thái gốc → tự khôi phục sau test (server có thể set key)
        monkeypatch.delenv(k)
    calls = []

    def fake_runner(cfg, steps):
        calls.append(([s.name for s in cfg.searches], steps))
        import logging
        logging.getLogger("winfinder.scan").info("▶ giả lập quét")
        time.sleep(0.2)
        return ScanResult(done=["trends"], skipped={"collect": "chưa có token"})

    c = Client(workdir, fake_runner)
    c.calls = calls
    yield c
    c.close()


def test_home_page(client):
    status, body, headers = client.req("/")
    assert status == 200 and "posture corrector" in body and client.app.token in body
    assert headers["X-Frame-Options"] == "DENY"


def test_post_requires_token(client, workdir):
    status, _, _ = client.req("/niches", {"name": "x", "keywords": "y"}, token=False)
    assert status == 403
    assert len(nm.load_niches(workdir / "niches.yaml")) == 1


def test_bad_host_rejected(client):
    assert client.req("/", host="evil.example")[0] == 403


def test_add_edit_toggle_delete_niche(client, workdir):
    s, _, h = client.req("/niches", {"name": "Eye care", "keywords": "eye bags\nred light therapy",
                                     "languages": "en, de", "enabled": "1", "note": "test"})
    assert s == 303 and "msg=" in h["Location"]
    n = nm.load_niches(workdir / "niches.yaml")[-1]
    assert (n.name, n.keywords, n.languages, n.enabled) == ("Eye care", ["eye bags", "red light therapy"], ["en", "de"], True)

    s, _, h = client.req("/niches", {"name": "posture", "keywords": "x"})  # trùng tên
    assert s == 303 and "err=" in h["Location"]

    client.req("/niches", {"original_name": "Eye care", "name": "Eyes", "keywords": "eye bags", "enabled": "1"})
    client.req("/niches/toggle", {"name": "Eyes"})
    assert nm.load_niches(workdir / "niches.yaml")[-1].enabled is False
    client.req("/niches/delete", {"name": "Eyes"})
    assert [n.name for n in nm.load_niches(workdir / "niches.yaml")] == ["posture"]


def test_scan_job_lifecycle(client):
    s, _, _ = client.req("/scan", {"niche": "posture", "steps": ["trends", "bogus"]})
    assert s == 303
    s2, _, h = client.req("/scan", {})  # đang chạy → từ chối lần 2
    assert "err=" in h["Location"]
    for _ in range(50):
        st = json.loads(client.req("/api/status")[1])
        if not st["running"]:
            break
        time.sleep(0.05)
    assert st["result"]["done"] == ["trends"] and st["result"]["skipped"]["collect"]
    assert any("giả lập quét" in line for line in st["log"])
    assert client.calls == [(["posture"], ["trends"])]


def test_scan_unknown_niche(client):
    _, _, h = client.req("/scan", {"niche": "nope"})
    assert "err=" in h["Location"] and client.calls == []


def test_reports_served_with_nav_and_no_traversal(client, workdir):
    (workdir / "reports").mkdir()
    (workdir / "reports" / "winners.html").write_text('<nav class="nav"><a href="winners.html">x</a></nav>')
    s, body, _ = client.req("/winners.html")
    assert s == 200 and 'href="/"' in body
    assert client.req("/../config.yaml")[0] == 404
    assert client.req("/config.yaml")[0] == 404
    assert client.req("/missing.html")[0] == 404


def test_keys_saved_privately_and_never_echoed(client, workdir):
    s, _, _ = client.req("/keys", {"ANTHROPIC_API_KEY": "sk-ant-secret-123", "EVIL": "x"})
    assert s == 303
    env = workdir / ".env"
    assert "ANTHROPIC_API_KEY=sk-ant-secret-123" in env.read_text() and "EVIL" not in env.read_text()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    body = client.req("/")[1]
    assert "sk-ant-secret-123" not in body and "✓ đã có" in body
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-secret-123"


# ------------------------------------------------------------------ hồ sơ, đánh dấu, 3 góc nhìn
def test_dynamic_views_profiles_and_labels(client, workdir):
    import shutil
    from datetime import date

    from winfinder import db as wdb, demo, link, link_report, report
    from winfinder.analysis.scoring import analyze
    from winfinder.config import load_config
    from winfinder.sources.adlibrary import parse_ad

    as_of = date(2026, 11, 16)
    cfg = load_config(workdir / "config.yaml")
    cfg.link.min_ad_score, cfg.link.fetch_pages = 0, False
    conn = wdb.connect(cfg.database)
    for search, country, raw in demo.generate(as_of):
        wdb.upsert_ads(conn, [parse_ad(raw, cfg.exclude_domains)], country=country, search=search,
                       keyword="k", observed_on=as_of)
    stores = analyze(conn, cfg, as_of)
    report.write_json(stores, cfg.report_dir / "latest.json", as_of)
    link_report.write(link.link_stores(conn, cfg, stores, demo.DemoTrendsProvider(as_of), as_of), cfg.report_dir,
                      as_of, "demo")

    s, body, _ = client.req("/products.html")
    assert s == 200 and "posture corrector" in body and 'href="/profiles"' in body and 'name="label"' in body
    assert client.req("/stores.html?sort=nope")[0] == 200           # hồ sơ không tồn tại → bỏ qua
    assert client.req("/opportunities.html")[0] == 200

    s, _, h = client.req("/label", {"kind": "store", "key": "postureglow.com", "label": "win"})
    assert s == 303 and h["Location"] == "/stores.html#s-postureglow-com"
    assert wdb.get_labels(wdb.connect(cfg.database))[("store", "postureglow.com")] == "win"
    client.req("/label", {"kind": "store", "key": "postureglow.com", "label": "win"})   # bấm lại = bỏ nhãn
    assert ("store", "postureglow.com") not in wdb.get_labels(wdb.connect(cfg.database))
    assert "err=" in client.req("/label", {"kind": "x", "key": "a", "label": "win"})[2]["Location"]

    s, body, _ = client.req("/profiles")
    assert s == 200 and 'name="quick.strong"' in body
    assert client.req("/calibration")[0] == 200

    good_yaml = (workdir / "profiles.yaml").read_text()
    _, _, h = client.req("/profiles/yaml", {"yaml": "profiles: [{key: 'bad key'}]"})
    assert "err=" in h["Location"] and (workdir / "profiles.yaml").read_text() == good_yaml
    _, _, h = client.req("/profiles/yaml", {"yaml": "profiles: [ {"})                  # lỗi cú pháp YAML
    assert "err=" in h["Location"] and (workdir / "profiles.yaml").read_text() == good_yaml

    from winfinder import profiles as wpf
    profiles, ev = wpf.load(workdir / "profiles.yaml")
    n = len(profiles)
    _, _, h = client.req("/profiles", {"action": "dup:quick", **{f"{p.key}.w.{w.metric}": str(w.weight)
                                                                for p in profiles for w in p.weights},
                                       **{f"{p.key}.label": p.label for p in profiles}})
    assert "msg=" in h["Location"] and len(wpf.load(workdir / "profiles.yaml")[0]) == n + 1
    shutil.rmtree(cfg.report_dir)
    assert "Chưa có dữ liệu" in client.req("/products.html")[1]
