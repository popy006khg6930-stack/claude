"""원본 API 레코드(dict) → 표준 ``Transaction`` 변환 (ARCHITECTURE.md §2.2).

* 신규 영문 필드와 구버전 한글 태그를 모두 읽는다 (별칭표는 ``fields`` 모듈).
* 계약일(년·월·일)이 없거나 잘못되면 ``NormalizationError`` — 나머지 값은 없으면 ``None`` / ``""``.
* 표준 필드로 쓰지 않은 원본 필드는 빈 값을 빼고 ``extra`` 에 문자열로 남긴다.
  표준 필드 원본 값이 있는데 해석하지 못한 경우(예: 등기일자 ``"미정"``)도 원본 키로 ``extra`` 에 남긴다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any, TypeVar

from ..errors import NormalizationError
from ..models import DealType, Transaction, assign_seq
from .fields import (
    UNDERGROUND_KEYS,
    clean_text,
    coerce_deal_type,
    key_index,
    parse_cancel_flag,
    parse_float,
    parse_int,
    parse_short_date,
    parse_used_flag,
)

__all__ = ["normalize_item", "normalize_items"]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_REPR_LIMIT = 200


def short_repr(raw: Any, limit: int = _REPR_LIMIT) -> str:
    """오류 메시지용 원본 레코드 요약 (빈 값 제외, ``limit`` 자로 자름)."""
    if isinstance(raw, Mapping):
        parts = []
        for key, value in raw.items():
            text = clean_text(value)
            if text:
                parts.append(f"{key}={text}")
        text = "{" + ", ".join(parts) + "}"
    else:
        text = repr(raw)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _Reader:
    """원본 레코드를 한 번 훑어 표준 필드 값(``found``)과 나머지(``extra``)로 나눈다.

    같은 표준 필드에 원본 키가 여럿 있으면 별칭표 앞쪽(신규 영문 키)이 이긴다. 빈 값은 버린다.
    해석하지 못한 표준 필드 값은 ``leftovers`` 에 원본 키로 모아 ``extra`` 에 보탠다.
    """

    __slots__ = ("raw", "found", "extra", "leftovers")

    def __init__(self, raw: Mapping[str, Any], deal_type: DealType) -> None:
        index = key_index(deal_type)
        found: dict[str, tuple[int, str, str]] = {}  # 표준 필드 → (우선순위, 원본 키, 값)
        extra: dict[str, str] = {}
        for key, value in raw.items():
            text = clean_text(value)
            if not text:
                continue
            if type(key) is not str:
                key = str(key)
            hit = index.get(key)
            if hit is None:
                extra[key] = text
                continue
            name, rank = hit
            previous = found.get(name)
            if previous is None or rank < previous[0]:
                found[name] = (rank, key, text)
        self.raw = raw
        self.found = found
        self.extra = extra
        self.leftovers: dict[str, str] = {}

    def text(self, name: str) -> str:
        hit = self.found.get(name)
        return hit[2] if hit else ""

    def value(self, name: str, parser: Callable[[str], _T | None]) -> _T | None:
        hit = self.found.get(name)
        if hit is None:
            return None
        result = parser(hit[2])
        if result is None:
            self.leftovers[hit[1]] = hit[2]  # 원본 값을 잃지 않도록 extra 로
        return result


def _deal_date(reader: _Reader) -> date:
    y, m, d = reader.text("deal_year"), reader.text("deal_month"), reader.text("deal_day")
    year, month, day = parse_int(y), parse_int(m), parse_int(d)
    if year is None or month is None or day is None:
        raise NormalizationError(
            f"계약일(년·월·일)이 없거나 숫자가 아닙니다 (년={y!r}, 월={m!r}, 일={d!r}): {short_repr(reader.raw)}"
        )
    try:
        if year < 1900:
            raise ValueError
        return date(year, month, day)
    except ValueError:
        raise NormalizationError(
            f"계약일이 올바르지 않습니다 ({y}-{m}-{d}): {short_repr(reader.raw)}"
        ) from None


def _cancel_state(text: str) -> bool | None:
    """해제여부 원본 값 → True/False. 알 수 없는 값은 None (해제 아님으로 보고 extra 에 남긴다)."""
    if parse_cancel_flag(text):
        return True
    return False if text.upper() in ("N", "X") else None


def _road_address(reader: _Reader) -> str:
    """``"샘플로 51"`` / ``"샘플로 12-3"`` / ``"샘플로 지하 7"`` (본번·부번 앞자리 0 제거)."""
    road = reader.text("road_name")
    bonbun = reader.value("road_bonbun", parse_int)
    bubun = reader.value("road_bubun", parse_int)
    if not road:
        return ""
    if bonbun is None or bonbun <= 0:
        return road
    underground = any(reader.extra.get(key) == "1" for key in UNDERGROUND_KEYS)
    text = f"{road} {'지하 ' if underground else ''}{bonbun}"
    if bubun is not None and bubun > 0:
        text += f"-{bubun}"
    return text


def _build(deal_type: DealType, raw: Mapping[str, Any], lawd_cd: str, sigungu: str) -> Transaction:
    reader = _Reader(raw, deal_type)
    deal_date = _deal_date(reader)

    raw_cd = reader.text("lawd_cd")
    code = clean_text(lawd_cd) or raw_cd
    if not code:
        raise NormalizationError(f"시군구 코드(lawd_cd·sggCd)를 알 수 없습니다: {short_repr(raw)}")

    build_year = reader.value("build_year", parse_int)
    if build_year is not None and build_year <= 0:
        build_year = None  # 건축년도 0 이하 = 미상

    tx = Transaction(
        deal_type=deal_type,
        lawd_cd=code,
        deal_date=deal_date,
        dong=reader.text("dong"),
        jibun=reader.text("jibun"),
        name=reader.text("name"),
        sigungu=clean_text(sigungu) or reader.text("sigungu"),
        floor=reader.value("floor", parse_int),
        area_m2=reader.value("area_m2", parse_float),
        land_area_m2=reader.value("land_area_m2", parse_float),
        build_year=build_year,
        price=reader.value("price", parse_int),
        deposit=reader.value("deposit", parse_int),
        monthly_rent=reader.value("monthly_rent", parse_int),
        is_cancelled=bool(reader.value("is_cancelled", _cancel_state)),
        cancel_date=reader.value("cancel_date", parse_short_date),
        deal_method=reader.text("deal_method"),
        agent_location=reader.text("agent_location"),
        registration_date=reader.value("registration_date", parse_short_date),
        seller=reader.text("seller"),
        buyer=reader.text("buyer"),
        building_dong=reader.text("building_dong"),
        house_type=reader.text("house_type"),
        contract_type=reader.text("contract_type"),
        contract_term=reader.text("contract_term"),
        renewal_right_used=reader.value("renewal_right_used", parse_used_flag),
        prev_deposit=reader.value("prev_deposit", parse_int),
        prev_monthly_rent=reader.value("prev_monthly_rent", parse_int),
        complex_id=reader.text("complex_id"),
        road_address=_road_address(reader),
    )

    extra = reader.extra  # 표준 필드로 쓰지 않은 원본 필드 (빈 값 제외)
    if raw_cd and raw_cd != code:
        extra["sggCd"] = raw_cd  # 조회 코드와 원본 시군구 코드가 다를 때 (행정구역 개편 등)
    extra.update(reader.leftovers)
    tx.extra = extra
    return tx


def normalize_item(
    deal_type: DealType,
    raw: Mapping[str, Any],
    *,
    lawd_cd: str = "",
    sigungu: str = "",
) -> Transaction:
    """원본 레코드 1건 → ``Transaction``.

    * ``lawd_cd`` — 조회에 쓴 시군구 코드(저장소 파티션 키). 비우면 원본 ``sggCd``/``지역코드``.
      둘 다 있고 다르면 원본 값을 ``extra["sggCd"]`` 에 남긴다.
    * ``sigungu`` — 시군구 이름. 비우면 원본 ``sggNm``/``시군구``.
    * ``seq`` 는 0 이다 — 여러 건을 한꺼번에 넣을 때는 ``normalize_items`` 를 쓴다.

    변환할 수 없으면(계약일 없음·잘못됨, 시군구 코드 없음, dict 아님) ``NormalizationError``.
    """
    deal_type = coerce_deal_type(deal_type)
    if not isinstance(raw, Mapping):
        raise NormalizationError(f"원본 레코드가 dict 형식이 아닙니다: {short_repr(raw)}")
    try:
        return _build(deal_type, raw, lawd_cd, sigungu)
    except NormalizationError:
        raise
    except Exception as exc:  # 예상하지 못한 값 형식 → 레코드 단위 오류로 바꾼다
        raise NormalizationError(
            f"{deal_type.label} 레코드를 변환하지 못했습니다 ({exc}): {short_repr(raw)}"
        ) from exc


def normalize_items(
    deal_type: DealType,
    raws: Iterable[Mapping[str, Any]],
    *,
    lawd_cd: str = "",
    sigungu: str = "",
    strict: bool = False,
    on_error: Callable[[Mapping, Exception], None] | None = None,
) -> list[Transaction]:
    """원본 레코드 목록 → ``Transaction`` 목록.

    잘못된 레코드는 건너뛰고 ``on_error(raw, exc)`` 를 부른다 (없으면 경고 로그).
    ``strict=True`` 면 첫 오류에서 ``NormalizationError`` 를 그대로 올린다.
    마지막에 ``assign_seq`` 를 적용해, 같은 배치의 동일 거래도 서로 다른 키를 갖게 한다.
    """
    deal_type = coerce_deal_type(deal_type)
    result: list[Transaction] = []
    skipped = 0
    for raw in raws:
        try:
            result.append(normalize_item(deal_type, raw, lawd_cd=lawd_cd, sigungu=sigungu))
        except NormalizationError as exc:
            if strict:
                raise
            skipped += 1
            if on_error is not None:
                on_error(raw, exc)
            else:
                logger.warning("레코드를 건너뜁니다: %s", exc)
    if skipped:
        logger.info("%s %s: %d건 변환, %d건 건너뜀", deal_type.label, lawd_cd or "-", len(result), skipped)
    return assign_seq(result)
