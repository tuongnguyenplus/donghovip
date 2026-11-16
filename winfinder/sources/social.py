"""Nguồn mạng xã hội để xác minh angle/persona: người thật có nói về nỗi đau này không, bằng lời nào.

  - Reddit: API chính thức (OAuth app-only, cần REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET — tạo app loại "script"
    tại reddit.com/prefs/apps). Bài viết + số upvote/bình luận + subreddit.
  - YouTube: Data API v3 chính thức (YOUTUBE_API_KEY). Video + BÌNH LUẬN (nơi người dùng nói bằng lời thật).
  - TikTok / Instagram / Facebook: không có API tìm kiếm công khai cho mục đích này (TikTok Research API chỉ cho
    học thuật; Instagram chỉ tìm theo hashtag, cần tài khoản business + Meta duyệt app; Facebook group không có
    API tìm kiếm). Nối qua dịch vụ bên thứ ba bằng cùng giao diện `SocialSource` (khung connector).
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

import requests

from winfinder.http import HttpError, get_json, make_session

log = logging.getLogger(__name__)


@dataclass
class SocialPost:
    source: str                  # reddit | youtube | youtube_comment | ...
    id: str
    title: str
    text: str
    url: str
    created: str                 # YYYY-MM-DD
    score: int = 0               # upvote / like / view
    comments: int = 0
    community: str = ""          # subreddit / kênh

    def to_dict(self) -> dict:
        return asdict(self)


class SocialSource(Protocol):
    name: str

    def search(self, query: str, since: date, limit: int = 100) -> list[SocialPost]: ...


class SocialError(RuntimeError):
    pass


def _day(ts: float | str) -> str:
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    return str(ts)[:10]


# ------------------------------------------------------------------ Reddit
class RedditSource:
    name = "reddit"
    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
    API = "https://oauth.reddit.com"

    def __init__(self, client_id: str, client_secret: str, session: requests.Session | None = None,
                 sleep_seconds: float = 1.0, user_agent: str = "winfinder/0.1 (market research)"):
        if not client_id or not client_secret:
            raise SocialError("Thiếu REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET")
        self.auth = (client_id, client_secret)
        self.session = session or make_session()
        self.session.headers["User-Agent"] = user_agent
        self.sleep_seconds = sleep_seconds
        self._token: str | None = None
        self._expires = 0.0

    def _auth_header(self) -> dict:
        if not self._token or time.time() > self._expires - 60:
            r = self.session.post(self.TOKEN_URL, auth=self.auth, data={"grant_type": "client_credentials"},
                                  timeout=30)
            if r.status_code >= 400:
                raise SocialError(f"Reddit OAuth lỗi {r.status_code}: {r.text[:200]}")
            data = r.json()
            self._token, self._expires = data["access_token"], time.time() + float(data.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    def search(self, query: str, since: date, limit: int = 100) -> list[SocialPost]:
        out: list[SocialPost] = []
        after = None
        cutoff = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        for _ in range(3):  # tối đa 3 trang × 100 bài
            self.session.headers.update(self._auth_header())
            params = {"q": query, "sort": "relevance", "t": "year", "limit": 100, "type": "link", "raw_json": 1}
            if after:
                params["after"] = after
            data = get_json(self.session, f"{self.API}/search", params, retries=2)
            kids = (data.get("data") or {}).get("children") or []
            for k in kids:
                d = k.get("data") or {}
                if float(d.get("created_utc") or 0) < cutoff:
                    continue
                out.append(SocialPost(
                    "reddit", str(d.get("id")), d.get("title") or "", (d.get("selftext") or "")[:1500],
                    "https://www.reddit.com" + (d.get("permalink") or ""), _day(float(d.get("created_utc") or 0)),
                    int(d.get("score") or 0), int(d.get("num_comments") or 0), d.get("subreddit") or ""))
            after = (data.get("data") or {}).get("after")
            if not after or len(out) >= limit:
                break
            time.sleep(self.sleep_seconds)
        return out[:limit]


# ------------------------------------------------------------------ YouTube
class YouTubeSource:
    name = "youtube"
    API = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str, session: requests.Session | None = None, comment_videos: int = 3):
        if not api_key:
            raise SocialError("Thiếu YOUTUBE_API_KEY")
        self.key = api_key
        self.session = session or make_session()
        self.comment_videos = comment_videos

    def search(self, query: str, since: date, limit: int = 100) -> list[SocialPost]:
        published = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = get_json(self.session, f"{self.API}/search", {
            "part": "snippet", "q": query, "type": "video", "maxResults": 50, "order": "relevance",
            "publishedAfter": published, "key": self.key}, retries=1)
        items = data.get("items") or []
        ids = [i["id"]["videoId"] for i in items if (i.get("id") or {}).get("videoId")]
        stats = {}
        if ids:
            sd = get_json(self.session, f"{self.API}/videos",
                          {"part": "statistics", "id": ",".join(ids), "key": self.key}, retries=1)
            stats = {v["id"]: v.get("statistics") or {} for v in sd.get("items") or []}
        out = []
        for i in items:
            vid = (i.get("id") or {}).get("videoId")
            if not vid:
                continue
            sn, st = i.get("snippet") or {}, stats.get(vid, {})
            out.append(SocialPost("youtube", vid, sn.get("title") or "", (sn.get("description") or "")[:800],
                                  f"https://www.youtube.com/watch?v={vid}", _day(sn.get("publishedAt") or ""),
                                  int(st.get("viewCount") or 0), int(st.get("commentCount") or 0),
                                  sn.get("channelTitle") or ""))
        # bình luận của các video nhiều bình luận nhất — lời thật của người xem
        for v in sorted(out, key=lambda p: -p.comments)[: self.comment_videos]:
            if not v.comments:
                continue
            try:
                cd = get_json(self.session, f"{self.API}/commentThreads", {
                    "part": "snippet", "videoId": v.id, "maxResults": 50, "order": "relevance",
                    "textFormat": "plainText", "key": self.key}, retries=1)
            except HttpError as exc:  # video tắt bình luận → 403
                log.info("YouTube bình luận %s: %s", v.id, exc)
                continue
            for c in cd.get("items") or []:
                top = ((c.get("snippet") or {}).get("topLevelComment") or {})
                s = top.get("snippet") or {}
                if _day(s.get("publishedAt") or "") < since.isoformat():
                    continue
                out.append(SocialPost("youtube_comment", top.get("id") or "", "", (s.get("textOriginal") or "")[:600],
                                      f"https://www.youtube.com/watch?v={v.id}&lc={top.get('id')}",
                                      _day(s.get("publishedAt") or ""), int(s.get("likeCount") or 0), 0,
                                      v.community))
        return out[:limit * 2]


def make_sources(enabled: list[str], env: dict) -> tuple[list[SocialSource], list[str]]:
    """(nguồn dùng được, lý do các nguồn bị bỏ qua)."""
    sources: list[SocialSource] = []
    skipped = []
    for name in enabled:
        try:
            if name == "reddit":
                sources.append(RedditSource(env.get("REDDIT_CLIENT_ID", ""), env.get("REDDIT_CLIENT_SECRET", "")))
            elif name == "youtube":
                sources.append(YouTubeSource(env.get("YOUTUBE_API_KEY", "")))
            elif name in ("tiktok", "instagram", "facebook"):
                skipped.append(f"{name}: chưa có API công khai — cần nối qua connector bên thứ ba")
            else:
                skipped.append(f"{name}: không hỗ trợ")
        except SocialError as exc:
            skipped.append(f"{name}: {exc}")
    return sources, skipped


def window_since(today: date, days: int) -> date:
    return today - timedelta(days=days)
