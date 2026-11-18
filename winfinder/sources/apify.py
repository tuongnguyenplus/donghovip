"""Client Apify có TRẦN CHI PHÍ cho mỗi lần quét.

Mọi actor của Apify tính tiền theo kết quả. Một lần quét dùng chung một `ApifyBudget`: trước mỗi lượt chạy, tool
tính phần ngân sách còn lại và truyền `maxTotalChargeUsd` cho Apify — Apify tự dừng actor khi chạm trần, nên
không bao giờ tiêu quá ngân sách. Hết ngân sách → bỏ qua lượt chạy, ghi lý do.

Xác thực: biến môi trường APIFY_TOKEN (nhập trên trang tool). Trong môi trường cloud có proxy tự gắn key thì
không cần biến này.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from winfinder.http import make_session

log = logging.getLogger(__name__)

API = "https://api.apify.com/v2"
MIN_RUN_CAP = 0.5           # một số actor (TikTok) từ chối trần < $0.50 — trần chỉ là giới hạn, không phải phí
TERMINAL = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}


class ApifyError(RuntimeError):
    pass


class BudgetExhausted(ApifyError):
    pass


@dataclass
class ApifyBudget:
    limit_usd: float
    spent_usd: float = 0.0
    runs: list[dict] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    def summary(self) -> str:
        return f"Apify: đã dùng ${self.spent_usd:.2f} / trần ${self.limit_usd:.2f} ({len(self.runs)} lượt chạy)"


class ApifyClient:
    def __init__(self, budget: ApifyBudget, token: str | None = None, session: requests.Session | None = None,
                 per_run_cap: float = 1.0, poll_seconds: float = 5.0, timeout_seconds: float = 900):
        self.budget = budget
        self.session = session or make_session()
        token = token if token is not None else os.environ.get("APIFY_TOKEN", "")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.per_run_cap = per_run_cap
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    def _req(self, method: str, url: str, **kw) -> Any:
        for attempt in range(4):
            try:
                r = self.session.request(method, url, timeout=60, **kw)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise ApifyError(f"Không kết nối được Apify: {exc}") from exc
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (401, 403):
                raise ApifyError("Apify từ chối xác thực — kiểm tra APIFY_TOKEN")
            if r.status_code >= 400:
                try:
                    err = r.json().get("error") or {}
                except ValueError:
                    err = {}
                raise ApifyError(f"Apify lỗi {r.status_code}: {err.get('type', '')} {err.get('message', r.text[:200])}")
            return r.json()
        raise ApifyError("Apify quá tải, thử lại sau")

    def run(self, actor: str, run_input: dict, *, max_cost: float | None = None, label: str = "") -> list[dict]:
        """Chạy actor (vd 'apify/facebook-ads-scraper') → dataset items. Không bao giờ vượt ngân sách còn lại."""
        cap = min(max_cost or self.per_run_cap, self.budget.remaining)
        if cap < 0.01:
            raise BudgetExhausted(f"hết ngân sách Apify ({self.budget.summary()})")
        actor_id = actor.replace("/", "~")
        try:
            data = self._req("POST", f"{API}/acts/{actor_id}/runs", params={"maxTotalChargeUsd": round(cap, 2)},
                             json=run_input)["data"]
        except ApifyError as exc:
            if "below-minimum" not in str(exc):
                raise
            if self.budget.remaining < MIN_RUN_CAP:
                raise BudgetExhausted(f"actor {actor} cần trần tối thiểu ${MIN_RUN_CAP}, ngân sách còn "
                                      f"${self.budget.remaining:.2f}") from exc
            cap = MIN_RUN_CAP
            data = self._req("POST", f"{API}/acts/{actor_id}/runs", params={"maxTotalChargeUsd": cap},
                             json=run_input)["data"]
        run_id, started = data["id"], time.time()
        while data.get("status") not in TERMINAL:
            if time.time() - started > self.timeout_seconds:
                self._req("POST", f"{API}/actor-runs/{run_id}/abort")
                data = self._req("GET", f"{API}/actor-runs/{run_id}")["data"]
                break
            time.sleep(self.poll_seconds)
            data = self._req("GET", f"{API}/actor-runs/{run_id}")["data"]
        cost = float(data.get("usageTotalUsd") or 0.0)
        self.budget.spent_usd += cost
        self.budget.runs.append({"actor": actor, "label": label, "status": data.get("status"), "cost": round(cost, 4)})
        log.info("Apify %s %s: %s, $%.3f (còn $%.2f)", actor, label, data.get("status"), cost, self.budget.remaining)
        if data.get("status") not in ("SUCCEEDED", "ABORTED", "TIMED-OUT"):
            raise ApifyError(f"actor {actor} kết thúc với trạng thái {data.get('status')}")
        items = self._req("GET", f"{API}/datasets/{data['defaultDatasetId']}/items",
                          params={"clean": "true", "format": "json"})
        # actor trả lỗi dưới dạng 1 item {"error": ...} khi bị chặn
        return [x for x in items if isinstance(x, dict) and not x.get("error")]


def available() -> bool:
    """Có token trên máy, hoặc đang chạy trong môi trường có proxy gắn key (đặt APIFY_TOKEN=proxy-injected)."""
    return bool(os.environ.get("APIFY_TOKEN"))
