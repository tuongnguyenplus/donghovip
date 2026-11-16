"""HTTP helper có retry/backoff dùng chung cho mọi nguồn dữ liệu."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

USER_AGENT = "winfinder/0.1 (+research tool)"
RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def get_text(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    **kw: Any,
) -> str:
    """Như get_json nhưng trả về text thô (cho endpoint có tiền tố chống JSON-hijacking)."""
    return _get(session, url, params, **kw).text


def get_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    **kw: Any,
) -> Any:
    return _get(session, url, params, **kw).json()


def _get(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    retries: int = 4,
    backoff: float = 2.0,
    timeout: float = 30.0,
) -> requests.Response:
    """GET; retry khi lỗi mạng hoặc status 429/5xx."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
        else:
            if resp.status_code < 400:
                return resp
            if resp.status_code not in RETRY_STATUS:
                raise HttpError(resp.status_code, resp.text)
            last_exc = HttpError(resp.status_code, resp.text)
        if attempt < retries:
            wait = backoff * (2**attempt)
            log.warning("GET %s thất bại (%s), thử lại sau %.0fs", url, last_exc, wait)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc
