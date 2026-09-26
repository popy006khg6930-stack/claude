from __future__ import annotations

import pytest

from silgeorae.collector import (
    FakeTransport,
    HttpResponse,
    MolitClient,
    build_error_xml,
    build_response_xml,
    parse_response,
)
from silgeorae.errors import ApiError, QuotaExceededError, ServiceKeyError
from silgeorae.models import DealType

APT = DealType.APT_SALE
URL = MolitClient.DEFAULT_BASE_URL + "/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"


def params(**kw) -> dict[str, str]:
    base = {"serviceKey": "KEY", "LAWD_CD": "11680", "DEAL_YMD": "202501", "pageNo": "1", "numOfRows": "10"}
    base.update(kw)
    return base


def rows(n: int, prefix: str = "단지") -> list[dict[str, str]]:
    return [{"aptNm": f"{prefix}{i}", "dealAmount": str(i)} for i in range(n)]


# ---------------------------------------------------------------------- XML 생성기
@pytest.mark.parametrize(
    "name",
    ["apt_trade.xml", "apt_rent.xml", "offi_trade.xml", "rh_trade.xml", "sh_trade.xml", "empty.xml", "legacy_apt_trade.xml"],
)
def test_build_response_xml_reproduces_fixtures(read_fixture, name):
    raw = read_fixture(name).decode("utf-8")
    page = parse_response(raw)
    built = build_response_xml(
        page.items,
        total_count=page.total_count,
        page_no=page.page_no,
        num_of_rows=page.num_of_rows,
        result_code=page.result_code,
        result_msg=page.result_msg,
    )
    assert built == raw  # 실제 응답과 같은 구조·들여쓰기


@pytest.mark.parametrize(
    "name, code, auth",
    [
        ("error_quota.xml", "22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"),
        ("error_service_key.xml", "30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"),
    ],
)
def test_build_error_xml_reproduces_fixtures(read_fixture, name, code, auth):
    assert build_error_xml(code, auth) == read_fixture(name).decode("utf-8")


def test_build_response_xml_escapes_and_defaults():
    xml = build_response_xml([{"aptNm": "A&B <C> \"D\"", "floor": -1, "rgstDate": None, "거래금액": " 82,500"}])
    assert "A&amp;B &lt;C&gt;" in xml
    page = parse_response(xml)
    assert page.items == [{"aptNm": 'A&B <C> "D"', "floor": "-1", "rgstDate": "", "거래금액": " 82,500"}]
    assert (page.total_count, page.page_no, page.num_of_rows, page.result_code) == (1, 1, 1, "000")


def test_build_response_xml_empty_and_counts():
    xml = build_response_xml([], total_count=0, num_of_rows=1000)
    assert "<items/>" in xml
    page = parse_response(xml)
    assert page.items == [] and page.num_of_rows == 1000
    page = parse_response(build_response_xml(rows(3), total_count=25, page_no=3, num_of_rows=10))
    assert (len(page.items), page.total_count, page.page_no, page.num_of_rows) == (3, 25, 3, 10)


@pytest.mark.parametrize("bad", ["", "has space", "1abc", "a<b"])
def test_build_response_xml_rejects_bad_tags(bad):
    with pytest.raises(ValueError):
        build_response_xml([{bad: "x"}])


def test_build_error_xml_escapes():
    with pytest.raises(ApiError) as info:
        parse_response(build_error_xml("99", "A & B"))
    assert "A & B" in str(info.value) and info.value.retryable


# ---------------------------------------------------------------------- FakeTransport
def test_paging_reports_true_total():
    fake = FakeTransport({(APT, "11680", "202501"): rows(25)})
    pages = [parse_response(fake(URL, params(pageNo=str(n)), 5.0).body) for n in (1, 2, 3, 4)]
    assert [len(p.items) for p in pages] == [10, 10, 5, 0]
    assert {p.total_count for p in pages} == {25}
    assert [p.page_no for p in pages] == [1, 2, 3, 4]
    assert pages[2].items[0]["aptNm"] == "단지20"
    assert len(fake.calls) == 4 and fake.calls[0] == (URL, params())


def test_unknown_partition_is_empty():
    page = parse_response(FakeTransport()(URL, params(), 5.0).body)
    assert page.items == [] and page.total_count == 0


def test_unknown_service():
    fake = FakeTransport()
    response = fake(MolitClient.DEFAULT_BASE_URL + "/RTMSDataSvcNope/getRTMSDataSvcNope", params(), 5.0)
    with pytest.raises(ApiError) as info:
        parse_response(response.body)
    assert info.value.code == "12" and not info.value.retryable
    assert "NO_OPENAPI_SERVICE_ERROR" in str(info.value)


