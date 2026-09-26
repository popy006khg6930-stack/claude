from __future__ import annotations

import json

import pytest

from silgeorae.collector import HttpResponse, build_error_xml, sync_regions
from silgeorae.collector.region_sync import STANREGINCD_URL, parse_stanregincd, regions_from_rows
from silgeorae.errors import ApiError, ConfigError, QuotaExceededError, ServiceKeyError
from silgeorae.regions import RegionTable


def row(code: str, name: str, **extra) -> dict[str, str]:
    data = {
        "region_cd": code, "sido_cd": code[:2], "sgg_cd": code[2:5], "umd_cd": code[5:8], "ri_cd": code[8:],
        "locatjumin_cd": code, "locatjijuk_cd": code, "locatadd_nm": name, "locat_order": "0",
        "locat_rm": "", "locathigh_cd": "", "locallow_nm": name.split()[-1], "adpt_de": "",
    }
    data.update(extra)
    return data


ROWS = [
    row("1100000000", "서울특별시"),  # 시도 → 제외
    row("1111000000", "서울특별시 종로구"),
    row("1111010100", "서울특별시 종로구 청운동"),  # 읍면동 → 제외
    row("1168000000", "서울특별시 강남구"),
    row("1168010600", "서울특별시 강남구 대치동"),
    row("3600000000", "세종특별자치시", sgg_cd="000"),
    row("3611000000", "세종특별자치시"),
    row("3611010100", "세종특별자치시 반곡동"),
    row("4100000000", "경기도"),
    row("4111000000", "경기도 수원시"),
    row("4111100000", "경기도 수원시 장안구"),
    row("4111110100", "경기도 수원시 장안구 파장동"),
    row("4111300000", "경기도 수원시 권선구"),
    row("4115000000", "경기도 의정부시"),
    row("4159000000", "경기도 화성시"),
    row("4159100000", "경기도 화성시 만세구"),  # 합성 예시: 코드표 이후 신설된 일반구
    row("4159300000", "경기도 화성시 효행구"),
    row("4182000000", "경기도 가평군"),
    row("4182025021", "경기도 가평군 가평읍 읍내리"),  # 리 → 제외
    row("4799000000", "경상북도 가상출장소"),  # 출장소 → 제외
    row("5111000000", "강원특별자치도 춘천시"),
    row("2811000000", "인천광역시  중구 "),  # 공백 정리
]
EXPECTED = ["11110", "11680", "28110", "36110", "41110", "41111", "41113", "41150", "41590", "41591", "41593",
            "41820", "51110"]


class StanRegTransport:
    """행정안전부 법정동코드 API 흉내 (페이지당 ``per_page`` 행, totalCount 는 실제 행 수)."""

    def __init__(self, rows, per_page: int, *, with_total: bool = True):
        self.rows = rows
        self.per_page = per_page
        self.with_total = with_total
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        page = int(params["pageNo"])
        chunk = self.rows[(page - 1) * self.per_page: page * self.per_page]
        head = [{"numOfRows": params["numOfRows"], "pageNo": params["pageNo"], "type": "JSON"},
                {"RESULT": {"resultCode": "INFO-0", "resultMsg": "NOMAL SERVICE"}}]
        if self.with_total:
            head.insert(0, {"totalCount": len(self.rows)})
        body = {"StanReginCd": [{"head": head}, {"row": chunk}]}
        return HttpResponse(200, json.dumps(body, ensure_ascii=False).encode("utf-8"))


def sync(transport, service_key="abc%2Bdef%3D%3D", **kw) -> RegionTable:
    kw.setdefault("sleep", lambda seconds: None)
    return sync_regions(service_key, transport=transport, **kw)


def test_sync_regions_two_pages():
    transport = StanRegTransport(ROWS, per_page=12)
    table = sync(transport)
    assert [r.lawd_cd for r in table.all()] == EXPECTED
    assert [(url, p["pageNo"]) for url, p in transport.calls] == [(STANREGINCD_URL, "1"), (STANREGINCD_URL, "2")]
    first = transport.calls[0][1]
    assert first == {"serviceKey": "abc+def==", "type": "json", "pageNo": "1", "numOfRows": "1000", "flag": "Y"}

    suwon = table.get("41110")
    assert (suwon.sido, suwon.sigungu, suwon.is_leaf) == ("경기도", "수원시", False)
    jangan = table.get("41111")
    assert (jangan.sigungu, jangan.parent_cd, jangan.is_leaf) == ("수원시 장안구", "41110", True)
    sejong = table.get("36110")
    assert (sejong.sido, sejong.sigungu, sejong.name) == ("세종특별자치시", "", "세종특별자치시")
    assert table.get("28110").name == "인천광역시 중구"
    assert table.get("41150").is_leaf and table.get("41150").parent_cd == ""
    # 새로 생긴 일반구도 구를 둔 시로 판별 → 이름으로 펼쳐진다
    assert [r.lawd_cd for r in table.resolve("화성시")] == ["41591", "41593"]
    assert [r.lawd_cd for r in table.resolve("경기")] == ["41111", "41113", "41150", "41591", "41593", "41820"]


