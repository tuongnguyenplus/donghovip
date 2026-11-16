"""Xác minh tính xác thực của angle/persona bằng bài viết thật trên mạng xã hội.

Hai lớp:
  1. Số liệu: khối lượng thảo luận 90 ngày gần / 90 ngày trước (đà), tương tác, cộng đồng nào đang nói.
  2. Claude đọc mẫu bài viết: persona này có THẬT không, có nói về nỗi đau này không, bằng lời nào
     (trích dẫn nguyên văn + cụm từ khách hàng → dùng làm hook/copy). Không có key → heuristic, ghi rõ.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Protocol

from winfinder.sources.social import SocialPost

log = logging.getLogger(__name__)

VERDICT_VI = {
    "confirmed": "Xác thực — persona thật, nói rõ về nỗi đau này",
    "partial": "Một phần — có người nói, nhưng persona/nỗi đau lệch so với ad",
    "weak": "Yếu — rất ít bài liên quan",
    "not_found": "Không tìm thấy — không thấy persona này nói về nỗi đau này",
    "unverified": "Chưa xác minh — không có dữ liệu mạng xã hội",
}
VERDICT_WEIGHT = {"confirmed": 1.0, "partial": 0.6, "weak": 0.25, "not_found": 0.0}

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["confirmed", "partial", "weak", "not_found"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "relevant_refs": {"type": "array", "items": {"type": "string"},
                          "description": "ids of posts genuinely written by this persona about this pain/desire"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "quote": {"type": "string", "description": "verbatim excerpt, max 25 words"},
                    "why": {"type": "string", "description": "Vietnamese, why this supports or contradicts"},
                },
                "required": ["ref", "quote", "why"],
                "additionalProperties": False,
            },
        },
        "customer_phrases": {"type": "array", "items": {"type": "string"},
                             "description": "up to 8 short English phrases these people actually use"},
        "persona_notes": {"type": "string", "description": "Vietnamese: who really talks about it, vs the ad persona"},
        "risks": {"type": "string", "description": "Vietnamese: anything making this angle weaker than it looks"},
    },
    "required": ["verdict", "confidence", "relevant_refs", "evidence", "customer_phrases", "persona_notes", "risks"],
    "additionalProperties": False,
}

SYSTEM = """You validate a direct-response ad angle against real social media posts.

The angle claims a specific persona has a specific pain or desire. Judge from the posts ONLY whether real people
of that persona talk about that pain, how much, and in which words. Search results are noisy: ignore ads,
brand posts, spam, and posts about something else even if they share keywords.

verdict:
- confirmed: several posts clearly written by this persona about this pain/desire.
- partial: people talk about the pain but they are a different persona, or the pain is framed differently.
- weak: only one or two relevant posts.
- not_found: nothing relevant.

