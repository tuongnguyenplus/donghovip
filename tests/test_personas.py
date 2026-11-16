import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from winfinder import db, demo
from winfinder.analysis.angles import parse_result, AdUnit
from winfinder.analysis.social import (
    SCHEMA, ClaudeVerifier, dedupe, heuristic_verdict, parse_verification, sample, social_score, stats,
)
from winfinder.config import load_config
from winfinder.personas_pipeline import run_personas, select_angles
from winfinder.sources.social import RedditSource, SocialError, SocialPost, YouTubeSource, make_sources

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 16)


def post(i, days_ago, text="x", source="reddit", score=1, comments=0, community="c"):
    return SocialPost(source, str(i), text, text, f"https://x/{i}", (AS_OF - timedelta(days=days_ago)).isoformat(),
                      score, comments, community)


# ------------------------------------------------------------------ angle: keyword nhu cầu + cụm mạng xã hội
def test_angle_result_keeps_persona_queries_and_old_cache_still_parses():
    units = [AdUnit("a1", "x", ["1"], ["s"], None, 0)]
    a = {"id": "A1", "name": "n", "persona": "p", "pain_point": "pp", "promise": "pr", "hook_type": "gift",
         "pain_keywords": ["Back Pain From Sitting", "back pain from sitting", ""], "social_queries": ["desk job"]}
    r = parse_result({"angles": [a], "assignments": [], "untapped_angles": []}, units)
    assert r.angles[0].pain_keywords == ["back pain from sitting"] and r.angles[0].social_queries == ["desk job"]
    old = {k: v for k, v in a.items() if k not in ("pain_keywords", "social_queries")}  # kết quả cache cũ
    assert parse_result({"angles": [old], "assignments": [], "untapped_angles": []}, units).angles[0].pain_keywords == []


# ------------------------------------------------------------------ thống kê + xác minh
def test_stats_windows_growth_and_match():
    posts = [post(i, 10, "my back pain from sitting") for i in range(6)] + [post(10 + i, 120) for i in range(2)]
    st = stats(posts, ["back pain from sitting"], AS_OF)
    assert (st.volume_90, st.volume_prev_90) == (6, 2) and st.growth == pytest.approx(7 / 3, abs=0.01)
    assert st.match_share == 0.75 and st.by_source == {"reddit": 8}


def test_dedupe_and_sample_mix_sources():
    posts = [post(1, 1), post(1, 1), post(2, 1, source="youtube_comment"), post(3, 1, score=500)]
    assert len(dedupe(posts)) == 3
    s = sample(dedupe(posts), 2)
    assert {p.source for p in s} == {"reddit", "youtube_comment"}


def test_heuristic_never_claims_confirmed():
    st = stats([post(i, 5, "back pain") for i in range(40)], ["back pain"], AS_OF)
    v = heuristic_verdict(st)
    assert v.verdict == "partial" and v.method == "heuristic"
    assert heuristic_verdict(stats([], [], AS_OF)).verdict == "not_found"


def test_parse_verification_drops_invented_quotes():
    posts = [post(1, 3, "I have terrible eye bags after night shifts"), post(2, 3, "unrelated")]
    v = parse_verification({
        "verdict": "confirmed", "confidence": "high", "relevant_refs": ["p1", "p9"],
        "evidence": [{"ref": "p1", "quote": "terrible eye bags after night shifts", "why": "ok"},
                     {"ref": "p2", "quote": "this was never written", "why": "bịa"},
                     {"ref": "p7", "quote": "x", "why": "không có bài"}],
        "customer_phrases": ["eye bags"], "persona_notes": "n", "risks": "",
    }, posts, "m")
    assert [e["quote"] for e in v.evidence] == ["terrible eye bags after night shifts"]
    assert v.relevant_share == 0.5 and v.method == "claude"


