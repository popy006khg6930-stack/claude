from __future__ import annotations

import json

import pytest

from silgeorae.collector import ParsedPage, build_error_xml, build_response_xml, parse_response
from silgeorae.collector.parser import error_from_body, normalize_code
from silgeorae.errors import ApiError, QuotaExceededError, ServiceKeyError

# docs/ARCHITECTURE.md §2.1 의 아파트 매매(상세) 필드
APT_SALE_FIELDS = {
    "sggCd", "umdCd", "landCd", "bonbun", "bubun", "roadNm", "roadNmSggCd", "roadNmCd", "roadNmSeq",
    "roadNmbCd", "roadNmBonbun", "roadNmBubun", "umdNm", "aptNm", "jibun", "excluUseAr", "dealYear",
    "dealMonth", "dealDay", "dealAmount", "floor", "buildYear", "aptSeq", "cdealType", "cdealDay",
    "dealingGbn", "estateAgentSggNm", "rgstDate", "aptDong", "slerGbn", "buyerGbn", "landLeaseholdGbn",
}


# ---------------------------------------------------------------------- 정상 응답
def test_parse_apt_trade_new_format(read_fixture):
    page = parse_response(read_fixture("apt_trade.xml"))
    assert isinstance(page, ParsedPage)
    assert (page.total_count, page.page_no, page.num_of_rows) == (3, 1, 1000)
    assert (page.result_code, page.result_msg) == ("000", "OK")
    first, second, third = page.items
    assert set(first) == APT_SALE_FIELDS
    assert first["aptNm"] == "한빛마을1단지" and first["aptSeq"] == "11680-9001"
    assert first["dealAmount"] == "285,000"
    assert second["dealAmount"] == "  312,000"  # 원문 그대로 (공백 제거는 정제팀)
    assert (second["cdealType"], second["cdealDay"]) == ("O", "25.01.28")
    assert first["cdealType"] == " "
    assert second["estateAgentSggNm"] == "서울 강남구, 서울 서초구"
    assert (third["umdNm"], third["jibun"]) == ("개포동", "12-3")


def test_parse_legacy_korean_tags(read_fixture):
    page = parse_response(read_fixture("legacy_apt_trade.xml"))
    assert (page.result_code, page.result_msg) == ("00", "NORMAL SERVICE.")
    assert (page.total_count, page.num_of_rows) == (1, 10)
    [item] = page.items
    assert item["거래금액"] == "    82,500"
    assert item["법정동"] == " 사직동"
    assert item["아파트"] == "샘플힐스"
    assert (item["년"], item["월"], item["일"]) == ("2021", "12", "6")
    assert item["지역코드"] == "11110"


@pytest.mark.parametrize(
    "name, count, field, value",
    [
        ("apt_rent.xml", 3, "deposit", "150,000"),
        ("offi_trade.xml", 1, "offiNm", "은하수타워"),
        ("rh_trade.xml", 1, "floor", "-1"),
        ("sh_trade.xml", 1, "jibun", "3**"),
    ],
)
def test_parse_other_fixtures(read_fixture, name, count, field, value):
    page = parse_response(read_fixture(name))
    assert len(page.items) == page.total_count == count
    assert page.items[0][field] == value


def test_parse_empty(read_fixture):
    page = parse_response(read_fixture("empty.xml"))
    assert page.items == [] and page.total_count == 0 and page.result_code == "000"


def test_accepts_str_bom_and_whitespace(read_fixture):
    raw = read_fixture("apt_trade.xml")
    expected = parse_response(raw)
    assert parse_response(raw.decode("utf-8")) == expected
    assert parse_response(b"\xef\xbb\xbf\r\n  " + raw) == expected
    assert parse_response("\ufeff" + raw.decode("utf-8") + "\n\n") == expected


@pytest.mark.parametrize("code", ["00", "000", "0"])
def test_ok_codes(code):
    page = parse_response(build_response_xml([{"aptNm": "가"}], result_code=code))
    assert page.items == [{"aptNm": "가"}] and page.result_code == code


@pytest.mark.parametrize("code", ["03", "003"])
def test_nodata_is_an_empty_page(code):
    page = parse_response(build_response_xml([], total_count=0, result_code=code, result_msg="NODATA_ERROR"))
    assert page.items == [] and page.total_count == 0 and page.result_code == code


def test_missing_counts_fall_back_to_items():
    body = (
        "<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>"
        "<body><items><item><a>1</a></item><item><a>2</a></item></items></body></response>"
    )
    page = parse_response(body)
    assert (page.total_count, page.page_no, page.num_of_rows) == (2, 1, 2)


# ---------------------------------------------------------------------- 오류 응답
def test_quota_fixture(read_fixture):
    with pytest.raises(QuotaExceededError) as info:
        parse_response(read_fixture("error_quota.xml"))
    assert info.value.code == "22" and info.value.retryable is False
    assert "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR" in str(info.value)
    assert "한도" in str(info.value)


def test_service_key_fixture(read_fixture):
    with pytest.raises(ServiceKeyError) as info:
        parse_response(read_fixture("error_service_key.xml"))
    assert info.value.code == "30" and info.value.retryable is False
    assert "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in str(info.value)
    assert "인증키" in str(info.value)


CODE_CASES = [
    ("20", ServiceKeyError, False), ("21", ServiceKeyError, False), ("30", ServiceKeyError, False),
    ("31", ServiceKeyError, False), ("32", ServiceKeyError, False), ("33", ServiceKeyError, False),
    ("22", QuotaExceededError, False),
    ("01", ApiError, True), ("02", ApiError, True), ("04", ApiError, True), ("05", ApiError, True),
    ("99", ApiError, True),
    ("10", ApiError, False), ("11", ApiError, False), ("12", ApiError, False), ("77", ApiError, False),
]


