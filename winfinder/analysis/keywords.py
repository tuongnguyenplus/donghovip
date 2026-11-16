"""Rút keyword sản phẩm từ text của store: tiêu đề ad, nội dung ad, tên sản phẩm Shopify.

Heuristic (không cần LLM):
  - Làm sạch: bỏ tên brand (domain, tên page), số/phiên bản/%, từ marketing, màu/size.
  - Tách đoạn tại stopword ("for", "with"...) để cụm từ không vắt qua giới từ.
  - Sinh n-gram 2–3 từ trong từng đoạn, cộng điểm theo nguồn:
      sản phẩm Shopify khớp với ad (hero product) ×3, tiêu đề ad ×2, nội dung ad ×1,
      và ×1.5 nếu n-gram nằm cuối đoạn (danh từ chính trong tiếng Anh thường đứng cuối).
  - Cụm khớp keyword ngách (config `searches`) ×2.
  - Bỏ cụm 2 từ luôn dính với cùng một từ bên cạnh; keyword neo = cụm điểm cao nhất.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for", "from", "get", "has", "have",
    "how", "i", "if", "in", "into", "is", "it", "its", "just", "me", "my", "no", "not", "of", "on", "or",
    "our", "out", "so", "that", "the", "their", "them", "this", "to", "up", "us", "was", "we", "what",
    "when", "who", "why", "will", "with", "you", "your", "yours", "all", "any", "every", "more", "most",
    "than", "then", "there", "these", "those", "too", "very", "only", "also", "about", "after", "before",
    "day", "days", "minutes", "minute", "seconds", "second", "hour", "hours", "week", "weeks", "time",
    "one", "two", "three", "ever", "still", "never", "now", "today", "tonight", "here", "even", "much",
    "dont", "don", "t", "s", "im", "ive", "doctors", "people", "everyone", "nobody",
}
MARKETING = {
    "shop", "buy", "sale", "off", "free", "shipping", "ship", "limited", "offer", "deal", "deals", "discount",
    "order", "save", "hot", "sold", "stock", "official", "store", "new", "best", "seller", "bestseller",
    "guarantee", "guaranteed", "money", "refund", "trial", "risk", "click", "link", "bio", "tap", "learn",
    "discover", "try", "tried", "love", "loved", "hate", "hates", "simple", "easy", "amazing", "viral",
    "trending", "tiktok", "instagram", "facebook", "gift", "gifts", "mom", "dad", "christmas", "black",
    "friday", "cyber", "monday", "holiday", "promo", "code", "coupon", "bundle", "pack", "pcs", "piece",
    "pieces", "set", "kit", "size", "sizes", "color", "colors", "colour", "white", "pink", "blue", "grey",
    "gray", "red", "green", "small", "medium", "large", "xl", "xxl", "pro", "plus", "max", "ultra", "mini",
    "premium", "upgraded", "upgrade", "version", "edition", "original", "authentic", "genuine", "wear",
    "fix", "only", "unisex", "women", "men", "womens", "mens", "kids", "adults", "adult", "stop", "need",
    "want", "finally", "works", "work", "make", "makes", "made", "use", "using", "used", "like", "look",
}
_TOKEN = re.compile(r"[a-z][a-z'\-]*[a-z]|[a-z]")


@dataclass
class ProductKeywords:
    seed: str | None
    keywords: list[str]           # gồm seed ở đầu
    source: str                   # "shopify+ads" | "ads"
    ranked: list[tuple[str, float]]


def brand_tokens(domain: str | None, page_names: list[str]) -> set[str]:
    toks: set[str] = set()
    if domain:
        label = domain.split(".")[-2] if "." in domain else domain
        toks.add(label.lower())
        toks.update(t for t in re.split(r"[-_]", label.lower()) if len(t) > 2)
    for name in page_names:
        low = name.lower()
        toks.add(re.sub(r"[^a-z0-9]", "", low))
        toks.update(t for t in _TOKEN.findall(low) if len(t) > 2)
    return toks


def singular(tok: str) -> str:
    """Số nhiều tiếng Anh đơn giản: correctors→corrector, brushes→brush, batteries→battery."""
    if len(tok) <= 3 or tok.endswith(("ss", "us", "is")):
        return tok
    if tok.endswith("ies") and len(tok) > 4:
        return tok[:-3] + "y"
    if tok.endswith(("shes", "ches", "xes", "sses")):
        return tok[:-2]
    if tok.endswith("s"):
        return tok[:-1]
    return tok


def segments(text: str, brand: set[str]) -> list[list[str]]:
    """Tách text thành các đoạn token sạch; mọi từ bị loại đều làm đứt đoạn."""
    text = re.sub(r"[™®©]", " ", text.lower())
    out: list[list[str]] = []
    for chunk in re.split(r"[|:;,.!?()\[\]/\"“”—–#+&*]+|\s-\s", text):
        cur: list[str] = []
        for raw in re.split(r"[^a-z0-9'\-]+", chunk):
            tok = raw.strip("'-")
            bad = (
                not tok or any(c.isdigit() for c in tok) or len(tok) < 2 or tok in STOPWORDS
                or tok in MARKETING or tok in brand or tok.endswith("'s")
            )
            if bad:
                if cur:
                    out.append(cur)
                cur = []
            else:
                cur.append(singular(tok))
        if cur:
            out.append(cur)
    return out


def _ngrams(seg: list[str]) -> list[tuple[int, str, bool]]:
    """(vị trí, cụm, có nằm cuối đoạn không)"""
    grams = []
    for n in (2, 3):
        for i in range(len(seg) - n + 1):
            grams.append((i, " ".join(seg[i : i + n]), i + n == len(seg)))
    return grams


def extract(
    ad_titles: list[str],
    ad_bodies: list[str],
    product_titles: list[str] | None = None,
    *,
    domain: str | None = None,
    page_names: list[str] | None = None,
    context_keywords: list[str] | None = None,
    max_keywords: int = 4,
) -> ProductKeywords:
    """context_keywords: keyword ngách đã dùng để tìm ra ad (config `searches`) — cụm khớp được ×2,
    giúp chọn 'cat hair remover' thay vì 'hair remover' (chung chung, lẫn nhu cầu khác)."""
    brand = brand_tokens(domain, page_names or [])
    ad_tokens = {t for s in ad_titles for seg in segments(s, brand) for t in seg}
    # sản phẩm Shopify chỉ tính khi có từ trùng với tiêu đề ad → sản phẩm đang được chạy ad (hero)
    hero = [p for p in product_titles or [] if {t for seg in segments(p, brand) for t in seg} & ad_tokens]

    scores: Counter[str] = Counter()
    lefts: dict[str, list[str | None]] = {}   # từ đứng ngay trước mỗi lần xuất hiện
    rights: dict[str, list[str | None]] = {}  # từ đứng ngay sau
    for weight, texts in ((3.0, hero), (2.0, ad_titles), (1.0, ad_bodies)):
        for text in texts:
            seen: set[str] = set()  # mỗi text chỉ cộng 1 lần cho mỗi n-gram
            for seg in segments(text, brand):
                for i, gram, at_end in _ngrams(seg):
                    n = len(gram.split())
                    lefts.setdefault(gram, []).append(seg[i - 1] if i > 0 else None)
                    rights.setdefault(gram, []).append(seg[i + n] if i + n < len(seg) else None)
                    if gram in seen:
                        continue
                    seen.add(gram)
                    scores[gram] += weight * (1.5 if at_end else 1.0)

    ctx = [" ".join(t for seg in segments(k, set()) for t in seg) for k in context_keywords or []]
    for g in scores:
        if any(c and (c in g or g in c) and len(g.split()) >= len(c.split()) for c in ctx):
            scores[g] *= 2.0

    # n-gram chỉ xuất hiện 1 lần trong 1 nội dung ad là nhiễu
    ranked = sorted(((g, s) for g, s in scores.items() if s > 1.5), key=lambda x: (-x[1], len(x[0].split()), x[0]))
    if not ranked:
        return ProductKeywords(None, [], "ads", [])
    # bỏ cụm 2 từ không đứng độc lập: mọi lần xuất hiện đều có CÙNG một từ đứng trước (hair remover ← cat)
    # hoặc CÙNG một từ đứng sau (smart posture → corrector). Giữ 'dog vest' nếu có heated/waterproof dog vest.
    def bound(ctx: list[str | None]) -> bool:
        return None not in ctx and len(set(ctx)) == 1

    ranked = [
        (g, sc) for g, sc in ranked
        if len(g.split()) > 2 or not (bound(lefts.get(g, [None])) or bound(rights.get(g, [None])))
    ]
    seed = ranked[0][0]
    picked = [seed]
    for g, _ in ranked:
        if len(picked) >= max_keywords:
            break
        # keyword phụ phải có từ chung với keyword neo — tránh cộng nhầm nhu cầu khác ('winter walk')
        if (g not in picked and set(g.split()) & set(seed.split())
                and not any(set(g.split()) == set(p.split()) for p in picked)):
            picked.append(g)
    return ProductKeywords(seed, picked, "shopify+ads" if hero else "ads", ranked[:15])
