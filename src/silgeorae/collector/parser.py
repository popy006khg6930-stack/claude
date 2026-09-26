"""API 응답(XML·JSON) 해석과 오류 코드 분류.

정상 응답 (``header/resultCode`` = ``000``, 구버전 ``00``)::

    <response><header><resultCode>000</resultCode>…</header>
      <body><items><item>…</item></items><numOfRows/><pageNo/><totalCount/></body></response>

게이트웨이 오류 (인증키·트래픽 등)::

    <OpenAPI_ServiceResponse><cmmMsgHeader>
      <errMsg/><returnAuthMsg/><returnReasonCode/></cmmMsgHeader></OpenAPI_ServiceResponse>

``resultCode`` 와 ``returnReasonCode`` 는 같은 표(공공데이터포털 표준 오류 코드)로 분류한다.
item 값은 원문 그대로 둔다 (앞뒤 공백 제거·숫자 변환은 정제팀 몫).
"""

from __future__ import annotations

import codecs
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..errors import ApiError, QuotaExceededError, ServiceKeyError

OK_CODES = frozenset({"00"})  # 정규화한 코드 ("000", "0" → "00")
NODATA_CODES = frozenset({"03"})  # 데이터 없음 → 빈 페이지 (오류 아님)
QUOTA_CODES = frozenset({"22"})
SERVICE_KEY_CODES = frozenset({"20", "21", "30", "31", "32", "33"})
RETRYABLE_CODES = frozenset({"01", "02", "04", "05", "99"})

