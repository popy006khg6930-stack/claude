"""여러 팀이 함께 쓰는 작은 도우미 함수 (연월 계산, 금액·면적 표기, 한국 시간)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Union

KST = timezone(timedelta(hours=9), "KST")
"""한국 표준시 (일광절약시간 없음 → 고정 오프셋으로 충분, Windows 에서도 tzdata 불필요)."""

PYEONG_M2 = 3.305785
"""1평 = 3.305785㎡"""

_YM_PATTERNS = (
    re.compile(r"^(\d{4})(\d{2})$"),  # 202501
    re.compile(r"^(\d{4})[-./\s](\d{1,2})$"),  # 2025-01, 2025.1, 2025/01
    re.compile(r"^(\d{4})년\s*(\d{1,2})월$"),  # 2025년 1월
)


def now_kst() -> datetime:
    """현재 한국 시각 (tz-aware)."""
    return datetime.now(KST)


def today_kst() -> date:
    """오늘 날짜 (한국 기준)."""
    return now_kst().date()


def parse_ym(value: Union[str, int]) -> str:
    """다양한 연월 표기를 ``"YYYYMM"`` 으로 정규화한다.

    >>> parse_ym("2025-01"), parse_ym(202501), parse_ym("2025.1"), parse_ym("2025년 3월")
    ('202501', '202501', '202501', '202503')
    """
    text = str(value).strip()
    for pattern in _YM_PATTERNS:
        m = pattern.match(text)
        if m:
            year, month = int(m.group(1)), int(m.group(2))
            if not 1 <= month <= 12:
                break
            if year < 1900:
                break
            return f"{year:04d}{month:02d}"
    raise ValueError(f"연월 형식이 올바르지 않습니다: {value!r} (예: 2025-01 또는 202501)")


def ym_of(d: date) -> str:
    """날짜 → ``"YYYYMM"``."""
    return f"{d.year:04d}{d.month:02d}"


def ym_to_date(ym: str) -> date:
    """``"YYYYMM"`` → 그 달 1일."""
    ym = parse_ym(ym)
    return date(int(ym[:4]), int(ym[4:]), 1)


def ym_add(ym: str, months: int) -> str:
    """연월에 개월 수를 더한다 (음수 가능)."""
    ym = parse_ym(ym)
    index = int(ym[:4]) * 12 + int(ym[4:]) - 1 + months
    return f"{index // 12:04d}{index % 12 + 1:02d}"


def month_range(start: str, end: str) -> list[str]:
    """시작~끝 연월(양끝 포함)을 오름차순 ``"YYYYMM"`` 목록으로."""
    start, end = parse_ym(start), parse_ym(end)
    if start > end:
        raise ValueError(f"시작 연월({start})이 끝 연월({end})보다 늦습니다.")
    months = []
    cur = start
    while cur <= end:
        months.append(cur)
        cur = ym_add(cur, 1)
    return months


def recent_months(n: int, today: Optional[date] = None) -> list[str]:
    """이번 달을 포함한 최근 ``n`` 개월 (오름차순)."""
    if n < 1:
        raise ValueError("개월 수는 1 이상이어야 합니다.")
    end = ym_of(today or today_kst())
    return month_range(ym_add(end, -(n - 1)), end)


def ym_label(ym: str) -> str:
    """``"202501"`` → ``"2025.01"`` (표·차트 축 표기용)."""
    ym = parse_ym(ym)
    return f"{ym[:4]}.{ym[4:]}"


def m2_to_pyeong(m2: Optional[float]) -> Optional[float]:
    """㎡ → 평 (반올림하지 않음)."""
    if m2 is None:
        return None
    return m2 / PYEONG_M2


def format_manwon(value: Optional[Union[int, float]], *, short: bool = False) -> str:
    """만원 단위 금액을 한국식으로 표기한다.

    >>> format_manwon(82500), format_manwon(5000), format_manwon(100000), format_manwon(None)
    ('8억 2,500만원', '5,000만원', '10억원', '-')
    >>> format_manwon(82500, short=True)
    '8.25억'
    """
    if value is None:
        return "-"
    amount = int(round(value))
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if short:
        if amount >= 10000:
            return f"{sign}{amount / 10000:.2f}".rstrip("0").rstrip(".") + "억"
        return f"{sign}{amount:,}만"
    eok, man = divmod(amount, 10000)
    if eok and man:
        return f"{sign}{eok:,}억 {man:,}만원"
    if eok:
        return f"{sign}{eok:,}억원"
    if man:
        return f"{sign}{man:,}만원"
    return "0원"


def format_area(m2: Optional[float]) -> str:
    """``84.97`` → ``"84.97㎡(25.7평)"``."""
    if m2 is None:
        return "-"
    return f"{m2:.2f}㎡({m2 / PYEONG_M2:.1f}평)"