@pytest.mark.parametrize("code, exc_type, retryable", CODE_CASES)
def test_gateway_reason_code_mapping(code, exc_type, retryable):
    with pytest.raises(ApiError) as info:
        parse_response(build_error_xml(code, "SOME_AUTH_MSG"))
    assert type(info.value) is exc_type
    assert info.value.code == code and info.value.retryable is retryable
    assert "SOME_AUTH_MSG" in str(info.value) and code in str(info.value)


@pytest.mark.parametrize("code, exc_type, retryable", CODE_CASES)
def test_header_result_code_mapping(code, exc_type, retryable):
    with pytest.raises(ApiError) as info:
        parse_response(build_response_xml([], result_code=code, result_msg="SOME_RESULT_MSG"))
    assert type(info.value) is exc_type
    assert info.value.code == code and info.value.retryable is retryable
    assert "SOME_RESULT_MSG" in str(info.value)


@pytest.mark.parametrize(
    "code, expected", [("000", "00"), ("0", "00"), ("003", "03"), ("030", "30"), (" 22 ", "22"), ("info-0", "INFO-0")]
)
def test_normalize_code(code, expected):
    assert normalize_code(code) == expected


def test_three_digit_error_codes_are_normalized():
    with pytest.raises(ServiceKeyError) as info:
        parse_response(build_response_xml([], result_code="030", result_msg="SERVICE_KEY_IS_NOT_REGISTERED_ERROR"))
    assert info.value.code == "30"


# ---------------------------------------------------------------------- JSON
def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def test_parse_json_item_list():
    body = {
        "response": {
            "header": {"resultCode": "000", "resultMsg": "OK"},
            "body": {
                "items": {"item": [{"aptNm": "한빛", "dealYear": 2025, "excluUseAr": 84.97, "rgstDate": None},
                                   {"aptNm": "푸른숲", "dealAmount": " 98,500"}]},
                "numOfRows": 10, "pageNo": "2", "totalCount": "12",
            },
        }
    }
    page = parse_response(_json(body))
    assert page.items == [
        {"aptNm": "한빛", "dealYear": "2025", "excluUseAr": "84.97", "rgstDate": ""},
        {"aptNm": "푸른숲", "dealAmount": " 98,500"},
    ]
    assert (page.total_count, page.page_no, page.num_of_rows, page.result_code) == (12, 2, 10, "000")


def test_parse_json_single_item_and_empty_items():
    single = {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": {"aptNm": "한빛"}}, "totalCount": 1}}}
    assert parse_response(_json(single)).items == [{"aptNm": "한빛"}]
    empty = {"response": {"header": {"resultCode": "000", "resultMsg": "OK"}, "body": {"items": "", "totalCount": 0}}}
    page = parse_response(_json(empty).decode("utf-8"))
    assert page.items == [] and page.total_count == 0


def test_parse_json_errors():
    with pytest.raises(ServiceKeyError) as info:
        parse_response(_json({"response": {"header": {"resultCode": "30", "resultMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}}}))
    assert info.value.code == "30"
    gateway = {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"errMsg": "SERVICE ERROR", "returnReasonCode": "22",
                                                            "returnAuthMsg": "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"}}}
    with pytest.raises(QuotaExceededError):
        parse_response(_json(gateway))
    nodata = {"response": {"header": {"resultCode": "03", "resultMsg": "NODATA_ERROR"}, "body": {"items": ""}}}
    assert parse_response(_json(nodata)).items == []


# ---------------------------------------------------------------------- 해석 불가 본문
@pytest.mark.parametrize(
    "body",
    [
        b"<html><head><title>502 Bad Gateway</title></head><body><h1>502 Bad Gateway</h1>nginx</body></html>",
        "Service Unavailable",
        b"<response><header>",  # 잘린 XML
        b"<rss><channel/></rss>",
        b"   ",
        b"",
        "{not json",
        "[1, 2]",
    ],
)
def test_unparseable_body_is_retryable(body):
    with pytest.raises(ApiError) as info:
        parse_response(body)
    assert type(info.value) is ApiError
    assert info.value.retryable is True and info.value.code == ""


def test_unparseable_message_has_snippet():
    with pytest.raises(ApiError, match="502 Bad Gateway"):
        parse_response(b"<html><head><title>502 Bad Gateway</title></head><body></body></html>")


@pytest.mark.parametrize(
    "token, exc_type, code",
    [
        ("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", ServiceKeyError, "30"),
        ("SERVICE_ACCESS_DENIED_ERROR", ServiceKeyError, "20"),
        ("DEADLINE_HAS_EXPIRED_ERROR", ServiceKeyError, "31"),
        ("UNREGISTERED_IP_ERROR", ServiceKeyError, "32"),
        ("LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR", QuotaExceededError, "22"),
    ],
)
def test_auth_token_in_garbage_body(token, exc_type, code):
    for body in (f"<html><body><p>{token}</p></body></html>", f"ERROR: {token}", f"<p>{token}".encode()):
        with pytest.raises(exc_type) as info:
            parse_response(body)
        assert info.value.code == code and info.value.retryable is False


def test_error_from_body(read_fixture):
    assert isinstance(error_from_body(read_fixture("error_quota.xml")), QuotaExceededError)
    assert error_from_body(read_fixture("apt_trade.xml")) is None
    assert error_from_body(b"<html>Internal Server Error</html>") is None
    assert isinstance(error_from_body(b"Unauthorized: SERVICE_KEY_IS_NOT_REGISTERED_ERROR"), ServiceKeyError)
