"""행정안전부 법정동코드 API 로 최신 시군구 코드표를 만든다 (``silgeorae regions --sync``).

요청: ``GET https://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList``
``?serviceKey=…&type=json&pageNo=1&numOfRows=1000&flag=Y`` (flag=Y: 현존 코드만)

응답(JSON) — 공개 문서 기준이며 이 환경에서는 실제 호출로 확인하지 못해 방어적으로 해석한다::

    {"StanReginCd": [
        {"head": [{"totalCount": 20551},
                  {"numOfRows": "1000", "pageNo": "1", "type": "JSON"},
                  {"RESULT": {"resultCode": "INFO-0", "resultMsg": "NOMAL SERVICE"}}]},
        {"row": [{"region_cd": "1111000000", "sido_cd": "11", "sgg_cd": "110",
                  "umd_cd": "000", "ri_cd": "00", "locatadd_nm": "서울특별시 종로구",
                  "locallow_nm": "종로구", ...}, ...]}]}

* ``head`` 는 목록 대신 dict 로 와도 되고, ``RESULT`` 가 ``INFO-0`` 이 아닌 ``INFO-*`` 면 데이터 없음으로 본다.
* 인증키·트래픽 오류는 실거래가 API 와 같은 게이트웨이 XML(``OpenAPI_ServiceResponse``)로 온다
  → ``parse_response`` 의 오류 분류(``ServiceKeyError`` 등)를 그대로 쓴다.
* 시군구 행: ``umd_cd == "000"``, ``ri_cd == "00"``, ``sgg_cd != "000"`` (시도 행 제외), 출장소 제외.
  ``lawd_cd = region_cd[:5]``, 시도 = ``locatadd_nm`` 첫 단어, 시군구 = 나머지 (세종은 "").
* 구를 둔 시: 끝자리가 0 인 코드이면서 앞 4자리가 같고 이름이 "그 시 이름 + 공백"으로 시작하는
  다른 코드가 있으면 상위 시(``is_leaf=0``), 그 구들은 ``parent_cd`` 를 갖는다.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any, Iterable, Mapping

from ..errors import ApiError, QuotaExceededError, ServiceKeyError
from ..regions import Region, RegionTable
from .client import MolitClient, normalize_service_key, quota_hint, service_key_hint
from .http import Transport
from .parser import api_error, normalize_code, parse_response

logger = logging.getLogger(__name__)

STANREGINCD_URL = "https://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList"
STANREGINCD_TITLE = "행정안전부_행정표준코드_법정동코드"
PAGE_SIZE = 1000
_MAX_PAGES = 500


def sync_regions(service_key: str, *, transport: Transport | None = None, **client_options: Any) -> RegionTable:
    """법정동코드 API 전체를 읽어 시군구 코드표(``RegionTable``)를 만든다.

    번들 코드표에 같은 코드가 있으면 ``former_cd``·``note`` 를 이어받는다.
    ``client_options`` 는 내부 ``MolitClient`` 에 넘긴다 (timeout, max_retries, backoff, sleep, min_interval).
    """
    key = normalize_service_key(service_key)
    client = MolitClient(key, transport=transport, **client_options)
    rows: list[Mapping[str, Any]] = []
    total: int | None = None
    page_no = 1
    while True:
        params = {"serviceKey": key, "type": "json", "pageNo": str(page_no), "numOfRows": str(PAGE_SIZE), "flag": "Y"}
        try:
            page_rows, page_total = client.request(STANREGINCD_URL, params, parse=parse_stanregincd)
        except ServiceKeyError as exc:
            raise ServiceKeyError(f"{exc} {service_key_hint(STANREGINCD_TITLE)}", code=exc.code) from exc
        except QuotaExceededError as exc:
            raise QuotaExceededError(f"{exc} {quota_hint(STANREGINCD_TITLE)}", code=exc.code) from exc
        rows.extend(page_rows)
        if page_total is not None:
            total = page_total
        if not page_rows:
            break
        if total is not None:
            if len(rows) >= total:
                break
        elif len(page_rows) < PAGE_SIZE:  # totalCount 를 모르면 덜 찬 페이지가 마지막
            break
        if page_no >= _MAX_PAGES:
            logger.warning("법정동코드 API: %d페이지에서 중단합니다.", page_no)
            break
        page_no += 1
    if total is not None and len(rows) < total:
        logger.warning("법정동코드 API: 전체 %d행 중 %d행만 받았습니다.", total, len(rows))

    regions = regions_from_rows(rows)
    if not regions:
        raise ApiError(
            "법정동코드 API 응답에서 시군구를 하나도 찾지 못했습니다. 잠시 후 다시 시도하세요.", retryable=False
        )
    regions = _inherit_history(regions)
    table = RegionTable(regions)
    logger.info(
        "법정동코드 %d행 → 시군구 %d곳 (조회 단위 %d곳, API 호출 %d회)",
        len(rows), len(table), len(table.all(leaves_only=True)), client.request_count,
    )
    return table


def parse_stanregincd(body: bytes | str) -> tuple[list[dict[str, str]], int | None]:
    """법정동코드 API 응답 1페이지 → (행 목록, totalCount 또는 None). 오류면 ``ApiError`` 계열."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    text = text.strip().lstrip("\ufeff").strip()
    if not text.startswith(("{", "[")):
        parse_response(text)  # 게이트웨이 오류 XML·HTML → 알맞은 예외
        raise ApiError("법정동코드 API 응답 형식이 예상과 다릅니다 (JSON 아님).", retryable=False)
    try:
        data = json.loads(text)
    except ValueError:
        raise ApiError("법정동코드 API 응답(JSON)을 해석할 수 없습니다.", retryable=True) from None
    sections = data.get("StanReginCd") if isinstance(data, dict) else data
    if not isinstance(sections, list):
        if isinstance(data, dict) and isinstance(data.get("RESULT"), Mapping):  # 데이터 없음 등 단독 RESULT
            _check_result(data["RESULT"])
            return [], 0
        parse_response(text)  # response/header 또는 OpenAPI_ServiceResponse 형식의 오류
        raise ApiError("법정동코드 API 응답 형식이 예상과 다릅니다 (StanReginCd 없음).", retryable=False)

    head: list[Any] = []
    rows: list[Any] = []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        if "head" in section:
            value = section["head"]
            head.extend(value if isinstance(value, list) else [value])
        if "row" in section:
            value = section["row"]
            rows.extend(value if isinstance(value, list) else [value])

    total: int | None = None
    for entry in head:
        if not isinstance(entry, Mapping):
            continue
        if isinstance(entry.get("RESULT"), Mapping) and not _check_result(entry["RESULT"]):
            return [], 0
        if "totalCount" in entry:
            try:
                total = int(str(entry["totalCount"]).strip())
            except ValueError:
                total = None
    clean = [
        {str(k): "" if v is None else str(v) for k, v in row.items()} for row in rows if isinstance(row, Mapping)
    ]
    return clean, total


