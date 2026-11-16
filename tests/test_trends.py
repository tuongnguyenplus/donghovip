import json
from pathlib import Path
from datetime import date, timedelta

import pytest

from winfinder import db
from winfinder.analysis.trends import analyze_series, sum_series, trend_score
from winfinder.config import load_config
from winfinder.demo import DemoTrendsProvider, _shape
from winfinder.sources.trends import (
    CsvTrends,
    DirectTrends,
    SerpApiTrends,
    expand_keywords,
    fetch_keyword_set,
)
from winfinder.trends_pipeline import analyze_topic_geo, fetch_topic_geo, spread_summary

AS_OF = date(2026, 11, 16)


def _series(kind: str, weeks: int = 260, seed: int = 1):
    import random

    rng = random.Random(seed)
    return [(AS_OF - timedelta(weeks=weeks - 1 - i), _shape(kind, i, weeks, rng, AS_OF)) for i in range(weeks)]


@pytest.mark.parametrize(
    "kind,label",
    [
        ("rising", "rising"),
        ("stable", "stable"),
        ("seasonal", "seasonal"),
        ("breakout", "breakout"),
        ("spike", "spike"),
        ("declining", "declining"),
    ],
)
def test_labels(kind, label):
    assert analyze_series(_series(kind)).label == label


def test_seasonal_detects_winter_peak():
    st = analyze_series(_series("seasonal"))
    assert st.seasonality > 0.9 and st.seasonal_amplitude > 1
    assert st.peak_week in (*range(48, 53), *range(1, 5))


def test_small_cyclic_wobble_is_not_seasonal():
    st = analyze_series(_series("stable"))
    assert st.seasonal_amplitude < 0.4 and st.label == "stable"


def test_insufficient_history():
    assert analyze_series(_series("rising", weeks=30)).label == "insufficient"


def test_scores_rank_rising_above_spike_and_declining():
    s = {k: trend_score(analyze_series(_series(k))) for k in ("rising", "spike", "declining", "stable")}
    assert s["rising"] > s["stable"] > s["declining"]
    assert s["rising"] > s["spike"]


def test_sum_series_keeps_common_dates():
    a = [(date(2026, 1, 1), 1.0), (date(2026, 1, 8), 2.0)]
    b = [(date(2026, 1, 8), 5.0)]
    assert sum_series([a, b]) == [(date(2026, 1, 8), 7.0)]


class FakeProvider:
    """Mỗi request chuẩn hoá lại 0–100 như Google Trends thật."""

    name = "fake"

    def __init__(self, truth):
        self.truth = truth
        self.calls = []

    def interest_over_time(self, keywords, geo, timeframe):
        self.calls.append(list(keywords))
        top = max(v for k in keywords for _, v in self.truth[k])
        return {k: [(d, v * 100 / top) for d, v in self.truth[k]] for k in keywords}


def test_anchor_rescaling_restores_common_scale():
    d = [date(2026, 1, 1) + timedelta(weeks=i) for i in range(3)]
    truth = {k: [(x, float(m * (i + 1))) for i, x in enumerate(d)]
             for k, m in {"a": 10, "b": 5, "c": 1, "d": 2, "e": 3, "f": 40}.items()}
    p = FakeProvider(truth)
    out = fetch_keyword_set(p, ["a", "b", "c", "d", "e", "f"], "US", "today 5-y")
    assert p.calls == [["a", "b", "c", "d", "e"], ["a", "f"]]
    ratio = out["f"][0][1] / out["a"][0][1]
    assert ratio == pytest.approx(4.0)


class FakeResp:
    def __init__(self, payload=None, text=None, status=200):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload if self._payload is not None else json.loads(self.text)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


def test_serpapi_parsing():
    payload = {
        "interest_over_time": {
            "timeline_data": [
                {"timestamp": "1767225600", "values": [{"query": "a", "extracted_value": 40},
                                                       {"query": "b", "extracted_value": 0}]},
            ]
        }
    }
    s = SerpApiTrends("key", session=FakeSession([FakeResp(payload)]), sleep_seconds=0)
    out = s.interest_over_time(["a", "b"], "US", "today 5-y")
    assert out["a"] == [(date(2026, 1, 1), 40.0)] and out["b"][0][1] == 0.0


def test_direct_parsing_strips_prefix():
    explore = ")]}'\n" + json.dumps({"widgets": [{"id": "TIMESERIES", "request": {"x": 1}, "token": "T"}]})
    multiline = ")]}',\n" + json.dumps({"default": {"timelineData": [{"time": "1767225600", "value": [12, 3]}]}})
    sess = FakeSession([FakeResp(text=""), FakeResp(text=explore), FakeResp(text=multiline)])
    out = DirectTrends(session=sess, sleep_seconds=0).interest_over_time(["a", "b"], "US", "today 5-y")
    assert out == {"a": [(date(2026, 1, 1), 12.0)], "b": [(date(2026, 1, 1), 3.0)]}
    assert sess.calls[2][1]["token"] == "T"


def test_csv_parse_google_export(tmp_path):
    text = "Category: All categories\n\nWeek,posture corrector: (United States),posture brace: (United States)\n" \
           "2026-01-04,45,<1\n2026-01-11,50,3\n"
    (tmp_path / "US").mkdir()
    (tmp_path / "US" / "multiTimeline.csv").write_text(text)
    out = CsvTrends(tmp_path).interest_over_time(["Posture Corrector", "posture brace", "missing"], "US", "x")
    assert out["Posture Corrector"][1] == (date(2026, 1, 11), 50.0)
    assert out["posture brace"][0][1] == 0.0
    assert "missing" not in out


def test_expand_keywords_dedupes_and_caps():
    sess = FakeSession([FakeResp(["posture corrector", ["posture corrector", "Posture Corrector for Women",
                                                         "posture corrector amazon"]])])
    out = expand_keywords(sess, ["Posture Corrector"], max_keywords=3, sleep_seconds=0)
    assert out == ["posture corrector", "posture corrector for women", "posture corrector amazon"]


def test_fetch_uses_cache(tmp_path):
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    conn = db.connect(":memory:")
    p = DemoTrendsProvider(AS_OF)
    calls = []
    orig = p.interest_over_time
    p.interest_over_time = lambda *a: (calls.append(a), orig(*a))[1]
    fetch_topic_geo(conn, cfg, p, "t", ["posture corrector"], "US", AS_OF)
    fetch_topic_geo(conn, cfg, p, "t", ["posture corrector"], "US", AS_OF + timedelta(days=2))
    assert len(calls) == 1
    fetch_topic_geo(conn, cfg, p, "t", ["posture corrector"], "US", AS_OF + timedelta(days=10))
    assert len(calls) == 2


def test_topic_volume_estimate_and_spread():
    p = DemoTrendsProvider(AS_OF)
    kws = ["posture corrector", "posture brace"]
    us = analyze_topic_geo("US", p.interest_over_time(kws, "US", ""), kws, 10000, "posture corrector")
    de = analyze_topic_geo("DE", p.interest_over_time(kws, "DE", ""), kws)
    vols = {k.keyword: k.est_monthly_volume for k in us.keywords}
    assert vols["posture corrector"] == 10000
    assert 3000 < vols["posture brace"] < 6000
    sp = spread_summary([us, de])
    assert sp["home_rising"] and sp["rising_elsewhere"] == []
