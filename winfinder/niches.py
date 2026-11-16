"""Danh sách ngách (niches.yaml) — sửa được từ giao diện web (`winfinder serve`) hoặc bằng tay."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

LANG_RE = re.compile(r"^[a-z]{2}$")


@dataclass
class Niche:
    name: str
    keywords: list[str]
    languages: list[str] = field(default_factory=list)
    enabled: bool = True
    note: str = ""


class NicheError(ValueError):
    pass


def split_list(text: str) -> list[str]:
    """'a, b\\nc' → ['a', 'b', 'c'] (bỏ trống, bỏ trùng, giữ thứ tự)."""
    items = [" ".join(x.split()) for x in re.split(r"[,\n;]", text or "")]
    return list(dict.fromkeys(x for x in items if x))


def validate(n: Niche, others: list[Niche]) -> Niche:
    name = " ".join(n.name.split())
    if not name:
        raise NicheError("Tên ngách không được để trống")
    if len(name) > 60:
        raise NicheError("Tên ngách tối đa 60 ký tự")
    if any(o.name.lower() == name.lower() for o in others):
        raise NicheError(f"Đã có ngách tên “{name}”")
    kws = [k.lower() for k in n.keywords if k.strip()]
    if not kws:
        raise NicheError("Cần ít nhất 1 keyword")
    if len(kws) > 20:
        raise NicheError("Tối đa 20 keyword / ngách")
    if any(len(k) > 80 for k in kws):
        raise NicheError("Mỗi keyword tối đa 80 ký tự")
    langs = [lang.lower() for lang in n.languages]
    bad = [lang for lang in langs if not LANG_RE.match(lang)]
    if bad:
        raise NicheError(f"Mã ngôn ngữ không hợp lệ: {', '.join(bad)} (dùng mã 2 chữ, vd en, de, fr)")
    return Niche(name, list(dict.fromkeys(kws)), list(dict.fromkeys(langs)), bool(n.enabled), n.note.strip()[:200])


def load_niches(path: str | Path) -> list[Niche]:
    path = Path(path)
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for x in raw.get("niches") or []:
        out.append(Niche(
            name=str(x.get("name", "")),
            keywords=[str(k) for k in x.get("keywords") or []],
            languages=[str(lang) for lang in x.get("languages") or []],
            enabled=bool(x.get("enabled", True)),
            note=str(x.get("note") or ""),
        ))
    return out


def save_niches(path: str | Path, niches: list[Niche]) -> None:
    """Ghi nguyên tử (file tạm rồi đổi tên) để không bao giờ để lại file hỏng."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ("# Danh sách ngách — sửa từ giao diện `winfinder serve` hoặc sửa tay.\n"
            "# keywords: dùng để tìm ad trên Ad Library và làm chủ đề Trends.\n"
            "# languages: lọc ngôn ngữ ad (mã 2 chữ: en, de, fr...). Bỏ trống = mọi ngôn ngữ.\n")
    body += yaml.safe_dump({"niches": [asdict(n) for n in niches]}, allow_unicode=True, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".niches-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def upsert(niches: list[Niche], new: Niche, original_name: str | None = None) -> list[Niche]:
    # thêm mới: phải khác tên mọi ngách; sửa: khác tên mọi ngách trừ chính nó
    others = [n for n in niches if not original_name or n.name.lower() != original_name.lower()]
    v = validate(new, others)
    if original_name and not any(n.name.lower() == original_name.lower() for n in niches):
        raise NicheError(f"Không tìm thấy ngách “{original_name}”")
    out, replaced = [], False
    for n in niches:
        if original_name and n.name.lower() == original_name.lower():
            out.append(v)
            replaced = True
        else:
            out.append(n)
    if not replaced:
        out.append(v)
    return out


def delete(niches: list[Niche], name: str) -> list[Niche]:
    out = [n for n in niches if n.name.lower() != name.lower()]
    if len(out) == len(niches):
        raise NicheError(f"Không tìm thấy ngách “{name}”")
    return out