def regions_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[Region]:
    """법정동코드 행들 → 시군구 ``Region`` 목록 (코드 순, 구를 둔 시 판별 포함)."""
    names: dict[str, tuple[str, str]] = {}
    for row in rows:
        region_cd = _field(row, "region_cd")
        if len(region_cd) != 10 or not region_cd.isdigit():
            continue
        sgg_cd = _field(row, "sgg_cd") or region_cd[2:5]
        umd_cd = _field(row, "umd_cd") or region_cd[5:8]
        ri_cd = _field(row, "ri_cd") or region_cd[8:10]
        if sgg_cd == "000" or umd_cd != "000" or ri_cd != "00":
            continue
        full_name = " ".join(_field(row, "locatadd_nm").split())
        if not full_name or "출장" in full_name:
            continue
        sido, _, sigungu = full_name.partition(" ")
        names[region_cd[:5]] = (sido, sigungu)

    codes = sorted(names)
    parent_of: dict[str, str] = {}
    parents: set[str] = set()
    for code in codes:
        sigungu = names[code][1]
        if not code.endswith("0") or not sigungu:
            continue
        prefix = sigungu + " "
        children = [c for c in codes if c != code and c[:4] == code[:4] and names[c][1].startswith(prefix)]
        if children:
            parents.add(code)
            for child in children:
                parent_of[child] = code
    return [
        Region(
            lawd_cd=code,
            sido=names[code][0],
            sigungu=names[code][1],
            parent_cd=parent_of.get(code, ""),
            is_leaf=code not in parents,
        )
        for code in codes
    ]


def _check_result(result: Mapping[str, Any]) -> bool:
    """``RESULT`` 확인: 정상이면 True, 데이터 없음이면 False, 오류면 예외."""
    code = str(result.get("resultCode", "")).strip()
    msg = str(result.get("resultMsg", "")).strip()
    upper = code.upper()
    if not upper or upper.startswith("INFO-0") or normalize_code(code) == "00":
        return True
    if upper.startswith("INFO-") or normalize_code(code) == "03":
        return False
    if upper.startswith("ERROR-"):
        number = upper[len("ERROR-"):]
        if number == "290":  # 인증키 오류
            raise ServiceKeyError(f"법정동코드 API 인증키 오류 [{code} {msg}]", code=code)
        raise ApiError(f"법정동코드 API 오류 [{code} {msg}]", code=code, retryable=number.startswith(("5", "6")))
    raise api_error(code, msg)


def _inherit_history(regions: list[Region]) -> list[Region]:
    """번들 코드표의 옛 코드·비고를 같은 코드에 이어 붙인다."""
    try:
        bundled = RegionTable.load()
    except Exception:  # 번들 표를 못 읽어도 동기화 결과는 쓸 수 있다
        logger.debug("번들 코드표를 읽지 못해 옛 코드 정보를 잇지 않습니다.", exc_info=True)
        return regions
    result = []
    for region in regions:
        old = bundled.get(region.lawd_cd)
        if old is not None and (old.former_cd or old.note) and not (region.former_cd or region.note):
            region = dataclasses.replace(region, former_cd=old.former_cd, note=old.note)
        result.append(region)
    return result


def _field(row: Mapping[str, Any], name: str) -> str:
    value = row.get(name)
    return "" if value is None else str(value).strip()
