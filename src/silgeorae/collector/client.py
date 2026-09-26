"""국토교통부 실거래가 API 클라이언트 — 페이지네이션·재시도·오류 분류.

요청: ``GET {base_url}/{서비스명}/get{서비스명}?serviceKey=…&LAWD_CD=…&DEAL_YMD=…&pageNo=…&numOfRows=…``

* 일시적 오류(HTTP 5xx·429, 연결 실패·타임아웃, 재시도 가능 코드 01·02·04·05·99, 해석 불가 본문)는
  ``backoff * 2**n`` 초 쉬고 ``max_retries`` 번까지 다시 시도한다.
* 인증키 오류(``ServiceKeyError``)·호출 한도 초과(``QuotaExceededError``)는 재시도 없이 바로 알린다.
* 밖으로 나가는 ``ApiError`` 에는 항상 ``deal_type``·``lawd_cd``·``deal_ym`` 이 채워져 있다.
"""

from __future__ import annotations

import http.client
import logging
import ssl
import time
import urllib.error
import urllib.parse
from typing import Callable, Mapping, TypeVar

from ..errors import ApiError, ConfigError, QuotaExceededError, ServiceKeyError
from ..models import DealType
from ..utils import parse_ym
from .http import HttpResponse, Transport, urllib_transport
from .parser import ParsedPage, error_from_body, parse_response

logger = logging.getLogger(__name__)

T = TypeVar("T")

NO_KEY_MESSAGE = (
    "공공데이터포털 인증키(serviceKey)가 설정되지 않았습니다. data.go.kr 에서 "
    f"'{DealType.APT_SALE.api_title}' 등 필요한 API 를 활용신청한 뒤, 마이페이지의 일반 인증키"
    "(Decoding)를 설정 파일이나 환경변수로 지정하세요. 인증키 없이 전체 흐름을 보려면 "
    "'silgeorae demo' 를 실행하세요."
)

_MAX_PAGES = 10_000  # 한 파티션에서 받을 최대 페이지 수 (무한 반복 방지)


def normalize_service_key(service_key: str | None) -> str:
    """인증키 정리: 공백·따옴표를 없애고, Encoding 키(``%`` 포함)는 한 번 unquote 해 Decoding 키로 바꾼다.

    Encoding 키를 그대로 쓰면 쿼리 인코딩 때 ``%`` 가 다시 인코딩되어
    포털이 ``SERVICE_KEY_IS_NOT_REGISTERED_ERROR`` 를 돌려준다.
    """
    key = "".join(str(service_key or "").split()).strip("'\"")
    if not key:
        raise ConfigError(NO_KEY_MESSAGE)
    if "%" in key:
        key = urllib.parse.unquote(key)
    return key


def service_key_hint(api_title: str) -> str:
    """인증키 오류 안내 문구."""
    return (
        f"공공데이터포털(data.go.kr)에서 '{api_title}' 활용신청이 승인되었는지, 인증키가 맞는지 확인하세요 "
        "(일반 인증키의 Decoding 값 권장). 새로 신청·승인된 키는 활성화까지 최대 1시간 정도 걸릴 수 있습니다."
    )


def quota_hint(api_title: str) -> str:
    """호출 한도 초과 안내 문구."""
    return (
        f"'{api_title}'의 오늘 호출 한도를 모두 썼습니다. 내일 다시 실행하면 이어서 수집합니다 "
        "(한도는 data.go.kr 마이페이지에서 확인·변경 신청)."
    )