def test_social_score_rewards_verified_growing_discussion():
    st = stats([post(i, 5, "pain") for i in range(50)] + [post(100 + i, 120, "pain") for i in range(10)], ["pain"],
               AS_OF)
    good = parse_verification({"verdict": "confirmed", "confidence": "high", "relevant_refs": ["p1"],
                               "evidence": [], "customer_phrases": [], "persona_notes": "", "risks": ""},
                              [post(1, 1)], "")
    bad = parse_verification({**{"verdict": "not_found", "confidence": "high", "relevant_refs": [],
                                 "evidence": [], "customer_phrases": [], "persona_notes": "", "risks": ""}},
                             [post(1, 1)], "")
    assert social_score(st, good) > social_score(st, bad)
    assert social_score(None, good) is None


def test_claude_verifier_request():
    payload = {"verdict": "partial", "confidence": "medium", "relevant_refs": ["p1"], "evidence": [],
               "customer_phrases": ["eye bags"], "persona_notes": "n", "risks": "r"}
    captured = {}

    def create(**kw):
        captured.update(kw)
        return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5",
                               content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    v = ClaudeVerifier(client=client).verify("red light glasses", {"name": "n", "persona": "nurse",
                                                                   "pain_point": "eye bags", "promise": "p"},
                                             [post(1, 2, "night shift eye bags")])
    assert v.verdict == "partial" and v.customer_phrases == ["eye bags"]
    assert captured["output_config"]["format"]["schema"] == SCHEMA and captured["output_config"]["effort"] == "medium"
    assert captured["fallbacks"] == "default" and "p1: [reddit" in captured["messages"][0]["content"]


# ------------------------------------------------------------------ nguồn mạng xã hội (HTTP giả)
class Resp:
    def __init__(self, payload, status=200):
        self.status_code, self._p, self.text = status, payload, json.dumps(payload)

    def json(self):
        return self._p


class Sess:
    def __init__(self, gets, posts=None):
        self.gets, self.posts, self.headers, self.calls = list(gets), list(posts or []), {}, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.gets.pop(0)

    def post(self, url, auth=None, data=None, timeout=None):
        self.calls.append((url, data))
        return self.posts.pop(0)


def test_reddit_source_oauth_and_parsing():
    recent = (AS_OF - timedelta(days=3)).toordinal()
    import datetime as dt
    ts = dt.datetime.combine(dt.date.fromordinal(recent), dt.time(), tzinfo=dt.timezone.utc).timestamp()
    old_ts = ts - 400 * 86400
    sess = Sess(gets=[Resp({"data": {"after": None, "children": [
        {"data": {"id": "a", "title": "Desk job back pain", "selftext": "help", "permalink": "/r/x/a",
                  "created_utc": ts, "score": 10, "num_comments": 4, "subreddit": "Posture"}},
        {"data": {"id": "b", "title": "old", "created_utc": old_ts}}]}})],
        posts=[Resp({"access_token": "T", "expires_in": 3600})])
    src = RedditSource("id", "secret", session=sess, sleep_seconds=0)
    out = src.search("desk job back pain", AS_OF - timedelta(days=365))
    assert [p.id for p in out] == ["a"] and out[0].community == "Posture" and out[0].url.endswith("/r/x/a")
    assert sess.headers["Authorization"] == "Bearer T"
    assert sess.calls[0][1] == {"grant_type": "client_credentials"}


def test_youtube_source_videos_and_comments():
    sess = Sess(gets=[
        Resp({"items": [{"id": {"videoId": "v1"}, "snippet": {"title": "Eye bags fix", "description": "d",
                                                                 "publishedAt": "2026-10-01T00:00:00Z",
                                                                 "channelTitle": "Ch"}}]}),
        Resp({"items": [{"id": "v1", "statistics": {"viewCount": "1000", "commentCount": "12"}}]}),
        Resp({"items": [{"snippet": {"topLevelComment": {"id": "c1", "snippet": {
            "textOriginal": "night shift gave me these bags", "likeCount": 7,
            "publishedAt": "2026-10-05T00:00:00Z"}}}}]}),
    ])
    out = YouTubeSource("key", session=sess).search("eye bags", AS_OF - timedelta(days=365))
    assert [(p.source, p.id) for p in out] == [("youtube", "v1"), ("youtube_comment", "c1")]
    assert out[0].score == 1000 and out[1].text.startswith("night shift")


