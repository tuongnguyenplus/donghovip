"""Nguồn dữ liệu Google Trends + mở rộng keyword.

Provider (chọn trong config `trends.provider`):
  - serpapi : SerpApi google_trends engine — ổn định, chạy tốt trên GitHub Actions, cần SERPAPI_API_KEY.
  - direct  : gọi thẳng endpoint nội bộ của trends.google.com — miễn phí nhưng không chính thức,
              hay bị 429 với IP máy chủ (GitHub Actions). Nên chạy trên máy cá nhân.
  - csv     : đọc file CSV tải tay từ trends.google.com (nút Download) — luôn dùng được.

Mọi provider trả về: {keyword: [(date, value), ...]} theo tuần, chuẩn hoá 0–100 TRONG cùng một request.
Tối đa 5 keyword/request; `fetch_keyword_set` dùng keyword neo (anchor) để ghép nhiều request
về cùng một thang đo.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol

import requests

from winfinder.http import get_json, get_text, make_session

log = logging.getLogger(__name__)

Series = list[tuple[date, float]]
MAX_PER_REQUEST = 5


class TrendsError(RuntimeError):
    pass


class TrendsProvider(Protocol):
    name: str

    def interest_over_time(self, keywords: list[str], geo: str, timeframe: str) -> dict[str, Series]: ...


def _ts_to_date(ts: str | int) -> date:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()


# ---------------------------------------------------------------- SerpApi
class SerpApiTrends:
    name = "serpapi"
    URL = "https://serpapi.com/search.json"

    def __init__(self, api_key: str, session: requests.Session | None = None, sleep_seconds: float = 1.0):
        if not api_key:
            raise TrendsError("Thiếu SERPAPI_API_KEY")
        self.api_key = api_key
        self.session = session or make_session()
        self.sleep_seconds = sleep_seconds

    def interest_over_time(self, keywords: list[str], geo: str, timeframe: str) -> dict[str, Series]:
        data = get_json(
            self.session,
            self.URL,
            {
                "engine": "google_trends",
                "q": ",".join(keywords),
                "geo": "" if geo in ("", "WW") else geo,
                "date": timeframe,
                "data_type": "TIMESERIES",
                "tz": 0,
                "api_key": self.api_key,
            },
            timeout=90,
        )
        if "error" in data:
            raise TrendsError(f"SerpApi: {data['error']}")
        out: dict[str, Series] = {k: [] for k in keywords}
        for point in (data.get("interest_over_time") or {}).get("timeline_data", []):
            d = _ts_to_date(point["timestamp"])
            for v in point.get("values", []):
                if v.get("query") in out:
                    out[v["query"]].append((d, float(v.get("extracted_value") or 0)))
        time.sleep(self.sleep_seconds)
        return out


# ---------------------------------------------------------------- Direct (không chính thức)
class DirectTrends:
    name = "direct"
    BASE = "https://trends.google.com/trends/api"

    def __init__(self, session: requests.Session | None = None, hl: str = "en-US", sleep_seconds: float = 5.0):
        self.session = session or make_session()
        self.hl = hl
        self.sleep_seconds = sleep_seconds
        self._warmed = False

    @staticmethod
    def _strip(text: str) -> dict:
        # Google thêm tiền tố ")]}'" trước JSON
        return json.loads(text[text.index("{"):])

    def _warm(self) -> None:
        if not self._warmed:
            try:  # lấy cookie NID, thiếu nó Google trả 429 ngay
                self.session.get("https://trends.google.com/trends/explore", params={"geo": "US"}, timeout=30)
            except requests.RequestException:
                pass
            self._warmed = True

    def interest_over_time(self, keywords: list[str], geo: str, timeframe: str) -> dict[str, Series]:
        self._warm()
        geo = "" if geo in ("", "WW") else geo
        req = {
            "comparisonItem": [{"keyword": k, "geo": geo, "time": timeframe} for k in keywords],
            "category": 0,
            "property": "",
        }
        explore = self._strip(
            get_text(self.session, f"{self.BASE}/explore", {"hl": self.hl, "tz": 0, "req": json.dumps(req)},
                     backoff=30.0)
        )
        widget = next((w for w in explore.get("widgets", []) if w.get("id") == "TIMESERIES"), None)
        if not widget:
            raise TrendsError("Google Trends: không có widget TIMESERIES")
        time.sleep(self.sleep_seconds)
        data = self._strip(
            get_text(
                self.session,
                f"{self.BASE}/widgetdata/multiline",
                {"hl": self.hl, "tz": 0, "req": json.dumps(widget["request"]), "token": widget["token"]},
                backoff=30.0,
            )
        )
        out: dict[str, Series] = {k: [] for k in keywords}
        for point in data.get("default", {}).get("timelineData", []):
            d = _ts_to_date(point["time"])
            for k, v in zip(keywords, point.get("value", [])):
                out[k].append((d, float(v)))
        time.sleep(self.sleep_seconds)
        return out


# ---------------------------------------------------------------- CSV tải tay
class CsvTrends:
    """Đọc CSV xuất từ trends.google.com.

    Đặt file vào `<csv_dir>/<GEO>/*.csv` (vd data/trends_csv/US/multiTimeline.csv).
    Cột được khớp theo tên keyword (phần trước dấu ':' trong header).
    """

    name = "csv"

    def __init__(self, csv_dir: str | Path):
        self.csv_dir = Path(csv_dir)

    @staticmethod
    def parse(text: str) -> dict[str, Series]:
        rows = list(csv.reader(io.StringIO(text)))
        header_i = next(
            (i for i, r in enumerate(rows) if r and r[0].strip().lower() in {"week", "tuần", "month", "day", "time"}),
            None,
        )
        if header_i is None:
            return {}
        header = rows[header_i]
        names = [h.split(":")[0].strip().lower() for h in header[1:]]
        out: dict[str, Series] = {n: [] for n in names}
        for r in rows[header_i + 1 :]:
            if len(r) < 2 or not r[0].strip():
                continue
            d = date.fromisoformat(r[0].strip()[:10])
            for n, v in zip(names, r[1:]):
                v = v.strip()
                out[n].append((d, 0.0 if v.startswith("<") else float(v or 0)))
        return out

    def interest_over_time(self, keywords: list[str], geo: str, timeframe: str) -> dict[str, Series]:
        found: dict[str, Series] = {}
        for f in sorted((self.csv_dir / (geo or "WW")).glob("*.csv")):
            for name, series in self.parse(f.read_text(encoding="utf-8-sig")).items():
                found.setdefault(name, series)
        return {k: found[k.lower()] for k in keywords if k.lower() in found}


# ---------------------------------------------------------------- Ghép nhiều request bằng keyword neo
def _mean(s: Series) -> float:
    return sum(v for _, v in s) / len(s) if s else 0.0


def fetch_keyword_set(provider: TrendsProvider, keywords: list[str], geo: str, timeframe: str) -> dict[str, Series]:
    """Lấy chuỗi cho cả bộ keyword trên CÙNG một thang đo.

    keyword[0] là neo: có mặt trong mọi request; các request sau được nhân hệ số
    (mean neo ở request đầu / mean neo ở request này).
    """
    if not keywords:
        return {}
    anchor, others = keywords[0], keywords[1:]
    batches = [others[i : i + MAX_PER_REQUEST - 1] for i in range(0, len(others), MAX_PER_REQUEST - 1)] or [[]]
    result: dict[str, Series] = {}
    ref: float | None = None
    for batch in batches:
        data = provider.interest_over_time([anchor, *batch], geo, timeframe)
        a = data.get(anchor) or []
        a_mean = _mean(a)
        if ref is None:
            ref = a_mean
            if a:
                result[anchor] = a
        scale = ref / a_mean if ref and a_mean else 1.0
        for k in batch:
            if data.get(k):
                result[k] = [(d, v * scale) for d, v in data[k]]
    return result


# ---------------------------------------------------------------- Mở rộng keyword
def autocomplete(session: requests.Session, query: str, hl: str = "en", gl: str = "us") -> list[str]:
    data = get_json(
        session,
        "https://suggestqueries.google.com/complete/search",
        {"client": "firefox", "hl": hl, "gl": gl, "q": query},
        retries=2,
    )
    return [s for s in (data[1] if isinstance(data, list) and len(data) > 1 else []) if isinstance(s, str)]


def expand_keywords(
    session: requests.Session,
    seeds: list[str],
    *,
    max_keywords: int = 12,
    alphabet: bool = False,
    hl: str = "en",
    gl: str = "us",
    sleep_seconds: float = 0.5,
) -> list[str]:
    """Seed + gợi ý Autocomplete (tuỳ chọn thêm 'seed a', 'seed b', ...). Giữ thứ tự, bỏ trùng."""
    out: list[str] = []

    def add(k: str) -> None:
        k = " ".join(k.lower().split())
        if k and k not in out and len(out) < max_keywords:
            out.append(k)

    for s in seeds:
        add(s)
    for s in seeds:
        queries = [s] + ([f"{s} {c}" for c in "abcdefghijklmnopqrstuvwxyz"] if alphabet else [])
        for q in queries:
            if len(out) >= max_keywords:
                return out
            try:
                for sug in autocomplete(session, q, hl, gl):
                    add(sug)
            except Exception as exc:
                log.warning("Autocomplete '%s': %s", q, exc)
            time.sleep(sleep_seconds)
    return out
