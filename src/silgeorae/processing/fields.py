"""원본 필드 해석 도우미 — 값 정리·숫자/날짜 변환과 원본 키 별칭표.

국토교통부 API 의 item 값은 모두 문자열이고 ``"  285,000"``, ``" "``(빈 값),
``"25.01.28"``(YY.MM.DD) 처럼 제각각이다. 여기의 변환 함수는 잘못된 값에도 예외를 던지지 않고
숫자·날짜는 ``None``, 문자열은 ``""`` 을 돌려준다 (필수 값 검사는 ``normalize`` 가 한다).

별칭표는 "표준 필드 → 원본 키 후보" 이다. 2024년 개편 후의 영문 키를 먼저, 구버전 한글 태그를
뒤에 둔다 (ARCHITECTURE.md §2.2). ``deal_year`` 처럼 ``Transaction`` 에 없는 이름은
여러 원본 값을 조합하는 중간 값이다 (계약일·도로명주소).
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from types import MappingProxyType
from collections.abc import Iterable, Mapping
from typing import Any

from ..models import DealType

__all__ = [
    "clean_text",
    "parse_int",
    "parse_float",
    "parse_short_date",
    "parse_used_flag",
    "parse_cancel_flag",
    "coerce_deal_type",
    "pick",
    "aliases_for",
    "key_index",
    "consumed_keys",
    "COMMON_ALIASES",
    "SALE_ALIASES",
    "RENT_ALIASES",
    "NAME_ALIASES",
    "AREA_ALIASES",
    "LAND_AREA_ALIASES",
    "UNDERGROUND_KEYS",
]

# ---------------------------------------------------------------------- 값 변환

# str.strip() 이 지우지 않는 보이지 않는 문자 (제로폭 공백·BOM 등)
_INVISIBLE = "\u200b\u200c\u200d\u2060\ufeff"
_EDGE_RE = re.compile(f"^[\\s{_INVISIBLE}]+|[\\s{_INVISIBLE}]+$")


def clean_text(value: Any) -> str:
    """문자열로 바꾸고 앞뒤 공백(전각 공백·NBSP·제로폭 문자 포함)을 지운다. ``None`` → ``""``."""
    if type(value) is not str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        elif not isinstance(value, str):
            value = str(value)
    text = value.strip()
    if text and not text.isascii() and (text[0] in _INVISIBLE or text[-1] in _INVISIBLE):
        text = _EDGE_RE.sub("", text)
    return text


def _number_text(value: Any) -> str:
    return clean_text(value).replace(",", "")


def parse_int(value: Any) -> int | None:
    """정수 해석. ``"  285,000"`` → 285000, ``"-1"`` → -1, ``"12.0"`` → 12.

    빈 값·숫자가 아닌 값·소수(``"84.97"``)는 ``None``.
    """
    if type(value) is str:
        try:
            return int(value.replace(",", ""))  # int() 는 앞뒤 공백(전각 포함)을 스스로 무시한다
        except ValueError:
            pass
    elif value is None or isinstance(value, bool):
        return None
    elif isinstance(value, int):
        return value
    elif isinstance(value, float):
        return int(value) if value.is_integer() else None  # nan·inf 는 is_integer() 가 False
    text = _number_text(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    number = parse_float(text)
    if number is not None and number.is_integer():
        return int(number)
    return None


def parse_float(value: Any) -> float | None:
    """실수 해석. ``"84.97"`` → 84.97, ``"1,234.5"`` → 1234.5. 빈 값·잘못된 값·nan·inf 는 ``None``."""
    number: float | None = None
    if type(value) is str:
        try:
            number = float(value.replace(",", ""))
        except ValueError:
            pass
    elif value is None or isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        number = float(value)
    if number is None:
        text = _number_text(value)
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


_DATE_PATTERNS = (
    re.compile(r"^([0-9]{2}|[0-9]{4})\s*[.\-/]\s*([0-9]{1,2})\s*[.\-/]\s*([0-9]{1,2})\s*\.?$"),  # 25.01.28, 2025-01-28
    re.compile(r"^([0-9]{4})([0-9]{2})([0-9]{2})$"),  # 20250128
    re.compile(r"^([0-9]{2}|[0-9]{4})\s*년\s*([0-9]{1,2})\s*월\s*([0-9]{1,2})\s*일$"),  # 2025년 1월 28일
)


def parse_short_date(value: Any) -> date | None:
    """날짜 해석. ``"25.01.28"``(YY.MM.DD → 20YY), ``"25.1.8"``, ``"2025.01.28"``, ``"2025-01-28"``,
    ``"20250128"``, ``"2025년 1월 28일"``. 빈 값·잘못된 값(없는 날짜 포함)은 ``None``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean_text(value)
    if not text:
        return None
    for pattern in _DATE_PATTERNS:
        m = pattern.match(text)
        if m:
            year, month, day = (int(g) for g in m.groups())
            if len(m.group(1)) == 2:
                year += 2000
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None