# 공공데이터포털 표준 오류 코드 → (영문 이름, 설명)
CODE_INFO: dict[str, tuple[str, str]] = {
    "00": ("NORMAL_SERVICE", "정상"),
    "01": ("APPLICATION_ERROR", "API 서버 내부 오류입니다."),
    "02": ("DB_ERROR", "API 서버 데이터베이스 오류입니다."),
    "03": ("NODATA_ERROR", "데이터가 없습니다."),
    "04": ("HTTP_ERROR", "API 서버 HTTP 오류입니다."),
    "05": ("SERVICETIME_OUT", "API 서버 연결 시간이 초과되었습니다."),
    "10": ("INVALID_REQUEST_PARAMETER_ERROR", "요청 파라미터가 잘못되었습니다."),
    "11": ("NO_MANDATORY_REQUEST_PARAMETERS_ERROR", "필수 요청 파라미터가 없습니다."),
    "12": ("NO_OPENAPI_SERVICE_ERROR", "해당 오픈API 서비스가 없거나 폐기되었습니다 (서비스명·주소 확인)."),
    "20": ("SERVICE_ACCESS_DENIED_ERROR", "서비스 접근이 거부되었습니다 (해당 API 활용신청·승인 필요)."),
    "21": ("TEMPORARILY_DISABLE_THE_SERVICEKEY_ERROR", "일시적으로 사용할 수 없는 인증키입니다."),
    "22": ("LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR", "일일 호출 한도(트래픽)를 초과했습니다."),
    "30": ("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "등록되지 않은 인증키입니다."),
    "31": ("DEADLINE_HAS_EXPIRED_ERROR", "활용기간이 만료된 인증키입니다."),
    "32": ("UNREGISTERED_IP_ERROR", "등록되지 않은 IP 에서 호출했습니다."),
    "33": ("UNSIGNED_CALL_ERROR", "서명되지 않은 호출입니다."),
    "99": ("UNKNOWN_ERROR", "알 수 없는 오류입니다."),
}

# HTML·평문 오류 페이지에서 찾아볼 인증·트래픽 오류 이름 → 코드
AUTH_TOKENS: dict[str, str] = {
    name: code for code, (name, _) in CODE_INFO.items() if code in SERVICE_KEY_CODES | QUOTA_CODES
}

_TAG_RE = re.compile(r"<[^>]*>")


@dataclass
class ParsedPage:
    """응답 1페이지."""

    items: list[dict[str, str]] = field(default_factory=list)  # item 별 {태그: 원문 값}
    total_count: int = 0  # 전체 건수 (totalCount)
    page_no: int = 1
    num_of_rows: int = 0
    result_code: str = ""  # 원문 코드 ("000", 구버전 "00")
    result_msg: str = ""


def normalize_code(code: object) -> str:
    """결과 코드 정규화: 숫자는 두 자리(``"000"``→``"00"``, ``"3"``→``"03"``), 그 밖은 대문자."""
    text = "" if code is None else str(code).strip()
    if text.isdigit():
        return f"{int(text):02d}"
    return AUTH_TOKENS.get(text.upper(), text.upper())


def api_error(code: object, auth_msg: str = "", err_msg: str = "") -> ApiError:
    """오류 코드 → 알맞은 예외 (``QuotaExceededError`` / ``ServiceKeyError`` / ``ApiError``)."""
    auth_msg = (auth_msg or "").strip()
    norm = normalize_code(code)
    if not norm and auth_msg.upper() in AUTH_TOKENS:
        norm = AUTH_TOKENS[auth_msg.upper()]
    name, desc = CODE_INFO.get(norm, ("", "API 가 오류를 돌려주었습니다."))
    label = " ".join(p for p in (norm, auth_msg or name) if p) or "코드 없음"
    if norm in QUOTA_CODES:
        cls, title, retryable = QuotaExceededError, "일일 호출 한도 초과", False
    elif norm in SERVICE_KEY_CODES:
        cls, title, retryable = ServiceKeyError, "인증키 오류", False
    else:
        cls, title, retryable = ApiError, "API 오류", norm in RETRYABLE_CODES
    message = f"{title} [{label}]: {desc}"
    err_msg = (err_msg or "").strip()
    if err_msg and err_msg not in (auth_msg, name, "SERVICE ERROR"):
        message += f" ({err_msg})"
    return cls(message, code=norm, retryable=retryable)


def parse_response(body: bytes | str) -> ParsedPage:
    """API 응답 본문(XML 또는 JSON) → ``ParsedPage``.

    정상·데이터 없음(``03``)은 페이지로, 오류 응답은 ``ApiError`` 계열 예외로 알린다.
    해석할 수 없는 본문(HTML 점검 페이지 등)은 재시도 가능한 ``ApiError`` 다.
    """
    data = _strip(body)
    if not data:
        raise ApiError("API 응답 본문이 비어 있습니다.", retryable=True)
    first = data[:1]
    if first in ("{", b"{"):
        return _parse_json(data)
    if first in ("<", b"<"):
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise _unrecognized(data) from None
        return _parse_xml(root, data)
    raise _unrecognized(data)


def error_from_body(body: bytes | str) -> ApiError | None:
    """본문에 알아볼 수 있는 API 오류(코드)가 있으면 그 예외, 아니면 None (HTTP 오류 응답 분류용)."""
    try:
        parse_response(body)
    except ApiError as exc:
        return exc if exc.code else None
    return None


# ---------------------------------------------------------------------- XML
def _parse_xml(root: ET.Element, raw: bytes | str) -> ParsedPage:
    tag = _local(root.tag)
    if tag == "OpenAPI_ServiceResponse":
        header = root.find("cmmMsgHeader")
        node = header if header is not None else root
        raise api_error(_text(node, "returnReasonCode"), _text(node, "returnAuthMsg"), _text(node, "errMsg"))
    if tag != "response":
        raise _unrecognized(raw)
    header = root.find("header")
    code = _text(header, "resultCode")
    msg = _text(header, "resultMsg")
    body = root.find("body")
    norm = normalize_code(code)
    if norm in NODATA_CODES:
        return ParsedPage([], 0, _to_int(_text(body, "pageNo"), 1), _to_int(_text(body, "numOfRows"), 0), code, msg)
    if norm and norm not in OK_CODES:
        raise api_error(code, msg)
    if not norm and body is None:
        raise ApiError("API 응답 형식을 알 수 없습니다 (resultCode·body 없음).", retryable=True)
    items = _xml_items(body)
    return ParsedPage(
        items=items,
        total_count=_to_int(_text(body, "totalCount"), len(items)),
        page_no=_to_int(_text(body, "pageNo"), 1),
        num_of_rows=_to_int(_text(body, "numOfRows"), len(items)),
        result_code=code,
        result_msg=msg,
    )


def _xml_items(body: ET.Element | None) -> list[dict[str, str]]:
    if body is None:
        return []
    container = body.find("items")
    nodes = container.findall("item") if container is not None else body.findall(".//item")
    return [{_local(child.tag): child.text or "" for child in node} for node in nodes]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(node: ET.Element | None, name: str) -> str:
    if node is None:
        return ""
    child = node.find(name)
    return (child.text or "").strip() if child is not None else ""


# ---------------------------------------------------------------------- JSON
def _parse_json(raw: bytes | str) -> ParsedPage:
    try:
        data = json.loads(raw)
    except ValueError:
        raise _unrecognized(raw) from None
    if not isinstance(data, dict):
        raise _unrecognized(raw)
    gateway = data.get("OpenAPI_ServiceResponse", data if "cmmMsgHeader" in data else None)
    if isinstance(gateway, dict):
        node = gateway.get("cmmMsgHeader", gateway)
        node = node if isinstance(node, dict) else {}
        raise api_error(node.get("returnReasonCode"), _str(node.get("returnAuthMsg")), _str(node.get("errMsg")))
    response = data.get("response", data)
    response = response if isinstance(response, dict) else {}
    header = response.get("header") if isinstance(response.get("header"), dict) else response
    code = _str(header.get("resultCode"))
    msg = _str(header.get("resultMsg"))
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    norm = normalize_code(code)
    if norm in NODATA_CODES:
        return ParsedPage([], 0, _to_int(body.get("pageNo"), 1), _to_int(body.get("numOfRows"), 0), code, msg)
    if norm and norm not in OK_CODES:
        raise api_error(code, msg)
    if not norm and not body:
        raise ApiError("API 응답 형식을 알 수 없습니다 (resultCode·body 없음).", retryable=True)
    items = _json_items(body.get("items"))
    return ParsedPage(
        items=items,
        total_count=_to_int(body.get("totalCount"), len(items)),
        page_no=_to_int(body.get("pageNo"), 1),
        num_of_rows=_to_int(body.get("numOfRows"), len(items)),
        result_code=code,
        result_msg=msg,
    )


def _json_items(node: Any) -> list[dict[str, str]]:
    """``{"item": [...]}`` / ``{"item": {...}}`` / ``[...]`` / ``""`` 모두 받는다."""
    if isinstance(node, Mapping):
        node = node.get("item", [])
    if isinstance(node, Mapping):
        node = [node]
    if not isinstance(node, list):
        return []
    return [{str(k): _str(v, strip=False) for k, v in item.items()} for item in node if isinstance(item, Mapping)]


def _str(value: Any, *, strip: bool = True) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text.strip() if strip else text


# ---------------------------------------------------------------------- 공통
def _strip(body: bytes | str) -> bytes | str:
    """앞뒤 공백과 UTF-8 BOM 제거."""
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body).strip()
        if data.startswith(codecs.BOM_UTF8):
            data = data[len(codecs.BOM_UTF8):].strip()
        return data
    data = str(body).strip()
    if data.startswith("\ufeff"):
        data = data[1:].strip()
    return data


def _to_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return default


def _unrecognized(raw: bytes | str) -> ApiError:
    """형식을 알 수 없는 본문: 인증 오류 이름이 들어 있으면 그 오류, 아니면 재시도 가능한 오류."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    for token, code in AUTH_TOKENS.items():
        if token in text:
            return api_error(code, token)
    snippet = " ".join(_TAG_RE.sub(" ", text).split())
    if len(snippet) > 120:
        snippet = snippet[:120] + "…"
    return ApiError(f"API 응답을 해석할 수 없습니다 (일시적인 서버 문제일 수 있습니다): {snippet or '(내용 없음)'}", retryable=True)
