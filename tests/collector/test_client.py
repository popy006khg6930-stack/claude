from __future__ import annotations

import logging
import ssl
import urllib.error

import pytest

from silgeorae.collector import FakeTransport, HttpResponse, MolitClient, build_error_xml, build_response_xml
from silgeorae.errors import ApiError, ConfigError, QuotaExceededError, ServiceKeyError
from silgeorae.models import DealType

APT = DealType.APT_SALE
KEY = "TEST-KEY"


def make_items(n: int) -> list[dict[str, str]]:
    return [{"aptNm": f"단지{i:04d}", "dealAmount": f"{10000 + i:,}", "dealYear": "2025"} for i in range(n)]


def ok(items=(), **kw) -> HttpResponse:
    return HttpResponse(200, build_response_xml(list(items), **kw).encode("utf-8"))


def make_client(transport, sleeps, **kw) -> MolitClient:
    return MolitClient(kw.pop("service_key", KEY), transport=transport, sleep=sleeps, **kw)


class ScriptedTransport:
    """정해진 순서대로 응답(또는 예외)을 돌려주는 Transport. 마지막 항목은 계속 반복한다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url, params, timeout):
        self.calls.append((url, dict(params), timeout))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# ---------------------------------------------------------------------- 요청·응답
def test_fetch_sends_expected_request(fixture_response, sleeps):
    transport = ScriptedTransport(fixture_response("apt_trade.xml"))
    client = make_client(transport, sleeps, timeout=7.5)
    items = client.fetch(APT, "11680", "202501")
    assert len(items) == 3 and items[0]["aptNm"] == "한빛마을1단지"
    [(url, params, timeout)] = transport.calls
    assert url == "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
    assert params == {"serviceKey": KEY, "LAWD_CD": "11680", "DEAL_YMD": "202501", "pageNo": "1", "numOfRows": "1000"}
    assert timeout == 7.5
    assert client.request_count == 1 and sleeps == []


def test_fetch_legacy_and_empty(fixture_response, sleeps):
    client = make_client(ScriptedTransport(fixture_response("legacy_apt_trade.xml")), sleeps)
    assert client.fetch(APT, "11110", "202112")[0]["거래금액"] == "    82,500"
    client = make_client(ScriptedTransport(fixture_response("empty.xml")), sleeps)
    assert client.fetch(APT, "11110", "202112") == []


@pytest.mark.parametrize("value, expected", [("2025-01", "202501"), (202501, "202501"), ("2025년 1월", "202501")])
def test_deal_ym_formats(value, expected, sleeps):
    fake = FakeTransport()
    make_client(fake, sleeps).fetch(APT, "11680", value)
    assert fake.calls[0][1]["DEAL_YMD"] == expected


def test_deal_type_given_as_text(sleeps):
    fake = FakeTransport({(DealType.APT_RENT, "11680", "202501"): make_items(2)})
    assert len(make_client(fake, sleeps).fetch("apt_rent", "11680", "202501")) == 2
    assert "RTMSDataSvcAptRent" in fake.calls[0][0]


def test_fetch_page(sleeps):
    fake = FakeTransport({(APT, "11680", "202501"): make_items(25)})
    page = make_client(fake, sleeps, page_size=10).fetch_page(APT, "11680", "202501", page_no=3)
    assert len(page.items) == 5 and page.total_count == 25 and page.page_no == 3


# ---------------------------------------------------------------------- 페이지네이션
def test_pagination_collects_all_pages_in_order(sleeps):
    items = make_items(2500)
    fake = FakeTransport({(APT, "11680", "202501"): items})
    client = make_client(fake, sleeps, page_size=1000)
    assert client.fetch(APT, "11680", "202501") == items
    assert [params["pageNo"] for _, params in fake.calls] == ["1", "2", "3"]
    assert {params["numOfRows"] for _, params in fake.calls} == {"1000"}
    assert client.request_count == 3


def test_pagination_exact_multiple_needs_no_extra_call(sleeps):
    fake = FakeTransport({(APT, "11680", "202501"): make_items(2000)})
    assert len(make_client(fake, sleeps, page_size=1000).fetch(APT, "11680", "202501")) == 2000
    assert len(fake.calls) == 2


def test_pagination_stops_on_empty_page(sleeps, caplog):
    transport = ScriptedTransport(ok(make_items(2), total_count=10), ok([], total_count=10))
    with caplog.at_level(logging.WARNING, logger="silgeorae.collector.client"):
        items = make_client(transport, sleeps, page_size=2).fetch(APT, "11680", "202501")
    assert len(items) == 2 and len(transport.calls) == 2
    assert "10건 중 2건" in caplog.text


def test_pagination_guards_against_ignored_page_no(sleeps):
    transport = ScriptedTransport(ok(make_items(2), total_count=10))  # 항상 같은 페이지
    items = make_client(transport, sleeps, page_size=2).fetch(APT, "11680", "202501")
    assert len(items) == 2 and len(transport.calls) == 2


# ---------------------------------------------------------------------- 재시도
def test_retryable_errors_then_success(sleeps):
    transport = ScriptedTransport(
        HttpResponse(200, build_error_xml("99", "UNKNOWN_ERROR")),
        HttpResponse(500, b"<html>Internal Server Error</html>"),
        ok(make_items(3)),
    )
    client = make_client(transport, sleeps)
    assert len(client.fetch(APT, "11680", "202501")) == 3
    assert sleeps == [1.0, 2.0]
    assert client.request_count == 3


def test_retries_exhausted(sleeps):
    transport = ScriptedTransport(HttpResponse(503, b"Service Unavailable"))
    client = make_client(transport, sleeps, max_retries=3)
    with pytest.raises(ApiError) as info:
        client.fetch(APT, "11680", "202501")
    err = info.value
    assert type(err) is ApiError and err.retryable is True
    assert (err.deal_type, err.lawd_cd, err.deal_ym) == (APT, "11680", "202501")
    assert err.code == "HTTP 503" and "4회" in str(err)
    assert sleeps == [1.0, 2.0, 4.0]
    assert client.request_count == 4


@pytest.mark.parametrize("max_retries, backoff, expected", [(0, 1.0, []), (2, 0.5, [0.5, 1.0]), (1, 0.0, [])])
def test_backoff_schedule(max_retries, backoff, expected, sleeps):
    transport = ScriptedTransport(HttpResponse(502, b""))
    client = make_client(transport, sleeps, max_retries=max_retries, backoff=backoff)
    with pytest.raises(ApiError):
        client.fetch(APT, "11680", "202501")
    assert sleeps == expected
    assert client.request_count == max_retries + 1


def test_network_errors_are_retried(sleeps):
    transport = ScriptedTransport(
        urllib.error.URLError("connection refused"),
        TimeoutError("timed out"),
        ConnectionResetError(104, "reset"),
        ok(make_items(1)),
    )
    client = make_client(transport, sleeps, max_retries=3)
    assert len(client.fetch(APT, "11680", "202501")) == 1
    assert sleeps == [1.0, 2.0, 4.0]


def test_network_error_exhausted_keeps_context(sleeps):
    client = make_client(ScriptedTransport(TimeoutError("timed out")), sleeps, max_retries=1)
    with pytest.raises(ApiError, match="응답 시간 초과") as info:
        client.fetch(DealType.LAND, "41135", "202403")
    assert info.value.retryable is True
    assert (info.value.deal_type, info.value.lawd_cd, info.value.deal_ym) == (DealType.LAND, "41135", "202403")
    assert isinstance(info.value.__cause__, ApiError)


def test_http_429_is_retried(sleeps):
    transport = ScriptedTransport(HttpResponse(429, b"Too Many Requests"), ok(make_items(1)))
    assert len(make_client(transport, sleeps).fetch(APT, "11680", "202501")) == 1
    assert sleeps == [1.0]


def test_certificate_error_is_not_retried(sleeps):
    cert_error = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
    transport = ScriptedTransport(cert_error)
    with pytest.raises(ApiError, match="SSL 인증서") as info:
        make_client(transport, sleeps).fetch(APT, "11680", "202501")
    assert info.value.retryable is False and len(transport.calls) == 1 and sleeps == []


# ---------------------------------------------------------------------- 중단 오류
def test_service_key_error_is_not_retried(fixture_response, sleeps):
    transport = ScriptedTransport(fixture_response("error_service_key.xml"))
    client = make_client(transport, sleeps)
    with pytest.raises(ServiceKeyError) as info:
        client.fetch(DealType.APT_RENT, "11680", "202501")
    err = info.value
    assert err.code == "30" and err.retryable is False
    assert (err.deal_type, err.lawd_cd, err.deal_ym) == (DealType.APT_RENT, "11680", "202501")
    message = str(err)
    assert "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in message
    assert DealType.APT_RENT.api_title in message  # 활용신청할 API 이름
    assert "Decoding" in message and "1시간" in message
    assert client.request_count == 1 and sleeps == []


def test_quota_error_is_not_retried(fixture_response, sleeps):
    transport = ScriptedTransport(fixture_response("error_quota.xml"))
    client = make_client(transport, sleeps)
    with pytest.raises(QuotaExceededError) as info:
        client.fetch(APT, "11680", "202501")
    assert info.value.code == "22" and info.value.lawd_cd == "11680" and info.value.deal_type is APT
    assert "내일" in str(info.value) and APT.api_title in str(info.value)
    assert client.request_count == 1 and sleeps == []


@pytest.mark.parametrize("status", [401, 403])
def test_http_401_403_is_service_key_error(status, sleeps):
    transport = ScriptedTransport(HttpResponse(status, b"Unauthorized"))
    with pytest.raises(ServiceKeyError) as info:
        make_client(transport, sleeps).fetch(APT, "11680", "202501")
    assert info.value.code == f"HTTP {status}" and info.value.deal_ym == "202501"
    assert APT.api_title in str(info.value)
    assert len(transport.calls) == 1 and sleeps == []


@pytest.mark.parametrize("status", [400, 404, 405])
def test_other_4xx_is_not_retried(status, sleeps):
    transport = ScriptedTransport(HttpResponse(status, b"<html>error</html>"))
    with pytest.raises(ApiError) as info:
        make_client(transport, sleeps).fetch(APT, "11680", "202501")
    assert type(info.value) is ApiError and info.value.retryable is False
    assert info.value.code == f"HTTP {status}" and info.value.lawd_cd == "11680"
    assert len(transport.calls) == 1 and sleeps == []


def test_error_body_beats_http_status(sleeps):
    body = build_error_xml("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR").encode()
    transport = ScriptedTransport(HttpResponse(500, body))
    with pytest.raises(ServiceKeyError) as info:
        make_client(transport, sleeps).fetch(APT, "11680", "202501")
    assert info.value.code == "30" and sleeps == []


def test_non_retryable_code_raises_immediately(sleeps):
    transport = ScriptedTransport(ok([], result_code="10", result_msg="INVALID_REQUEST_PARAMETER_ERROR"))
    with pytest.raises(ApiError) as info:
        make_client(transport, sleeps).fetch(APT, "11680", "202501")
    assert info.value.code == "10" and info.value.retryable is False
    assert (info.value.deal_type, info.value.deal_ym) == (APT, "202501")
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------- 인증키
@pytest.mark.parametrize(
    "given, sent",
    [
        ("abc%2Bdef%2Fg%3D%3D", "abc+def/g=="),  # Encoding 키 → Decoding 키
        ("abc+def/g==", "abc+def/g=="),  # Decoding 키는 그대로
        ("  abc+def/g==\n", "abc+def/g=="),
        ('"abc+def/g=="', "abc+def/g=="),
    ],
)
def test_service_key_normalization(given, sent, sleeps):
    fake = FakeTransport()
    make_client(fake, sleeps, service_key=given).fetch(APT, "11680", "202501")
    assert fake.calls[0][1]["serviceKey"] == sent


@pytest.mark.parametrize("key", ["", "   ", None])
def test_missing_service_key(key):
    with pytest.raises(ConfigError, match=r"data\.go\.kr"):
        MolitClient(key)


def test_key_is_not_leaked(sleeps, caplog):
    fake = FakeTransport({(APT, "11680", "202501"): make_items(1)})
    client = make_client(fake, sleeps, service_key="SECRET-KEY-123")
    with caplog.at_level(logging.DEBUG, logger="silgeorae.collector"):
        client.fetch(APT, "11680", "202501")
    assert "SECRET-KEY-123" not in caplog.text
    assert "SECRET-KEY-123" not in repr(client)


# ---------------------------------------------------------------------- 인자 검증·설정
@pytest.mark.parametrize("lawd_cd", ["1168", "116800", "abcde", "", "1168010600"])
def test_invalid_lawd_cd(lawd_cd, sleeps):
    fake = FakeTransport()
    with pytest.raises(ConfigError, match="5자리"):
        make_client(fake, sleeps).fetch(APT, lawd_cd, "202501")
    assert fake.calls == []


@pytest.mark.parametrize("deal_ym", ["2025-13", "abc", "25-01", ""])
def test_invalid_deal_ym(deal_ym, sleeps):
    fake = FakeTransport()
    with pytest.raises(ConfigError):
        make_client(fake, sleeps).fetch(APT, "11680", deal_ym)
    assert fake.calls == []


def test_invalid_deal_type(sleeps):
    with pytest.raises(ConfigError):
        make_client(FakeTransport(), sleeps).fetch("우주정거장", "11680", "202501")


@pytest.mark.parametrize(
    "kwargs", [{"page_size": 0}, {"base_url": "apis.data.go.kr/1613000"}, {"service_overrides": {APT: " "}}]
)
def test_invalid_settings(kwargs):
    with pytest.raises(ConfigError):
        MolitClient(KEY, **kwargs)


def test_url_for_every_deal_type():
    client = MolitClient(KEY)
    for deal_type in DealType:
        service = deal_type.api_service
        assert client.url_for(deal_type) == f"https://apis.data.go.kr/1613000/{service}/get{service}"


def test_service_overrides_and_base_url(sleeps):
    transport = ScriptedTransport(ok(make_items(1)))
    client = make_client(
        transport, sleeps, base_url="http://localhost:8080/mock/", service_overrides={APT: "RTMSDataSvcAptTrade"}
    )
    assert client.url_for(APT) == "http://localhost:8080/mock/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
    assert client.url_for(DealType.APT_RENT) == "http://localhost:8080/mock/RTMSDataSvcAptRent/getRTMSDataSvcAptRent"
    assert client.url_for("apt_sale") == client.url_for(APT)
    client.fetch(APT, "11680", "202501")
    assert transport.calls[0][0] == client.url_for(APT)
    assert MolitClient(KEY, service_overrides={"apt_sale": "X"}).url_for(APT).endswith("/X/getX")


def test_min_interval_spaces_requests():
    clock = FakeClock()
    fake = FakeTransport()
    client = MolitClient(KEY, transport=fake, min_interval=0.5, sleep=clock.sleep)
    client._clock = clock.monotonic
    client.fetch(APT, "11680", "202501")
    assert clock.sleeps == []  # 첫 요청은 기다리지 않는다
    clock.now += 0.2
    client.fetch(APT, "11680", "202502")
    assert clock.sleeps == [pytest.approx(0.3)]
    clock.now += 1.0
    client.fetch(APT, "11680", "202503")
    assert len(clock.sleeps) == 1  # 이미 충분히 지났다


def test_min_interval_with_real_clock_uses_injected_sleep(sleeps):
    client = make_client(FakeTransport(), sleeps, min_interval=30.0)
    client.fetch(APT, "11680", "202501")
    client.fetch(APT, "11680", "202502")
    assert len(sleeps) == 1 and 29.0 < sleeps[0] <= 30.0


def test_min_interval_zero_never_waits(sleeps):
    client = make_client(FakeTransport(), sleeps)
    for month in ("202501", "202502", "202503"):
        client.fetch(APT, "11680", month)
    assert sleeps == [] and client.request_count == 3


def test_request_count_accumulates_across_fetches(sleeps):
    fake = FakeTransport({(APT, "11680", "202501"): make_items(15)})
    fake.fail(APT, "11680", "202502", HttpResponse(500, b""), times=1)
    client = make_client(fake, sleeps, page_size=10)
    client.fetch(APT, "11680", "202501")  # 2페이지
    client.fetch(APT, "11680", "202502")  # 실패 1회 + 성공 1회
    assert client.request_count == 4 and len(fake.calls) == 4
