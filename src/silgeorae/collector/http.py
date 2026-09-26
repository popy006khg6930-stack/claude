"""HTTP 전송 계층.

``Transport`` 는 ``(url, params, timeout) → HttpResponse`` 함수다. 실제 호출은 ``urllib_transport``,
오프라인 테스트·데모는 ``FakeTransport`` (fake.py) 를 끼운다.

* HTTP 오류 응답(4xx·5xx)도 예외 없이 ``HttpResponse`` 로 돌려준다 (분류는 클라이언트가 한다).
* 연결 실패·타임아웃은 ``OSError`` 계열 예외(URLError, TimeoutError, ConnectionError)로 알린다.
"""

from __future__ import annotations

import http.client
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping

from .. import __version__

USER_AGENT = f"silgeorae/{__version__} (Python urllib)"


@dataclass
class HttpResponse:
    """HTTP 응답 1건 (상태 코드·본문 바이트·헤더)."""

    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.body, str):  # 테스트 편의: 문자열 본문도 받는다
            self.body = self.body.encode("utf-8")

    @property
    def text(self) -> str:
        """본문을 UTF-8 로 디코드한 문자열 (깨진 바이트는 대체 문자)."""
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        """2xx 응답 여부."""
        return 200 <= self.status < 300


Transport = Callable[[str, Mapping[str, str], float], HttpResponse]
"""``(url, params, timeout) → HttpResponse``. 네트워크 오류는 OSError 계열 예외로 알린다."""


def urllib_transport(url: str, params: Mapping[str, str], timeout: float) -> HttpResponse:
    """표준 라이브러리 ``urllib`` 로 GET 요청한다 (``HTTPS_PROXY`` 등 환경변수 프록시를 따른다).

    쿼리는 ``urlencode`` 로 한 번만 인코딩한다 — 그래서 인증키는 Decoding 키여야 한다
    (``MolitClient`` 가 Encoding 키를 자동으로 되돌린다).
    """
    query = urllib.parse.urlencode(params)
    full_url = f"{url}{'&' if '?' in url else '?'}{query}" if query else url
    request = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return HttpResponse(int(status), resp.read(), dict(resp.headers.items()))
    except urllib.error.HTTPError as exc:  # 4xx·5xx 도 응답으로 돌려준다
        try:
            body = exc.read() or b""
        except Exception:  # 오류 본문을 못 읽어도 상태 코드는 알린다
            body = b""
        headers = dict(exc.headers.items()) if exc.headers is not None else {}
        return HttpResponse(int(exc.code), body, headers)
    except http.client.HTTPException as exc:  # IncompleteRead 등 → 재시도 가능한 연결 오류로
        raise ConnectionError(f"HTTP 응답을 끝까지 읽지 못했습니다: {exc!r}") from exc