_USED_TRUE = frozenset({"사용", "Y", "YES", "TRUE", "O"})
_USED_FALSE = frozenset({"미사용", "비사용", "N", "NO", "FALSE", "X"})


def parse_used_flag(value: Any) -> bool | None:
    """갱신요구권 사용 여부. ``"사용"`` → True, ``"미사용"`` → False, 빈 값·알 수 없는 값 → ``None``."""
    text = clean_text(value).upper()
    if text in _USED_TRUE:
        return True
    if text in _USED_FALSE:
        return False
    return None


def parse_cancel_flag(value: Any) -> bool:
    """해제여부. ``"O"``(대소문자 무시)·``"Y"`` → True, 그 밖(빈 값 포함) → False."""
    return clean_text(value).upper() in ("O", "Y")


def coerce_deal_type(value: DealType | str) -> DealType:
    """``DealType`` 또는 코드·한글 이름(``"apt_sale"``, ``"아파트 전월세"``) → ``DealType``."""
    if isinstance(value, DealType):
        return value
    return DealType.parse(str(value))


def pick(raw: Mapping[str, Any], keys: Iterable[str]) -> tuple[str, str]:
    """후보 키 중 값이 비어 있지 않은 첫 번째의 ``(원본 키, 정리된 값)``. 없으면 ``("", "")``."""
    for key in keys:
        if key in raw:
            text = clean_text(raw[key])
            if text:
                return key, text
    return "", ""


# ---------------------------------------------------------------------- 별칭표

COMMON_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "lawd_cd": ("sggCd", "지역코드"),  # 조회 인자 lawd_cd 가 없을 때만 사용
        "sigungu": ("sggNm", "시군구"),
        "dong": ("umdNm", "법정동"),
        "jibun": ("jibun", "지번"),
        "deal_year": ("dealYear", "년"),
        "deal_month": ("dealMonth", "월"),
        "deal_day": ("dealDay", "일"),
        "floor": ("floor", "층"),
        "build_year": ("buildYear", "건축년도"),
        "is_cancelled": ("cdealType", "해제여부"),
        "cancel_date": ("cdealDay", "해제사유발생일"),
        "deal_method": ("dealingGbn", "거래유형"),
        "agent_location": ("estateAgentSggNm", "중개사소재지"),
        "registration_date": ("rgstDate", "등기일자"),
        "seller": ("slerGbn", "매도자"),
        "buyer": ("buyerGbn", "매수자"),
        "building_dong": ("aptDong", "동"),
        "house_type": ("houseType", "주택유형"),
        "complex_id": ("aptSeq", "일련번호"),
        "road_name": ("roadNm", "도로명"),
        "road_bonbun": ("roadNmBonbun", "도로명건물본번호코드"),
        "road_bubun": ("roadNmBubun", "도로명건물부번호코드"),
    }
)
"""모든 유형에 공통인 필드."""

SALE_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType({"price": ("dealAmount", "거래금액")})
"""매매 유형에만 쓰는 필드."""

RENT_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "deposit": ("deposit", "보증금액", "보증금"),
        "monthly_rent": ("monthlyRent", "월세금액", "월세"),
        "contract_type": ("contractType", "계약구분"),
        "contract_term": ("contractTerm", "계약기간"),
        "renewal_right_used": ("useRRRight", "갱신요구권사용"),
        "prev_deposit": ("preDeposit", "종전계약보증금"),
        "prev_monthly_rent": ("preMonthlyRent", "종전계약월세"),
    }
)
"""전월세 유형에만 쓰는 필드."""

_APT_NAME = ("aptNm", "아파트", "단지")
_OFFI_NAME = ("offiNm", "단지")
_RH_NAME = ("mhouseNm", "연립다세대")
_EXCLU_AREA = ("excluUseAr", "전용면적")
_BUILDING_AREA = ("buildingAr", "건물면적")
_PLOTTAGE = ("plottageAr", "대지면적")

