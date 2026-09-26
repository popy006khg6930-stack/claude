"""오프라인 테스트·데모용 가짜 국토교통부 API.

``FakeTransport`` 는 ``MolitClient(transport=...)`` 에 끼워 쓰는 ``Transport`` 구현으로,
실제 API 와 같은 XML(``tests/fixtures/apt_trade.xml`` 형식)을 페이지 단위로 돌려준다::

    fake = FakeTransport({(DealType.APT_SALE, "11680", "202501"): [{"aptNm": "한빛", ...}]})
    client = MolitClient("DEMO-KEY", transport=fake)
    client.fetch(DealType.APT_SALE, "11680", "202501")   # → [{"aptNm": "한빛", ...}]
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from xml.sax.saxutils import escape

from ..models import DealType
from ..utils import parse_ym
from .http import HttpResponse

_XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_TAG_RE = re.compile(r"^[^\W\d][\w.\-]*$")  # XML 이름 (한글 태그 포함)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # XML 1.0 에서 쓸 수 없는 문자
_XML_HEADERS = {"Content-Type": "text/xml;charset=UTF-8"}

Partition = tuple[DealType, str, str]


def build_response_xml(
    items: Iterable[Mapping[str, Any]],
    *,
    total_count: int | None = None,
    page_no: int = 1,
    num_of_rows: int | None = None,
    result_code: str = "000",
    result_msg: str = "OK",
) -> str:
    """정상 응답 XML (실제 API 와 같은 구조). 값은 XML 이스케이프, item 이 없으면 ``<items/>``."""
    rows = [dict(item) for item in items]
    total = len(rows) if total_count is None else int(total_count)
    per_page = len(rows) if num_of_rows is None else int(num_of_rows)
    lines = [
        _XML_DECLARATION,
        "<response>",
        "  <header>",
        f"    <resultCode>{_escape(result_code)}</resultCode>",
        f"    <resultMsg>{_escape(result_msg)}</resultMsg>",
        "  </header>",
        "  <body>",
    ]
    if rows:
        lines.append("    <items>")
        for row in rows:
            lines.append("      <item>")
            for key, value in row.items():
                tag = _check_tag(key)
                lines.append(f"        <{tag}>{_escape(value)}</{tag}>")
            lines.append("      </item>")
        lines.append("    </items>")
    else:
        lines.append("    <items/>")
    lines += [
        f"    <numOfRows>{per_page}</numOfRows>",
        f"    <pageNo>{int(page_no)}</pageNo>",
        f"    <totalCount>{total}</totalCount>",
        "  </body>",
        "</response>",
    ]
    return "\n".join(lines) + "\n"


def build_error_xml(reason_code: str, auth_msg: str, err_msg: str = "SERVICE ERROR") -> str:
    """게이트웨이 오류 응답 XML (``OpenAPI_ServiceResponse``)."""
    return (
        "<OpenAPI_ServiceResponse>\n"
        "  <cmmMsgHeader>\n"
        f"    <errMsg>{_escape(err_msg)}</errMsg>\n"
        f"    <returnAuthMsg>{_escape(auth_msg)}</returnAuthMsg>\n"
        f"    <returnReasonCode>{_escape(reason_code)}</returnReasonCode>\n"
        "  </cmmMsgHeader>\n"
        "</OpenAPI_ServiceResponse>\n"
    )


@dataclass
class _Failure:
    deal_type: DealType | None  # None = 모든 유형
    lawd_cd: str | None
    deal_ym: str | None
    response: HttpResponse | BaseException
    remaining: int | None  # None = 계속

    def matches(self, deal_type: DealType, lawd_cd: str, deal_ym: str) -> bool:
        return (
            (self.deal_type is None or self.deal_type is deal_type)
            and (self.lawd_cd is None or self.lawd_cd == lawd_cd)
            and (self.deal_ym is None or self.deal_ym == deal_ym)
        )


class FakeTransport:
    """가짜 실거래가 API (``Transport`` 구현). 네트워크 없이 테스트·``demo`` 에 쓴다.

    * ``data`` 는 ``(DealType, 시군구코드, 연월) → 원본 item dict 목록``.
    * 요청 URL 의 서비스명으로 유형을 알아내고, ``pageNo``·``numOfRows`` 로 잘라 실제 ``totalCount`` 를 알린다.
    * 모르는 서비스 → 오류 코드 12, 인증키 없음(또는 ``%`` 가 남은 이중 인코딩 키) → 오류 코드 30.
    * ``fail()`` 로 특정 파티션(``None`` 은 전체)에 오류 응답·예외를 끼워 넣는다.
    * ``calls`` 에 모든 요청 ``(url, params)`` 가 쌓인다.
    """

    def __init__(self, data: Mapping[Partition, Sequence[Mapping[str, Any]]] | None = None) -> None:
        self._data: dict[Partition, list[dict[str, Any]]] = {}
        self._failures: list[_Failure] = []
        self.calls: list[tuple[str, dict[str, str]]] = []
        for (deal_type, lawd_cd, deal_ym), items in (data or {}).items():
            self.add(deal_type, lawd_cd, deal_ym, items)

    # ------------------------------------------------------------------ 데이터
    def add(
        self, deal_type: DealType, lawd_cd: str, deal_ym: str, items: Iterable[Mapping[str, Any]]
    ) -> None:
        """파티션에 item 을 덧붙인다."""
        key = _partition(deal_type, lawd_cd, deal_ym)
        self._data.setdefault(key, []).extend(dict(item) for item in items)

    def replace(
        self, deal_type: DealType, lawd_cd: str, deal_ym: str, items: Iterable[Mapping[str, Any]]
    ) -> None:
        """파티션 내용을 통째로 바꾼다 (해제·정정 반영 흉내 등)."""
        self._data[_partition(deal_type, lawd_cd, deal_ym)] = [dict(item) for item in items]

    def items_for(self, deal_type: DealType, lawd_cd: str, deal_ym: str) -> list[dict[str, Any]]:
        """파티션에 들어 있는 item (복사본)."""
        return [dict(item) for item in self._data.get(_partition(deal_type, lawd_cd, deal_ym), [])]

    def partitions(self) -> list[Partition]:
        """데이터가 들어 있는 파티션 목록."""
        return list(self._data)

    def fail(
        self,
        deal_type: DealType | None,
        lawd_cd: str | None,
        deal_ym: str | None,
        response: HttpResponse | BaseException,
        times: int | None = None,
    ) -> None:
        """다음 ``times`` 번(None = 계속) 그 파티션 요청에 ``response`` 를 돌려준다.

        ``response`` 가 예외 객체면 대신 던진다 (예: ``TimeoutError()``). 인자에 None 을 주면 모두에 해당.
        여러 번 등록하면 등록 순서대로 쓴다.
        """
        if times is not None and int(times) < 1:
            raise ValueError("times 는 1 이상이거나 None 이어야 합니다.")
        self._failures.append(
            _Failure(
                None if deal_type is None else _deal_type(deal_type),
                None if lawd_cd is None else str(lawd_cd).strip(),
                None if deal_ym is None else parse_ym(deal_ym),
                response,
                None if times is None else int(times),
            )
        )

    def clear_failures(self) -> None:
        """``fail()`` 로 등록한 오류를 모두 지운다."""
        self._failures.clear()

    # ------------------------------------------------------------------ Transport
    def __call__(self, url: str, params: Mapping[str, str], timeout: float) -> HttpResponse:
        query = {str(k): str(v) for k, v in params.items()}
        self.calls.append((url, query))
        deal_type = _deal_type_from_url(url)
        if deal_type is None:
            return _xml(build_error_xml("12", "NO_OPENAPI_SERVICE_ERROR"))
        key = query.get("serviceKey", "").strip()
        if not key or "%" in key:  # 이중 인코딩된 Encoding 키도 실제 포털처럼 거부
            return _xml(build_error_xml("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"))
        lawd_cd = query.get("LAWD_CD", "").strip()
        deal_ym = query.get("DEAL_YMD", "").strip()
        failure = self._take_failure(deal_type, lawd_cd, deal_ym)
        if isinstance(failure, BaseException):
            raise failure
        if failure is not None:
            return failure
        if not lawd_cd or not deal_ym:
            return _xml(build_response_xml([], result_code="11", result_msg="NO_MANDATORY_REQUEST_PARAMETERS_ERROR"))
        try:
            page_no = int(query.get("pageNo", "1"))
            num_of_rows = int(query.get("numOfRows", "10"))
        except ValueError:
            page_no = num_of_rows = 0
        if page_no < 1 or num_of_rows < 1:
            return _xml(build_response_xml([], result_code="10", result_msg="INVALID_REQUEST_PARAMETER_ERROR"))
        items = self._data.get((deal_type, lawd_cd, deal_ym), [])
        start = (page_no - 1) * num_of_rows
        chunk = items[start:start + num_of_rows]
        return _xml(build_response_xml(chunk, total_count=len(items), page_no=page_no, num_of_rows=num_of_rows))

    def _take_failure(
        self, deal_type: DealType, lawd_cd: str, deal_ym: str
    ) -> HttpResponse | BaseException | None:
        for failure in self._failures:
            if failure.matches(deal_type, lawd_cd, deal_ym):
                if failure.remaining is not None:
                    failure.remaining -= 1
                    if failure.remaining <= 0:
                        self._failures.remove(failure)
                return failure.response
        return None


# ---------------------------------------------------------------------- 도우미
def _deal_type(value: DealType | str) -> DealType:
    return value if isinstance(value, DealType) else DealType.parse(str(value))


def _partition(deal_type: DealType | str, lawd_cd: str, deal_ym: str) -> Partition:
    return _deal_type(deal_type), str(lawd_cd).strip(), parse_ym(deal_ym)


def _deal_type_from_url(url: str) -> DealType | None:
    segments = {s for s in urllib.parse.urlsplit(url).path.split("/") if s}
    for deal_type in DealType:
        service = deal_type.api_service
        if service in segments or f"get{service}" in segments:
            return deal_type
    return None


def _xml(text: str) -> HttpResponse:
    return HttpResponse(200, text.encode("utf-8"), dict(_XML_HEADERS))


def _escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return escape(_CONTROL_RE.sub("", text))


def _check_tag(key: Any) -> str:
    tag = str(key)
    if not _TAG_RE.match(tag):
        raise ValueError(f"XML 태그로 쓸 수 없는 필드 이름입니다: {key!r}")
    return tag
