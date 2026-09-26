"""패키지 공통 예외 계층 (팀 간 계약).

CLI 는 ``SilgeoraeError`` 를 잡아 사용자에게 한국어 메시지로 보여 준다.
"""

from __future__ import annotations

from typing import Sequence


class SilgeoraeError(Exception):
    """silgeorae 에서 발생시키는 모든 예외의 기반 클래스."""


class ConfigError(SilgeoraeError):
    """설정 파일·인증키·명령행 인자 등 사용자 입력 오류."""


class RegionError(SilgeoraeError):
    """지역(시군구) 해석 오류의 기반 클래스."""


class RegionNotFoundError(RegionError):
    """입력한 지역명·코드에 해당하는 시군구가 없음."""


class AmbiguousRegionError(RegionError):
    """입력한 지역명이 여러 시군구에 해당함 (예: '중구')."""

    def __init__(self, message: str, candidates: Sequence[object] = ()):
        super().__init__(message)
        self.candidates = list(candidates)


class ApiError(SilgeoraeError):
    """국토교통부/공공데이터포털 API 호출 실패.

    ``retryable`` 이 True 이면 잠시 후 재시도하면 성공할 수 있는 오류
    (일시적 서버 오류, 타임아웃 등)이다.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        retryable: bool = False,
        deal_type: object = None,
        lawd_cd: str = "",
        deal_ym: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.deal_type = deal_type
        self.lawd_cd = lawd_cd
        self.deal_ym = deal_ym


class ServiceKeyError(ApiError):
    """인증키 미등록·만료·해당 API 미신청 등. 재시도해도 소용없으므로 수집을 즉시 중단한다."""


class QuotaExceededError(ApiError):
    """일일 호출 한도(트래픽) 초과. 수집을 즉시 중단하고 다음 날 이어서 한다."""


class NormalizationError(SilgeoraeError):
    """원본 레코드를 표준 ``Transaction`` 으로 변환하지 못함."""


class StorageError(SilgeoraeError):
    """저장소(SQLite) 관련 오류."""


class ExportError(SilgeoraeError):
    """리포트 파일 생성 오류 (예: openpyxl 미설치)."""
