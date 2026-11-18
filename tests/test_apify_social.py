from datetime import date, timedelta
from pathlib import Path

import pytest

from winfinder import db
from winfinder.config import load_config
from winfinder.sources.apify import ApifyBudget
from winfinder.sources.apify_social import (
    TikTokSource, find_instagram_handle, instagram_brand, is_english, lookup_handle,
)
from winfinder.sources.social import make_sources

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 18)
SINCE = TODAY - timedelta(days=365)


@pytest.mark.parametrize("text,ok", [
    ("Does it hurt tho", True),
    ("Is there a office chair that the average person can afford?", True),
    ("sakit sa kili kili nyan", False),            # Tagalog (gặp trong dữ liệu thật)
    ("ano meaning nung max", False),
    ("Это самое лучшее чувство!", False),
    ("Lo unico que pudo solucionar Mis Problemas", False),
    ("", False),
])
def test_is_english(text, ok):
    assert is_english(text) is ok


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = replies, []
        self.budget = ApifyBudget(3)

    def run(self, actor, run_input, max_cost=None, label=""):
        self.calls.append((actor, run_input))
        return self.replies[actor]


def video(i, text, lang="en", ad=False, comments=10, days=20):
    return {"id": str(i), "text": text, "textLanguage": lang, "isAd": ad, "isSponsored": False,
            "createTimeISO": (TODAY - timedelta(days=days)).isoformat() + "T00:00:00Z", "diggCount": 100 * i,
            "commentCount": comments, "webVideoUrl": f"https://tiktok.com/@a/video/{i}", "authorMeta": {"name": "a"}}


def test_tiktok_filters_ads_language_age_and_reads_comments():
    cfg = load_config(ROOT / "config.yaml").apify
    cfg.tiktok_comment_videos = 1
    client = FakeClient({
        "clockworks/tiktok-scraper": [
            video(1, "My desk job is killing my back", comments=50),
            video(2, "Buy now 50% off", ad=True),
            video(3, "Lo unico que pudo solucionar mi espalda", lang="es"),
            video(4, "old video about back pain", days=400),
        ],
        "clockworks/tiktok-comments-scraper": [
            {"cid": "c1", "text": "Same, my lower back hurts after work", "createTimeISO": "2026-11-01T00:00:00Z",
             "diggCount": 5, "videoWebUrl": "https://tiktok.com/@a/video/1", "uniqueId": "u1"},
            {"cid": "c2", "text": "sakit sa kili kili nyan", "createTimeISO": "2026-11-01T00:00:00Z"},
            {"cid": "c3", "text": "how much", "createTimeISO": "2026-11-01T00:00:00Z"},   # quá ngắn
        ],
    })
    posts = TikTokSource(client, cfg).search("desk job back pain", SINCE)
    assert [(p.source, p.id) for p in posts] == [("tiktok", "1"), ("tiktok_comment", "c1")]
    inp = client.calls[0][1]
    assert inp["proxyCountryCode"] == "US" and inp["searchQueries"] == ["desk job back pain"]
    assert client.calls[1][1]["postURLs"] == ["https://tiktok.com/@a/video/1"]


def test_find_instagram_handle_from_store_html():
    html = ('<footer><a href="https://www.instagram.com/axzenia_official/">IG</a>'
            '<a href="https://instagram.com/p/Cx123/">post</a>'
            '<a href="https://www.instagram.com/axzenia_official">again</a></footer>')
    assert find_instagram_handle(html) == "axzenia_official"
    assert find_instagram_handle("<p>no social</p>") is None


def test_lookup_handle_is_cached(tmp_path):
    conn = db.connect(tmp_path / "w.db")

    class S:
        calls = 0

        def get(self, url, params=None, timeout=None):
            S.calls += 1
            return type("R", (), {"status_code": 200, "text": '<a href="https://instagram.com/brand">x</a>'})()

    assert lookup_handle(conn, S(), "brand.com", TODAY) == "brand"
    assert lookup_handle(conn, S(), "brand.com", TODAY + timedelta(days=3)) == "brand"
    assert S.calls == 1


def test_instagram_brand_metrics():
    posts = [{"timestamp": (TODAY - timedelta(days=d)).isoformat() + "T00:00:00Z", "likesCount": likes,
              "commentsCount": 0, "url": f"https://instagram.com/p/{d}", "paidPartnership": d == 5,
              "latestComments": [{"text": "I need this for my back pain", "timestamp": "2026-11-10T00:00:00Z"},
                                 {"text": "😍😍"}]}
             for d, likes in ((5, 400), (10, 300), (40, 100), (60, 100))]
    ig = instagram_brand(FakeClient({"apify/instagram-scraper": posts}), "brand", TODAY, 4)
    assert (ig.posts, ig.posts_30d, ig.paid_partnerships) == (4, 2, 1)
    assert ig.engagement_ratio == pytest.approx(351 / 101, abs=0.01)   # bài mới tương tác cao hơn hẳn
    assert ig.comments and all("back pain" in c["text"] for c in ig.comments)


def test_make_sources_tiktok_needs_apify():
    cfg = load_config(ROOT / "config.yaml").apify
    sources, skipped = make_sources(["tiktok", "instagram"], {})
    assert not sources and any("APIFY_TOKEN" in x for x in skipped) and any("hashtag" in x or "chặn" in x
                                                                             for x in skipped)
    sources, _ = make_sources(["tiktok"], {}, FakeClient({}), cfg)
    assert [s.name for s in sources] == ["tiktok"]
