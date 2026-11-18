"""Organic TikTok + Instagram qua Apify.

TikTok (clockworks/tiktok-scraper + tiktok-comments-scraper):
  - Tìm video theo cụm từ persona hay viết, quét như người dùng ở US, chỉ giữ tiếng Anh, BỎ video là ad/tài trợ.
  - Bình luận của các video nhiều bình luận nhất = lời thật của khách (lọc tiếng Anh).
Instagram (apify/instagram-scraper):
  - Tìm theo hashtag bị Instagram chặn (cần đăng nhập) → chỉ theo dõi PROFILE brand của store đối thủ:
    nhịp đăng, tương tác, và bình luận trên bài của họ.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import requests

from winfinder.config import ApifyConfig
from winfinder.http import HttpError, get_text
from winfinder.sources.apify import ApifyClient
from winfinder.sources.social import SocialPost

log = logging.getLogger(__name__)

TIKTOK = "clockworks/tiktok-scraper"
TIKTOK_COMMENTS = "clockworks/tiktok-comments-scraper"
INSTAGRAM = "apify/instagram-scraper"

EN_WORDS = set("""the a an and or but i i'm im my me it it's its is was are be been this that to of in on for with you your
so just not no do does did can how what why when who have has had would will about if they them their got get really
like love hate hurt help need want back pain feel after before tried works work""".split())


def is_english(text: str) -> bool:
    """Heuristic đủ dùng cho bình luận ngắn: chủ yếu chữ Latin không dấu + có ít nhất 1 từ tiếng Anh phổ biến."""
    t = text.strip().lower()
    if not t:
        return False
    letters = [c for c in t if c.isalpha()]
    if letters and sum(1 for c in letters if c.isascii()) / len(letters) < 0.9:
        return False
    words = set(re.findall(r"[a-z']+", t))
    return bool(words & EN_WORDS)


def _day(s: str | None) -> str:
    return (s or "")[:10]


class TikTokSource:
    name = "tiktok"

    def __init__(self, client: ApifyClient, cfg: ApifyConfig):
        self.client, self.cfg = client, cfg

    def search(self, query: str, since: date, limit: int = 100) -> list[SocialPost]:
        c = self.cfg
        items = self.client.run(TIKTOK, {
            "searchQueries": [query], "resultsPerPage": c.tiktok_results_per_query, "searchSection": "/video",
            "proxyCountryCode": c.tiktok_country}, label=f"tiktok '{query}'")
        videos = []
        for x in items:
            if x.get("isAd") or x.get("isSponsored"):
                continue  # organic thôi — ad TikTok không phải tiếng nói của khách
            if _day(x.get("createTimeISO")) < since.isoformat():
                continue
            lang = (x.get("textLanguage") or "").lower()
            if c.english_only and lang and lang not in ("en", "un") and not is_english(x.get("text") or ""):
                continue
            a = x.get("authorMeta") or {}
            videos.append(SocialPost(
                "tiktok", str(x.get("id")), "", (x.get("text") or "")[:600], x.get("webVideoUrl") or "",
                _day(x.get("createTimeISO")), int(x.get("diggCount") or 0), int(x.get("commentCount") or 0),
                a.get("name") or ""))
        out = list(videos)
        top = [v for v in sorted(videos, key=lambda v: -v.comments) if v.comments][: c.tiktok_comment_videos]
        if top:
            comments = self.client.run(TIKTOK_COMMENTS, {
                "postURLs": [v.url for v in top], "commentsPerPost": c.tiktok_comments_per_video},
                label=f"tiktok comments '{query}'")
            for cm in comments:
                text = (cm.get("text") or "").strip()
                # < 3 từ ("how much?", "I like this") không mang ngôn ngữ về nỗi đau / mong muốn
                if _day(cm.get("createTimeISO")) < since.isoformat() or len(text.split()) < 3:
                    continue
                if c.english_only and not is_english(text):
                    continue
                out.append(SocialPost(
                    "tiktok_comment", str(cm.get("cid")), "", text[:500], cm.get("videoWebUrl") or "",
                    _day(cm.get("createTimeISO")), int(cm.get("diggCount") or 0), int(cm.get("replyCommentTotal") or 0),
                    cm.get("uniqueId") or ""))
        return out[: limit * 2]


# ------------------------------------------------------------------ Instagram theo profile brand
IG_SKIP = {"p", "reel", "reels", "explore", "accounts", "share", "stories", "tv", "direct", "about", "developer"}


def find_instagram_handle(raw_html: str) -> str | None:
    """Link instagram.com/<handle> trong trang store (thường ở footer)."""
    counts: dict[str, int] = {}
    for h in re.findall(r"instagram\.com/([A-Za-z0-9_.]{2,30})", raw_html or ""):
        h = h.strip(".").lower()
        if h and h not in IG_SKIP:
            counts[h] = counts.get(h, 0) + 1
    return max(counts, key=counts.get) if counts else None


def lookup_handle(conn: sqlite3.Connection, session: requests.Session, domain: str, today: date,
                  refresh_days: int = 30) -> str | None:
    row = conn.execute("SELECT handle, checked_on FROM brand_profiles WHERE domain = ? AND platform = 'instagram'",
                       (domain,)).fetchone()
    if row and (today - date.fromisoformat(row["checked_on"])).days < refresh_days:
        return row["handle"]
    handle = None
    try:
        handle = find_instagram_handle(get_text(session, f"https://{domain}/", retries=1, timeout=20))
    except (HttpError, requests.RequestException) as exc:
        log.info("Không đọc được %s để tìm Instagram: %s", domain, exc)
    conn.execute("INSERT OR REPLACE INTO brand_profiles VALUES (?,?,?,?)",
                 (domain, "instagram", handle, today.isoformat()))
    conn.commit()
    return handle


@dataclass
class InstagramBrand:
    handle: str
    posts: int
    posts_30d: int
    median_likes: float | None
    engagement_ratio: float | None     # tương tác TB nửa mới / nửa cũ của các bài gần nhất
    paid_partnerships: int
    comments: list[dict] = field(default_factory=list)   # bình luận mới nhất (lời khách)

    def to_dict(self) -> dict:
        return asdict(self)


def instagram_brand(client: ApifyClient, handle: str, today: date, n_posts: int = 12) -> InstagramBrand | None:
    items = client.run(INSTAGRAM, {"directUrls": [f"https://www.instagram.com/{handle}/"], "resultsType": "posts",
                                   "resultsLimit": n_posts}, label=f"instagram @{handle}")
    posts = sorted((x for x in items if x.get("timestamp")), key=lambda x: x["timestamp"], reverse=True)
    if not posts:
        return None

    def eng(x):
        return int(x.get("likesCount") or 0) + int(x.get("commentsCount") or 0)

    half = len(posts) // 2
    new, old = [eng(x) for x in posts[:half]], [eng(x) for x in posts[half:]]
    ratio = ((statistics.mean(new) + 1) / (statistics.mean(old) + 1)) if new and old else None
    cutoff = (today - timedelta(days=30)).isoformat()
    comments = []
    for x in posts:
        for c in x.get("latestComments") or []:
            text = (c.get("text") or "").strip()
            if len(text.split()) >= 3 and is_english(text):
                comments.append({"text": text[:400], "date": _day(c.get("timestamp")), "url": x.get("url") or "",
                                 "likes": int(c.get("likesCount") or 0)})
    likes = [int(x.get("likesCount") or 0) for x in posts]
    return InstagramBrand(handle, len(posts), sum(1 for x in posts if _day(x["timestamp"]) >= cutoff),
                          statistics.median(likes) if likes else None,
                          round(ratio, 2) if ratio is not None else None,
                          sum(1 for x in posts if x.get("paidPartnership")), comments[:40])