class MolitClient:
    """국토교통부 실거래가 API 클라이언트.

    ``fetch(유형, 시군구, 연월)`` 은 모든 페이지를 받아 원본 item dict 목록을 돌려준다.
    ``transport`` 에 ``FakeTransport`` 를 넣으면 네트워크 없이 동작한다.
    """

    DEFAULT_BASE_URL = "https://apis.data.go.kr/1613000"

    def __init__(
        self,
        service_key: str,
        *,
        transport: Transport | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff: float = 1.0,
        page_size: int = 1000,
        min_interval: float = 0.0,
        base_url: str = DEFAULT_BASE_URL,
        service_overrides: Mapping[DealType, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._service_key = normalize_service_key(service_key)
        self.transport: Transport = transport if transport is not None else urllib_transport
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.backoff = max(0.0, float(backoff))
        self.page_size = int(page_size)
        if self.page_size < 1:
            raise ConfigError(f"page_size 는 1 이상이어야 합니다: {page_size!r}")
        self.min_interval = max(0.0, float(min_interval))
        self.base_url = str(base_url).strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"API 주소(base_url)는 http:// 또는 https:// 로 시작해야 합니다: {base_url!r}")
        self.service_overrides: dict[DealType, str] = {}
        for key, service in (service_overrides or {}).items():
            name = str(service).strip().strip("/")
            if not name:
                raise ConfigError(f"{key} 의 서비스명이 비어 있습니다.")
            self.service_overrides[_as_deal_type(key)] = name
        self._sleep = sleep
        self._clock: Callable[[], float] = time.monotonic  # 테스트에서 바꿔 끼울 수 있다
        self._last_request_at: float | None = None
        self._request_count = 0

    def __repr__(self) -> str:  # 인증키는 드러내지 않는다
        return f"MolitClient(base_url={self.base_url!r}, requests={self._request_count})"

    @property
    def request_count(self) -> int:
        """지금까지 보낸 HTTP 요청 수 (재시도 포함)."""
        return self._request_count

    # ------------------------------------------------------------------ 공개 API
    def url_for(self, deal_type: DealType) -> str:
        """``DealType`` 의 요청 URL (``service_overrides`` 반영)."""
        dt = _as_deal_type(deal_type)
        service = self.service_overrides.get(dt, dt.api_service)
        return f"{self.base_url}/{service}/get{service}"

    def fetch(self, deal_type: DealType, lawd_cd: str, deal_ym: str) -> list[dict[str, str]]:
        """(유형, 시군구, 계약연월) 파티션의 원본 item 을 모든 페이지에 걸쳐 받는다."""
        dt, code, ym = _check_args(deal_type, lawd_cd, deal_ym)
        items: list[dict[str, str]] = []
        previous: list[dict[str, str]] | None = None
        total = 0
        page_no = 1
        while True:
            page = self._fetch_page(dt, code, ym, page_no)
            if page.items and page.items == previous:
                logger.warning(
                    "%s %s %s: %d페이지가 앞 페이지와 같아 중단합니다 (서버가 pageNo 를 무시하는 것 같습니다).",
                    dt.label, code, ym, page_no,
                )
                break
            items.extend(page.items)
            total = page.total_count
            if not page.items or len(items) >= total or page_no >= _MAX_PAGES:
                break
            previous = page.items
            page_no += 1
        if len(items) < total:
            logger.warning("%s %s %s: 전체 %d건 중 %d건만 받았습니다.", dt.label, code, ym, total, len(items))
        logger.debug("%s %s %s: %d건 (%d페이지)", dt.label, code, ym, len(items), page_no)
        return items

    def fetch_page(self, deal_type: DealType, lawd_cd: str, deal_ym: str, page_no: int = 1) -> ParsedPage:
        """한 페이지만 받는다 (인증키 확인 등). 재시도·오류 분류는 ``fetch`` 와 같다."""
        dt, code, ym = _check_args(deal_type, lawd_cd, deal_ym)
        return self._fetch_page(dt, code, ym, int(page_no))

    def request(
        self,
        url: str,
        params: Mapping[str, str],
        *,
        parse: Callable[[bytes], T] = parse_response,  # type: ignore[assignment]
    ) -> T:
        """GET 요청 (간격 조절·재시도·HTTP 오류 분류 포함). ``parse`` 가 2xx 본문을 해석한다.

        ``sync_regions`` 처럼 실거래가 API 가 아닌 주소에도 같은 재시도 규칙을 쓸 때 부른다.
        """
        attempt = 0
        while True:
            try:
                response = self._send(url, params)
                if not 200 <= response.status < 300:
                    raise _http_error(response)
                return parse(response.body)
            except ApiError as exc:
                if not exc.retryable:
                    raise
                error = exc
            except (OSError, http.client.HTTPException) as exc:
                if _is_certificate_error(exc):
                    raise ApiError(
                        f"SSL 인증서를 확인할 수 없습니다: {_describe(exc)} "
                        "(회사망 프록시·보안 프로그램의 인증서 설정을 확인하세요).",
                        retryable=False,
                    ) from exc
                error = ApiError(f"네트워크 오류: {_describe(exc)}", retryable=True)
                error.__cause__ = exc
            if attempt >= self.max_retries:
                raise ApiError(
                    f"API 호출이 계속 실패했습니다 (총 {attempt + 1}회 시도): {error}",
                    code=error.code,
                    retryable=True,
                ) from error
            delay = self.backoff * (2 ** attempt)
            attempt += 1
            logger.warning("API 호출 실패 — %.1f초 후 다시 시도합니다 (%d/%d): %s", delay, attempt, self.max_retries, error)
            if delay > 0:
                self._sleep(delay)

    # ------------------------------------------------------------------ 내부
    def _fetch_page(self, dt: DealType, code: str, ym: str, page_no: int) -> ParsedPage:
        params = {
            "serviceKey": self._service_key,
            "LAWD_CD": code,
            "DEAL_YMD": ym,
            "pageNo": str(page_no),
            "numOfRows": str(self.page_size),
        }
        try:
            return self.request(self.url_for(dt), params)
        except ServiceKeyError as exc:
            raise ServiceKeyError(
                f"{exc} {service_key_hint(dt.api_title)}", code=exc.code, deal_type=dt, lawd_cd=code, deal_ym=ym
            ) from exc
        except QuotaExceededError as exc:
            raise QuotaExceededError(
                f"{exc} {quota_hint(dt.api_title)}", code=exc.code, deal_type=dt, lawd_cd=code, deal_ym=ym
            ) from exc
        except ApiError as exc:
            exc.deal_type, exc.lawd_cd, exc.deal_ym = dt, code, ym
            raise

    def _send(self, url: str, params: Mapping[str, str]) -> HttpResponse:
        self._throttle()
        self._request_count += 1
        logger.debug("GET %s %s", url, {k: ("***" if k == "serviceKey" else v) for k, v in params.items()})
        return self.transport(url, params, self.timeout)

    def _throttle(self) -> None:
        """``min_interval`` 초보다 촘촘하게 요청하지 않도록 기다린다."""
        if self.min_interval > 0 and self._last_request_at is not None:
            wait = self.min_interval - (self._clock() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()


# ---------------------------------------------------------------------- 도우미
def _as_deal_type(value: DealType | str) -> DealType:
    if isinstance(value, DealType):
        return value
    try:
        return DealType.parse(str(value))
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def _check_args(deal_type: DealType | str, lawd_cd: str, deal_ym: str) -> tuple[DealType, str, str]:
    dt = _as_deal_type(deal_type)
    code = str(lawd_cd).strip()
    if len(code) != 5 or not code.isdigit():
        raise ConfigError(f"지역코드(LAWD_CD)는 5자리 숫자여야 합니다: {lawd_cd!r}")
    try:
        ym = parse_ym(deal_ym)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    return dt, code, ym


def _http_error(response: HttpResponse) -> ApiError:
    """2xx 가 아닌 응답 → 예외. 본문에 API 오류 코드가 있으면 그것을 우선한다."""
    specific = error_from_body(response.body)
    if specific is not None:
        return specific
    status = response.status
    code = f"HTTP {status}"
    if status in (401, 403):
        return ServiceKeyError(
            f"인증 실패 (HTTP {status}): 인증키가 올바르지 않거나 이 API 를 쓸 권한이 없습니다.", code=code
        )
    if status == 429:
        return ApiError("요청이 너무 잦습니다 (HTTP 429).", code=code, retryable=True)
    if status >= 500:
        return ApiError(f"API 서버 오류 (HTTP {status}).", code=code, retryable=True)
    if status == 404:
        return ApiError("API 주소를 찾을 수 없습니다 (HTTP 404) — 서비스명·주소를 확인하세요.", code=code)
    return ApiError(f"API 요청이 거부되었습니다 (HTTP {status}).", code=code)


def _is_certificate_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(exc, ssl.SSLCertVerificationError) or isinstance(reason, ssl.SSLCertVerificationError)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        exc = exc.reason
    if isinstance(exc, TimeoutError):
        return f"응답 시간 초과 ({exc})" if str(exc) else "응답 시간 초과"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