def test_sync_keeps_history_from_bundled_table():
    table = sync(StanRegTransport(ROWS, per_page=1000))
    chuncheon = table.get("51110")
    assert (chuncheon.former_cd, chuncheon.note) == ("42110", "2023-06-11 코드변경")
    assert [r.lawd_cd for r in table.resolve("42110")] == ["51110"]


def test_sync_without_total_count_stops_on_short_page():
    transport = StanRegTransport(ROWS, per_page=1000, with_total=False)
    assert [r.lawd_cd for r in sync(transport).all()] == EXPECTED
    assert len(transport.calls) == 1


def test_synced_table_round_trip(tmp_path):
    table = sync(StanRegTransport(ROWS, per_page=5))
    path = tmp_path / "sigungu.csv"
    table.save(path)
    assert RegionTable.load(path).all() == table.all()


def test_sync_gateway_errors(read_fixture):
    def serve(body: bytes):
        return lambda url, params, timeout: HttpResponse(200, body)

    with pytest.raises(ServiceKeyError) as info:
        sync(serve(read_fixture("error_service_key.xml")))
    assert info.value.code == "30" and "행정안전부_행정표준코드_법정동코드" in str(info.value)
    with pytest.raises(QuotaExceededError):
        sync(serve(build_error_xml("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR").encode()))


def test_sync_retries_transient_errors():
    good = StanRegTransport(ROWS, per_page=1000)
    responses = [HttpResponse(503, b"Service Unavailable")]
    sleeps: list[float] = []

    def transport(url, params, timeout):
        return responses.pop(0) if responses else good(url, params, timeout)

    table = sync(transport, sleep=sleeps.append)
    assert len(table) == len(EXPECTED) and sleeps == [1.0]


def test_sync_with_no_regions_is_an_error():
    with pytest.raises(ApiError, match="시군구"):
        sync(StanRegTransport([row("1100000000", "서울특별시")], per_page=1000))


def test_sync_requires_key():
    with pytest.raises(ConfigError, match="인증키"):
        sync_regions("", transport=StanRegTransport(ROWS, per_page=1000))


# ---------------------------------------------------------------------- 응답 해석
def test_parse_stanregincd_variants():
    body = {"StanReginCd": [{"head": {"totalCount": "2"}}, {"row": row("1111000000", "서울특별시 종로구")}]}
    rows, total = parse_stanregincd(json.dumps(body, ensure_ascii=False))
    assert total == 2 and rows[0]["locatadd_nm"] == "서울특별시 종로구"
    nodata = {"StanReginCd": [{"head": [{"RESULT": {"resultCode": "INFO-3", "resultMsg": "데이터 없음"}}]}]}
    assert parse_stanregincd(json.dumps(nodata, ensure_ascii=False).encode()) == ([], 0)
    assert parse_stanregincd(json.dumps({"RESULT": {"resultCode": "INFO-200", "resultMsg": "없음"}})) == ([], 0)
    with pytest.raises(ServiceKeyError):
        parse_stanregincd(json.dumps({"RESULT": {"resultCode": "ERROR-290", "resultMsg": "인증키"}}))
    with pytest.raises(ApiError) as info:
        parse_stanregincd(json.dumps({"StanReginCd": [{"head": [{"RESULT": {"resultCode": "ERROR-500"}}]}]}))
    assert info.value.retryable
    with pytest.raises(ApiError) as info:
        parse_stanregincd(b"<html>maintenance</html>")
    assert info.value.retryable


def test_regions_from_rows_derives_missing_fields():
    rows = [
        {"region_cd": "4113000000", "locatadd_nm": "경기도 성남시"},
        {"region_cd": "4113500000", "locatadd_nm": "경기도 성남시 분당구"},
        {"region_cd": "4113510300", "locatadd_nm": "경기도 성남시 분당구 정자동"},
        {"region_cd": "bad", "locatadd_nm": "잘못된 행"},
        {"region_cd": "4115000000", "locatadd_nm": ""},
    ]
    regions = regions_from_rows(rows)
    assert [(r.lawd_cd, r.sigungu, r.parent_cd, r.is_leaf) for r in regions] == [
        ("41130", "성남시", "", False),
        ("41135", "성남시 분당구", "41130", True),
    ]