@pytest.mark.parametrize("key", [None, "", "  ", "abc%2Bdef"])
def test_missing_or_double_encoded_key(key):
    query = params()
    if key is None:
        del query["serviceKey"]
    else:
        query["serviceKey"] = key
    with pytest.raises(ServiceKeyError) as info:
        parse_response(FakeTransport()(URL, query, 5.0).body)
    assert info.value.code == "30"


def test_parameter_errors():
    fake = FakeTransport()
    query = params()
    del query["LAWD_CD"]
    with pytest.raises(ApiError) as info:
        parse_response(fake(URL, query, 5.0).body)
    assert info.value.code == "11"
    with pytest.raises(ApiError) as info:
        parse_response(fake(URL, params(pageNo="x"), 5.0).body)
    assert info.value.code == "10"
    with pytest.raises(ApiError):
        parse_response(fake(URL, params(numOfRows="0"), 5.0).body)


def test_every_deal_type_is_recognized():
    fake = FakeTransport()
    client = MolitClient("KEY", transport=fake, sleep=lambda s: None)
    for deal_type in DealType:
        fake.add(deal_type, "11680", "202501", [{"mark": deal_type.value}])
    for deal_type in DealType:
        assert client.fetch(deal_type, "11680", "202501") == [{"mark": deal_type.value}]


def test_add_replace_and_normalized_keys():
    fake = FakeTransport()
    fake.add("아파트", "11680", "2025-01", rows(2))
    fake.add(APT, " 11680 ", "202501", rows(1, prefix="추가"))
    assert fake.partitions() == [(APT, "11680", "202501")]
    assert [r["aptNm"] for r in fake.items_for(APT, "11680", "202501")] == ["단지0", "단지1", "추가0"]
    fake.replace(APT, "11680", "202501", rows(1, prefix="교체"))
    assert fake.items_for(APT, "11680", "202501") == [{"aptNm": "교체0", "dealAmount": "0"}]
    snapshot = fake.items_for(APT, "11680", "202501")
    snapshot[0]["aptNm"] = "바뀜"
    assert fake.items_for(APT, "11680", "202501")[0]["aptNm"] == "교체0"  # 복사본


def test_fail_times_then_recovers():
    fake = FakeTransport({(APT, "11680", "202501"): rows(3)})
    fake.fail(APT, "11680", "2025-01", HttpResponse(500, b"boom"), times=2)
    assert [fake(URL, params(), 5.0).status for _ in range(3)] == [500, 500, 200]
    assert len(parse_response(fake(URL, params(), 5.0).body).items) == 3


def test_fail_only_affects_its_partition():
    fake = FakeTransport()
    fake.fail(APT, "11680", "202501", HttpResponse(500, b""))
    assert fake(URL, params(DEAL_YMD="202502"), 5.0).status == 200
    assert fake(URL, params(LAWD_CD="11650"), 5.0).status == 200
    rent_url = URL.replace("AptTradeDev", "AptRent")
    assert fake(rent_url, params(), 5.0).status == 200
    assert all(fake(URL, params(), 5.0).status == 500 for _ in range(5))  # times=None → 계속


def test_fail_wildcards_order_and_exceptions():
    fake = FakeTransport()
    quota = HttpResponse(200, build_error_xml("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"))
    fake.fail(None, None, None, TimeoutError("timed out"), times=1)
    fake.fail(None, "11680", None, quota, times=1)
    with pytest.raises(TimeoutError):
        fake(URL, params(), 5.0)
    with pytest.raises(QuotaExceededError):
        parse_response(fake(URL, params(DEAL_YMD="202412"), 5.0).body)
    assert fake(URL, params(), 5.0).status == 200
    fake.fail(None, None, None, HttpResponse(503, b""))
    fake.clear_failures()
    assert fake(URL, params(), 5.0).status == 200
    with pytest.raises(ValueError):
        fake.fail(APT, "11680", "202501", quota, times=0)


def test_end_to_end_with_client_retries():
    sleeps: list[float] = []
    fake = FakeTransport({(APT, "11680", "202501"): rows(2500)})
    fake.fail(APT, "11680", "202501", HttpResponse(502, b"<html>Bad Gateway</html>"), times=1)
    client = MolitClient("KEY", transport=fake, sleep=sleeps.append)
    items = client.fetch(APT, "11680", "202501")
    assert items == rows(2500)
    assert sleeps == [1.0] and client.request_count == 4 and len(fake.calls) == 4