Evidence quotes must be copied verbatim from the posts (max 25 words) and reference the post id.
customer_phrases: the exact wording real people use (great for hooks). Never invent quotes or phrases.
Write persona_notes, risks and every "why" in Vietnamese."""


@dataclass
class SocialStats:
    posts: int
    volume_90: int
    volume_prev_90: int
    growth: float | None           # (90 ngày gần + 1) / (90 ngày trước + 1)
    engagement: float | None       # trung vị (upvote + bình luận) bài Reddit / lượt thích bình luận
    match_share: float             # % bài có chứa cụm từ của nỗi đau (heuristic)
    communities: list[tuple[str, int]]
    by_source: dict[str, int]


@dataclass
class Verification:
    verdict: str
    verdict_vi: str
    confidence: str
    relevant_share: float | None
    evidence: list[dict] = field(default_factory=list)
    customer_phrases: list[str] = field(default_factory=list)
    persona_notes: str = ""
    risks: str = ""
    method: str = "heuristic"      # claude | heuristic | none
    model: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def dedupe(posts: list[SocialPost]) -> list[SocialPost]:
    seen, out = set(), []
    for p in posts:
        if (p.source, p.id) not in seen:
            seen.add((p.source, p.id))
            out.append(p)
    return out


def _phrases(keywords: list[str]) -> list[str]:
    out = set()
    for k in keywords:
        w = k.lower().split()
        out.update(" ".join(w[i:i + 2]) for i in range(len(w) - 1))
        if len(w) == 1:
            out.add(w[0])
    return [p for p in out if len(p) > 3]


def stats(posts: list[SocialPost], keywords: list[str], today: date) -> SocialStats:
    d90, d180 = (today - timedelta(days=90)).isoformat(), (today - timedelta(days=180)).isoformat()
    v90 = sum(1 for p in posts if p.created >= d90)
    prev = sum(1 for p in posts if d180 <= p.created < d90)
    reddit = [p.score + p.comments for p in posts if p.source == "reddit"]
    likes = [p.score for p in posts if p.source == "youtube_comment"]
    eng = statistics.median(reddit) if reddit else (statistics.median(likes) if likes else None)
    phr = _phrases(keywords)
    match = sum(1 for p in posts if any(x in f"{p.title} {p.text}".lower() for x in phr)) if phr else 0
    comm = Counter(p.community for p in posts if p.community)
    return SocialStats(
        posts=len(posts), volume_90=v90, volume_prev_90=prev,
        growth=round((v90 + 1) / (prev + 1), 2) if posts else None,
        engagement=round(float(eng), 1) if eng is not None else None,
        match_share=round(match / len(posts), 3) if posts else 0.0,
        communities=comm.most_common(5), by_source=dict(Counter(p.source for p in posts)),
    )


def heuristic_verdict(st: SocialStats) -> Verification:
    if not st.posts:
        return Verification("not_found", VERDICT_VI["not_found"], "low", 0.0, method="heuristic")
    relevant = st.posts * st.match_share
    if relevant >= 20 and st.match_share >= 0.3:
        v = "partial"  # heuristic không đủ để nói 'confirmed' — cần người/Claude đọc
    elif relevant >= 5:
        v = "weak"
    else:
        v = "not_found"
    return Verification(v, VERDICT_VI[v], "low", st.match_share, method="heuristic",
                        persona_notes="Chưa có Claude đọc bài viết — chỉ đếm bài có chứa cụm từ của nỗi đau.")


def sample(posts: list[SocialPost], n: int = 40) -> list[SocialPost]:
    """Ưu tiên bài có tương tác + mới; trộn đều các nguồn."""
    def key(p: SocialPost):
        return (math.log1p(p.score + p.comments), p.created)

    by_src: dict[str, list[SocialPost]] = {}
    for p in sorted(posts, key=key, reverse=True):
        by_src.setdefault(p.source, []).append(p)
    out: list[SocialPost] = []
    while len(out) < n and any(by_src.values()):
        for src in list(by_src):
            if by_src[src] and len(out) < n:
                out.append(by_src[src].pop(0))
    return out


def fingerprint(angle: dict, posts: list[SocialPost]) -> str:
    h = hashlib.sha256(json.dumps([angle.get("persona"), angle.get("pain_point"), angle.get("promise")],
                                  ensure_ascii=False).encode())
    for p in sorted(f"{x.source}:{x.id}" for x in posts):
        h.update(p.encode())
    return h.hexdigest()[:16]


class Verifier(Protocol):
    model: str

    def verify(self, product: str, angle: dict, posts: list[SocialPost]) -> Verification | None: ...


class ClaudeVerifier:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", client=None):
        import anthropic

        self.model, self.effort = model, effort
        self.client = client or anthropic.Anthropic()
        self._a = anthropic

    def verify(self, product: str, angle: dict, posts: list[SocialPost]) -> Verification | None:
        listing = "\n".join(
            f"p{i}: [{p.source}{' r/' + p.community if p.source == 'reddit' else ''} · {p.created} · "
            f"{p.score} score · {p.comments} comments] {p.title} — {p.text[:300]}".replace("\n", " ")
            for i, p in enumerate(posts, 1))
        prompt = (f"Product: {product}\nAd angle: {angle.get('name')}\nPersona (from ads): {angle.get('persona')}\n"
                  f"Pain/desire: {angle.get('pain_point')}\nPromise: {angle.get('promise')}\n\n"
                  f"{len(posts)} posts found on social media:\n{listing}")
        a = self._a
        try:
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=8000, system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        except a.APIStatusError as exc:
            log.error("Claude API lỗi %s khi xác minh angle: %s", exc.status_code, exc.message)
            return None
        except a.APIConnectionError as exc:
            log.error("Không kết nối được Claude API: %s", exc)
            return None
        if resp.stop_reason in ("refusal", "max_tokens"):
            log.warning("Xác minh angle dừng: %s", resp.stop_reason)
            return None
        try:
            data = json.loads(next(b.text for b in resp.content if b.type == "text"))
        except (StopIteration, ValueError):
            return None
        return parse_verification(data, posts, resp.model)


def parse_verification(data: dict, posts: list[SocialPost], model: str = "") -> Verification:
    refs = {f"p{i}": p for i, p in enumerate(posts, 1)}
    relevant = [r for r in data.get("relevant_refs", []) if r in refs]
    evidence = []
    for e in data.get("evidence", [])[:6]:
        p = refs.get(e.get("ref"))
        if not p:
            continue
        quote = " ".join(str(e.get("quote", "")).split()[:30])
        # chỉ giữ trích dẫn có thật trong bài (chống bịa)
        if quote and quote.lower()[:40] in f"{p.title} {p.text}".lower().replace("\n", " "):
            evidence.append({"quote": quote, "why": e.get("why", ""), "url": p.url, "source": p.source,
                             "community": p.community, "date": p.created})
    v = data.get("verdict", "not_found")
    return Verification(
        v, VERDICT_VI.get(v, v), data.get("confidence", "low"),
        round(len(relevant) / len(posts), 3) if posts else None, evidence,
        [" ".join(str(x).split()) for x in data.get("customer_phrases", [])][:8],
        data.get("persona_notes", ""), data.get("risks", ""), "claude", model)


def social_score(st: SocialStats | None, ver: Verification | None) -> float | None:
    """0–100: xác thực (50%) + khối lượng 90 ngày (25%) + đà thảo luận (25%)."""
    if st is None or ver is None or ver.verdict == "unverified":
        return None
    w = VERDICT_WEIGHT.get(ver.verdict, 0.0)
    rel = ver.relevant_share if ver.relevant_share is not None else st.match_share
    vol = min(1.0, math.log10(st.volume_90 * max(rel, 0.05) + 1) / 2)       # ~100 bài liên quan → 1
    gr = min(1.0, max(0.0, ((st.growth or 1) - 0.8) / 1.2))
    return round(100 * (0.5 * w + 0.25 * vol + 0.25 * gr), 1)
