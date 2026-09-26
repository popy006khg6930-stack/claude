"""수집팀(collector) — 국토교통부 실거래가 API 호출과 지역코드 동기화.

* ``MolitClient.fetch(유형, 시군구, 연월)`` → 원본 item dict 목록 (페이지네이션·재시도·오류 분류)
* ``parse_response`` → XML/JSON 응답 해석, 오류 코드 → ``ApiError`` 계열 예외
* ``FakeTransport`` · ``build_response_xml`` → 네트워크 없는 테스트·데모
* ``sync_regions`` → 행정안전부 법정동코드 API 로 최신 시군구 코드표
"""

from __future__ import annotations

from .client import MolitClient, normalize_service_key
from .fake import FakeTransport, build_error_xml, build_response_xml
from .http import HttpResponse, Transport, urllib_transport
from .parser import ParsedPage, parse_response
from .region_sync import sync_regions

__all__ = [
    "FakeTransport",
    "HttpResponse",
    "MolitClient",
    "ParsedPage",
    "Transport",
    "build_error_xml",
    "build_response_xml",
    "normalize_service_key",
    "parse_response",
    "sync_regions",
    "urllib_transport",
]