def test_make_sources_reports_missing_keys_and_unsupported():
    sources, skipped = make_sources(["reddit", "youtube", "tiktok"], {"YOUTUBE_API_KEY": "k"})
    assert [s.name for s in sources] == ["youtube"]
    assert any("REDDIT" in x for x in skipped) and any("tiktok" in x for x in skipped)
    with pytest.raises(SocialError):
        RedditSource("", "")


# ------------------------------------------------------------------ pipeline trên demo
def _angles_doc():
    return {"products": [{"product": "posture corrector", "generic": "posture corrector", "angles": [
        {"status": "rising", "recent_share": 0.5, "angle": {
            "id": "A1", "name": "Quà tặng cho mẹ", "persona": "con cái", "pain_point": "p", "promise": "q",
            "hook_type": "gift", "pain_keywords": ["gift for mom"], "social_queries": ["gift ideas for my mom"]}},
        {"status": "steady", "recent_share": 0.3, "angle": {
            "id": "A2", "name": "Tự tin", "persona": "x", "pain_point": "p", "promise": "q", "hook_type": "other",
            "pain_keywords": ["how to fix posture"], "social_queries": ["my posture looks bad in photos"]}},
        {"status": "rising", "recent_share": 0.2, "angle": {
            "id": "A3", "name": "Ưu đãi", "persona": "x", "pain_point": "p", "promise": "q", "hook_type": "offer",
            "pain_keywords": [], "social_queries": []}},
    ]}]}


def test_select_angles_skips_offer_angles_and_ranks_rising_first():
    cfg = load_config(ROOT / "config.yaml")
    assert [a["angle"]["id"] for _, _, a in select_angles(_angles_doc(), cfg)] == ["A1", "A2"]


def test_run_personas_end_to_end_and_cache():
    cfg = load_config(ROOT / "config.yaml")
    cfg.trends.geos = ["US"]
    conn = db.connect(":memory:")
    src, ver = demo.DemoSocialSource(AS_OF), demo.DemoVerifier()
    res = run_personas(conn, cfg, _angles_doc(), AS_OF, provider=demo.DemoTrendsProvider(AS_OF),
                       sources=[src], verifier=ver)
    by = {r.angle: r for r in res}
    gift, conf = by["Quà tặng cho mẹ"], by["Tự tin"]
    assert gift.verification["verdict"] == "confirmed" and gift.verification["evidence"]
    assert gift.need["label"] == "seasonal" and gift.social["growth"] > 1
    assert conf.verification["verdict"] == "not_found"        # có bài nhưng toàn quảng cáo/review
    assert gift.social_score > conf.social_score
    calls_src, calls_ver = src.calls, ver.calls
    run_personas(conn, cfg, _angles_doc(), AS_OF, sources=[src], verifier=ver)
    assert (src.calls, ver.calls) == (calls_src, calls_ver)   # cache: không tìm/không xác minh lại


def test_run_personas_without_sources_marks_unverified():
    cfg = load_config(ROOT / "config.yaml")
    res = run_personas(db.connect(":memory:"), cfg, _angles_doc(), AS_OF, sources=[],
                       skipped_sources=["reddit: Thiếu REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET"])
    assert all(r.verification["verdict"] == "unverified" for r in res)
    assert any("REDDIT" in n for n in res[0].notes)


def test_run_personas_without_claude_uses_heuristic():
    cfg = load_config(ROOT / "config.yaml")
    res = run_personas(db.connect(":memory:"), cfg, _angles_doc(), AS_OF, sources=[demo.DemoSocialSource(AS_OF)])
    assert all(r.verification["method"] == "heuristic" for r in res)
    assert all(r.verification["verdict"] != "confirmed" for r in res)