NAME_ALIASES: Mapping[DealType, tuple[str, ...]] = MappingProxyType(
    {
        DealType.APT_SALE: _APT_NAME,
        DealType.APT_RENT: _APT_NAME,
        DealType.PRESALE: _APT_NAME,
        DealType.OFFI_SALE: _OFFI_NAME,
        DealType.OFFI_RENT: _OFFI_NAME,
        DealType.RH_SALE: _RH_NAME,
        DealType.RH_RENT: _RH_NAME,
        # 단독/다가구·토지·상업업무용·공장창고는 이름이 없다 → ""
    }
)
"""단지·건물명(name) 원본 키 (유형별)."""

AREA_ALIASES: Mapping[DealType, tuple[str, ...]] = MappingProxyType(
    {
        DealType.APT_SALE: _EXCLU_AREA,
        DealType.APT_RENT: _EXCLU_AREA,
        DealType.OFFI_SALE: _EXCLU_AREA,
        DealType.OFFI_RENT: _EXCLU_AREA,
        DealType.RH_SALE: _EXCLU_AREA,
        DealType.RH_RENT: _EXCLU_AREA,
        DealType.PRESALE: _EXCLU_AREA,
        DealType.SH_SALE: ("totalFloorAr", "연면적"),
        DealType.SH_RENT: ("totalFloorAr", "계약면적", "연면적"),
        DealType.LAND: ("dealArea", "거래면적"),
        DealType.COMMERCIAL: _BUILDING_AREA,
        DealType.INDUSTRIAL: _BUILDING_AREA,
    }
)
"""면적(area_m2) 원본 키 (유형별)."""

LAND_AREA_ALIASES: Mapping[DealType, tuple[str, ...]] = MappingProxyType(
    {
        DealType.RH_SALE: ("landAr", "대지권면적"),
        DealType.SH_SALE: _PLOTTAGE,
        DealType.COMMERCIAL: _PLOTTAGE,
        DealType.INDUSTRIAL: _PLOTTAGE,
    }
)
"""대지(권)면적(land_area_m2) 원본 키 (유형별)."""

UNDERGROUND_KEYS: tuple[str, ...] = ("roadNmbCd", "도로명지상지하코드")
"""도로명 지상/지하 구분 (``"1"`` = 지하). 도로명주소 표기에만 참고하고 extra 에도 남긴다."""


_AliasTable = Mapping[str, tuple[str, ...]]
_KeyIndex = Mapping[str, tuple[str, int]]


def _build_tables() -> tuple[dict[DealType, _AliasTable], dict[DealType, _KeyIndex]]:
    aliases: dict[DealType, _AliasTable] = {}
    indexes: dict[DealType, _KeyIndex] = {}
    for deal_type in DealType:
        table = dict(COMMON_ALIASES)
        table.update(RENT_ALIASES if deal_type.is_rent else SALE_ALIASES)
        for name, per_type in (("name", NAME_ALIASES), ("area_m2", AREA_ALIASES), ("land_area_m2", LAND_AREA_ALIASES)):
            if deal_type in per_type:
                table[name] = per_type[deal_type]
        index: dict[str, tuple[str, int]] = {}
        for name, keys in table.items():
            for rank, key in enumerate(keys):
                if key in index:  # 한 원본 키는 표준 필드 하나에만 쓰여야 한다
                    raise RuntimeError(f"별칭표 오류: {deal_type.value} 의 {key!r} 가 {index[key][0]}·{name} 에 중복")
                index[key] = (name, rank)
        aliases[deal_type] = MappingProxyType(table)
        indexes[deal_type] = MappingProxyType(index)
    return aliases, indexes


_FIELD_ALIASES, _KEY_INDEXES = _build_tables()


def aliases_for(deal_type: DealType) -> Mapping[str, tuple[str, ...]]:
    """유형별 "표준 필드 → 원본 키 후보" 표 (읽기 전용).

    매매 유형에는 ``price`` 가, 전월세 유형에는 보증금·월세·계약 관련 필드가 들어 있다.
    """
    return _FIELD_ALIASES[coerce_deal_type(deal_type)]


def key_index(deal_type: DealType) -> Mapping[str, tuple[str, int]]:
    """유형별 "원본 키 → (표준 필드, 우선순위)" 역색인 (우선순위 0 이 가장 먼저 쓰인다)."""
    return _KEY_INDEXES[coerce_deal_type(deal_type)]


def consumed_keys(deal_type: DealType) -> frozenset[str]:
    """정규화에서 표준 필드로 쓰이는 원본 키 전체 (나머지는 ``extra`` 로 간다)."""
    return frozenset(key_index(deal_type))
