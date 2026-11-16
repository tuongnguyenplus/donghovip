import json
from datetime import date

from winfinder.sources.adlibrary import AdLibraryClient, extract_domain, parse_ad


def test_extract_domain_normalizes_caption():
    assert extract_domain(["WWW.PostureGlow.COM"]) == "postureglow.com"
    assert extract_domain(["https://shop.example.co/products/x"]) == "shop.example.co"


def test_extract_domain_skips_excluded_and_garbage():
    exclude = {"amazon.com", "fb.me"}
    assert extract_domain(["Shop now", "AMAZON.COM", "smile.amazon.com"], exclude) is None
    assert extract_domain(["fb.me", "realstore.shop"], exclude) == "realstore.shop"


def test_parse_ad():
    ad = parse_ad(
        {
            "id": 1,
            "page_id": 9,
            "page_name": "P",
            "ad_delivery_start_time": "2026-11-01T08:00:00+0000",
            "ad_creative_link_captions": ["STORE.COM"],
            "eu_total_reach": "1200",
        }
    )
    assert (ad.ad_id, ad.domain, ad.start_date, ad.eu_total_reach) == ("1", "store.com", "2026-11-01", 1200)
    assert ad.stop_date is None and ad.bodies == []


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


def test_search_follows_pagination_and_retries_rate_limit():
    session = FakeSession(
        [
            FakeResp(400, {"error": {"code": 613, "message": "rate limit"}}),
            FakeResp(200, {"data": [{"id": "1"}], "paging": {"next": "https://next"}}),
            FakeResp(200, {"data": [{"id": "2"}]}),
        ]
    )
    client = AdLibraryClient("tok", session=session, sleep_seconds=0, rate_limit_wait=0)
    ads = list(client.search("posture", "IE", date(2026, 9, 1)))
    assert [a["id"] for a in ads] == ["1", "2"]
    first_params = session.calls[0][1]
    assert first_params["ad_reached_countries"] == '["IE"]'
    assert session.calls[2] == ("https://next", None)
