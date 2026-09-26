"""실거래 통계 — 입출력 없는 순수 함수 모음.

* 입력은 ``Transaction`` 목록, 출력은 작은 dataclass 목록이다 (표로 바꾸는 일은 ``report`` 가 맡는다).
* **해제(취소)된 거래는 가격·건수 통계에서 뺀다.** 해제 건수·해제 목록에서만 센다.
* **유형(DealType)이 다른 거래는 섞지 않는다.** 모든 묶음 키에 유형이 들어간다.
* 금액은 모두 만원 단위. 평균·중위값은 반올림하지 않은 ``float`` 로 돌려준다.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from itertools import groupby
from typing import Iterable, Optional, Sequence

from ..models import DealType, PropertyType, Transaction
from ..utils import KST, PYEONG_M2, parse_ym

AREA_BANDS: tuple[str, ...] = (
    "소형(60㎡ 이하)",
    "중소형(60~85㎡)",
    "중대형(85~135㎡)",
    "대형(135㎡ 초과)",
)
"""면적대 이름 (작은 것부터)."""

AREA_UNKNOWN = "면적 미상"

# 면적대(주거 규모 구분)를 적용하는 종류 — 토지·상업·공장 면적에는 의미가 없다.
AREA_BAND_PROPERTY_TYPES: frozenset[PropertyType] = frozenset(
    {
        PropertyType.APT,
        PropertyType.OFFICETEL,
        PropertyType.ROWHOUSE,
        PropertyType.DETACHED,
        PropertyType.PRESALE,
    }
)

RENEWAL = "갱신"
"""전월세 계약구분 중 갱신계약 값."""

_TYPE_ORDER = {dt: i for i, dt in enumerate(DealType)}
_PROPERTY_ORDER = {pt: i for i, pt in enumerate(PropertyType)}


# ---------------------------------------------------------------------- 기본 도우미
def type_order(deal_type: DealType) -> int:
    """유형 정렬 순서 (``DealType`` 선언 순서: 아파트 매매 → 아파트 전월세 → …)."""
    return _TYPE_ORDER[deal_type]


def sorted_deal_types(deal_types: Iterable[DealType]) -> list[DealType]:
    """중복을 없애고 선언 순서로 정렬한 유형 목록."""
    return sorted(set(deal_types), key=type_order)


def area_band(area_m2: Optional[float]) -> str:
    """면적(㎡) → 면적대 이름.

    >>> area_band(59.99), area_band(60), area_band(60.01), area_band(85), area_band(135.5), area_band(None)
    ('소형(60㎡ 이하)', '소형(60㎡ 이하)', '중소형(60~85㎡)', '중소형(60~85㎡)', '대형(135㎡ 초과)', '면적 미상')
    """
    if area_m2 is None or area_m2 <= 0:
        return AREA_UNKNOWN
    if area_m2 <= 60:
        return AREA_BANDS[0]
    if area_m2 <= 85:
        return AREA_BANDS[1]
    if area_m2 <= 135:
        return AREA_BANDS[2]
    return AREA_BANDS[3]


def median_of(values: Iterable[Optional[float]]) -> Optional[float]:
    """None 을 뺀 값들의 중위값 (없으면 None)."""
    data = [v for v in values if v is not None]
    return float(statistics.median(data)) if data else None


def mean_of(values: Iterable[Optional[float]]) -> Optional[float]:
    """None 을 뺀 값들의 평균 (없으면 None)."""
    data = [v for v in values if v is not None]
    return statistics.fmean(data) if data else None


def percent(part: int, whole: int) -> Optional[float]:
    """백분율 (``whole`` 이 0 이면 None)."""
    return part / whole * 100 if whole else None


def sigungu_of(tx: Transaction) -> str:
    """표시용 시군구 이름 (이름이 없으면 시군구 코드)."""
    return tx.sigungu or tx.lawd_cd


def is_jeonse(tx: Transaction) -> bool:
    """전세 여부 — 월세가 0 이거나 없는 전월세 거래."""
    return tx.is_rent and not tx.monthly_rent


def valid_sales(transactions: Iterable[Transaction]) -> list[Transaction]:
    """통계에 쓰는 매매 거래 — 해제되지 않았고 거래금액이 있는 매매."""
    return [t for t in transactions if t.deal_type.is_sale and not t.is_cancelled and t.price is not None]


def valid_rents(transactions: Iterable[Transaction]) -> list[Transaction]:
    """통계에 쓰는 전월세 거래 — 해제되지 않은 전월세."""
    return [t for t in transactions if t.deal_type.is_rent and not t.is_cancelled]


def in_period(tx: Transaction, start_ym: Optional[str], end_ym: Optional[str]) -> bool:
    """계약연월이 ``start_ym``~``end_ym`` (양끝 포함, None 은 제한 없음) 안에 있는지."""
    ym = tx.deal_ym
    if start_ym is not None and ym < start_ym:
        return False
    if end_ym is not None and ym > end_ym:
        return False
    return True


def filter_period(
    transactions: Iterable[Transaction], start_ym: Optional[str] = None, end_ym: Optional[str] = None
) -> list[Transaction]:
    """계약연월 기준으로 기간 안의 거래만 고른다 (연월은 ``parse_ym`` 이 읽는 형식이면 된다)."""
    start = parse_ym(start_ym) if start_ym is not None else None
    end = parse_ym(end_ym) if end_ym is not None else None
    return [t for t in transactions if in_period(t, start, end)]


def data_period(transactions: Iterable[Transaction]) -> Optional[tuple[str, str]]:
    """거래들의 계약연월 최소~최대 (거래가 없으면 None)."""
    months = [t.deal_ym for t in transactions]
    if not months:
        return None
    return min(months), max(months)


def _aware(value: datetime) -> datetime:
    """시간대 없는 datetime 은 한국 시간으로 본다 (aware/naive 비교 오류 방지)."""
    return value.replace(tzinfo=KST) if value.tzinfo is None else value


def _representative_area(txs: Sequence[Transaction]) -> Optional[float]:
    """묶음의 대표 면적 — 가장 많이 나온 면적 (같으면 큰 값)."""
    counts = Counter(round(t.area_m2, 2) for t in txs if t.area_m2)
    if not counts:
        return None
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _common_build_year(txs: Sequence[Transaction]) -> Optional[int]:
    counts = Counter(t.build_year for t in txs if t.build_year)
    if not counts:
        return None
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _pyeong(area_m2: Optional[float]) -> Optional[float]:
    return round(area_m2 / PYEONG_M2, 2) if area_m2 else None


# ---------------------------------------------------------------------- 월별 추이
@dataclass
class MonthlyStat:
    """유형·계약월별 추이 1행. 매매 유형은 가격 값, 전월세 유형은 보증금·월세 값을 채운다."""

    deal_type: DealType
    ym: str  # "YYYYMM"
    count: int = 0  # 거래 건수 (해제 제외)
    cancelled: int = 0  # 해제 건수
    median_price: Optional[float] = None  # 중위 거래가
    mean_price: Optional[float] = None  # 평균 거래가
    mean_price_per_pyeong: Optional[float] = None  # 평균 평당가 (거래별 평당가의 평균)
    jeonse_count: int = 0
    wolse_count: int = 0
    jeonse_median_deposit: Optional[float] = None  # 전세 중위 보증금
    wolse_mean_deposit: Optional[float] = None  # 월세 평균 보증금
    wolse_mean_rent: Optional[float] = None  # 월세 평균 월세


def monthly_trend(
    transactions: Iterable[Transaction],
    months: Optional[Sequence[str]] = None,
    *,
    deal_types: Optional[Iterable[DealType]] = None,
) -> list[MonthlyStat]:
    """유형·계약월별 건수와 가격.

    ``months`` 를 주면 그 달들만, 거래가 없는 달도 0건 행으로 채운다.
    주지 않으면 유형마다 거래가 있는 달만 돌려준다. 결과는 유형 순서 → 연월 순.
    """
    txs = list(transactions)
    by_key: dict[tuple[DealType, str], list[Transaction]] = defaultdict(list)
    for tx in txs:
        by_key[(tx.deal_type, tx.deal_ym)].append(tx)
    types = sorted_deal_types(deal_types if deal_types is not None else (t.deal_type for t in txs))
    wanted = [parse_ym(m) for m in months] if months is not None else None

    result: list[MonthlyStat] = []
    for dt in types:
        yms = wanted if wanted is not None else sorted(ym for (d, ym) in by_key if d is dt)
        for ym in yms:
            group = by_key.get((dt, ym), [])
            live = [t for t in group if not t.is_cancelled]
            stat = MonthlyStat(deal_type=dt, ym=ym, count=len(live), cancelled=len(group) - len(live))
            if dt.is_rent:
                jeonse = [t for t in live if is_jeonse(t)]
                wolse = [t for t in live if not is_jeonse(t)]
                stat.jeonse_count = len(jeonse)
                stat.wolse_count = len(wolse)
                stat.jeonse_median_deposit = median_of(t.deposit for t in jeonse)
                stat.wolse_mean_deposit = mean_of(t.deposit for t in wolse)
                stat.wolse_mean_rent = mean_of(t.monthly_rent for t in wolse)
            else:
                stat.median_price = median_of(t.price for t in live)
                stat.mean_price = mean_of(t.price for t in live)
                stat.mean_price_per_pyeong = mean_of(t.price_per_pyeong for t in live)
            result.append(stat)
    return result


# ---------------------------------------------------------------------- 단지별
@dataclass
class ComplexStat:
    """단지·면적타입별 매매 요약 1행."""

    deal_type: DealType
    complex_key: str
    area_type: Optional[int]
    sigungu: str
    dong: str
    jibun: str
    name: str
    area_m2: Optional[float]  # 대표 면적 (가장 많이 거래된 면적)
    count: int
    latest_date: date
    latest_price: int
    max_price: int
    max_date: date  # 최고가를 처음 기록한 날
    min_price: int
    min_date: date
    mean_price_per_pyeong: Optional[float]
    build_year: Optional[int]

    @property
    def pyeong(self) -> Optional[float]:
        return _pyeong(self.area_m2)


def complex_summary(transactions: Iterable[Transaction], *, named_only: bool = True) -> list[ComplexStat]:
    """(유형, 단지, 면적타입)별 매매 요약. 거래 많은 순 → 단지명 순.

    ``named_only`` (기본) 이면 단지·건물명이 없는 거래(단독·토지 등, 지번 일부 마스킹)는
    같은 물건인지 알 수 없어 뺀다.
    """
    groups: dict[tuple[DealType, str, Optional[int]], list[Transaction]] = defaultdict(list)
    for tx in valid_sales(transactions):
        if named_only and not tx.name:
            continue
        groups[(tx.deal_type, tx.complex_key, tx.area_type)].append(tx)

    result: list[ComplexStat] = []
    for (dt, ckey, atype), txs in groups.items():
        by_date = sorted(txs, key=lambda t: (t.deal_date, t.price))
        latest = by_date[-1]
        top = max(by_date, key=lambda t: (t.price, -t.deal_date.toordinal()))  # 같은 값이면 먼저 기록한 날
        low = min(by_date, key=lambda t: (t.price, t.deal_date))
        first = by_date[0]
        result.append(
            ComplexStat(
                deal_type=dt,
                complex_key=ckey,
                area_type=atype,
                sigungu=sigungu_of(first),
                dong=first.dong,
                jibun=first.jibun,
                name=first.name,
                area_m2=_representative_area(txs),
                count=len(txs),
                latest_date=latest.deal_date,
                latest_price=latest.price,  # type: ignore[arg-type]  (valid_sales 가 보장)
                max_price=top.price,  # type: ignore[arg-type]
                max_date=top.deal_date,
                min_price=low.price,  # type: ignore[arg-type]
                min_date=low.deal_date,
                mean_price_per_pyeong=mean_of(t.price_per_pyeong for t in txs),
                build_year=_common_build_year(txs),
            )
        )
    result.sort(key=lambda s: (-s.count, s.name, s.dong, type_order(s.deal_type), s.area_type or 0))
    return result


# ---------------------------------------------------------------------- 법정동별 · 면적대별
@dataclass
class DongStat:
    """법정동별 매매 요약 1행."""

    deal_type: DealType
    lawd_cd: str
    sigungu: str
    dong: str
    count: int
    median_price: Optional[float]
    mean_price_per_pyeong: Optional[float]


def dong_summary(transactions: Iterable[Transaction]) -> list[DongStat]:
    """(유형, 시군구, 법정동)별 매매 건수·중위가·평균 평당가. 유형 순 → 건수 많은 순."""
    groups: dict[tuple[DealType, str, str], list[Transaction]] = defaultdict(list)
    for tx in valid_sales(transactions):
        groups[(tx.deal_type, tx.lawd_cd, tx.dong)].append(tx)
    result = [
        DongStat(
            deal_type=dt,
            lawd_cd=lawd_cd,
            sigungu=sigungu_of(txs[0]),
            dong=dong,
            count=len(txs),
            median_price=median_of(t.price for t in txs),
            mean_price_per_pyeong=mean_of(t.price_per_pyeong for t in txs),
        )
        for (dt, lawd_cd, dong), txs in groups.items()
    ]
    result.sort(key=lambda s: (type_order(s.deal_type), -s.count, s.sigungu, s.dong))
    return result


@dataclass
class AreaBandStat:
    """면적대별 매매 요약 1행."""

    deal_type: DealType
    band: str
    count: int
    share: Optional[float]  # 같은 유형 안에서의 비중(%)
    median_price: Optional[float]
    mean_price_per_pyeong: Optional[float]


def area_band_summary(transactions: Iterable[Transaction]) -> list[AreaBandStat]:
    """(유형, 면적대)별 매매 건수·비중·중위가·평균 평당가.

    주거용(아파트·오피스텔·연립다세대·단독/다가구·분양권)만 — 토지·상업·공장 면적은 주거 면적대와 무관하다.
    """
    groups: dict[DealType, dict[str, list[Transaction]]] = defaultdict(lambda: defaultdict(list))
    for tx in valid_sales(transactions):
        if tx.deal_type.property_type in AREA_BAND_PROPERTY_TYPES:
            groups[tx.deal_type][area_band(tx.area_m2)].append(tx)
    result: list[AreaBandStat] = []
    for dt in sorted_deal_types(groups):
        bands = groups[dt]
        total = sum(len(v) for v in bands.values())
        for band in AREA_BANDS + (AREA_UNKNOWN,):
            txs = bands.get(band)
            if not txs:
                continue
            result.append(
                AreaBandStat(
                    deal_type=dt,
                    band=band,
                    count=len(txs),
                    share=percent(len(txs), total),
                    median_price=median_of(t.price for t in txs),
                    mean_price_per_pyeong=mean_of(t.price_per_pyeong for t in txs),
                )
            )
    return result


# ---------------------------------------------------------------------- 신고가
@dataclass
class NewHigh:
    """신고가 거래 — 같은 단지·면적타입에서 이전 최고가를 넘은 매매."""

    tx: Transaction
    prev_price: int  # 이전 최고가
    prev_date: date  # 이전 최고가를 기록한 날

    @property
    def increase(self) -> int:
        """상승액(만원)."""
        return (self.tx.price or 0) - self.prev_price

    @property
    def increase_pct(self) -> Optional[float]:
        """상승률(%)."""
        return percent(self.increase, self.prev_price)


def find_new_highs(
    transactions: Iterable[Transaction],
    *,
    start_ym: Optional[str] = None,
    end_ym: Optional[str] = None,
    named_only: bool = True,
) -> list[NewHigh]:
    """신고가 거래를 찾는다.

    (유형, 단지, 면적타입)마다 계약일 순으로 보면서, **그보다 앞선 날짜**의 해제되지 않은 모든 거래의
    최고가보다 **엄격히 높은** 거래를 신고가로 본다 (앞선 거래가 1건 이상 있어야 함, 같은 날 거래끼리는
    서로 비교하지 않는다). ``start_ym``~``end_ym`` 을 주면 그 기간의 신고가만 돌려주되,
    기간 이전 거래는 비교 기준(이력)으로 모두 쓴다. 최근 계약일 순 → 상승률 큰 순.
    """
    start = parse_ym(start_ym) if start_ym is not None else None
    end = parse_ym(end_ym) if end_ym is not None else None
    groups: dict[tuple[DealType, str, int], list[Transaction]] = defaultdict(list)
    for tx in valid_sales(transactions):
        if tx.area_type is None or (named_only and not tx.name):
            continue
        groups[(tx.deal_type, tx.complex_key, tx.area_type)].append(tx)

    result: list[NewHigh] = []
    for txs in groups.values():
        txs.sort(key=lambda t: t.deal_date)
        best: Optional[int] = None
        best_date: Optional[date] = None
        for day, same_day in groupby(txs, key=lambda t: t.deal_date):
            day_txs = list(same_day)
            if best is not None and best_date is not None:
                for tx in day_txs:
                    if tx.price > best and in_period(tx, start, end):  # type: ignore[operator]
                        result.append(NewHigh(tx=tx, prev_price=best, prev_date=best_date))
            day_max = max(t.price for t in day_txs)  # type: ignore[type-var]
            if best is None or day_max > best:
                best, best_date = day_max, day
    result.sort(key=lambda h: (-h.tx.deal_date.toordinal(), -(h.increase_pct or 0.0), h.tx.name))
    return result


# ---------------------------------------------------------------------- 해제 · 상위 · 신규
def cancelled_deals(transactions: Iterable[Transaction]) -> list[Transaction]:
    """해제(취소)된 거래 목록 — 해제일(없으면 계약일) 최근 순."""
    txs = [t for t in transactions if t.is_cancelled]
    txs.sort(key=lambda t: ((t.cancel_date or t.deal_date).toordinal(), t.deal_date.toordinal()), reverse=True)
    return txs


def top_sales(transactions: Iterable[Transaction], n: int = 20, *, per_type: bool = True) -> list[Transaction]:
    """거래금액 상위 매매 (해제 제외). ``per_type`` 이면 유형마다 ``n`` 건씩, 유형 순으로 이어 붙인다."""
    if n <= 0:
        return []
    sales = valid_sales(transactions)
    key = lambda t: (-(t.price or 0), -t.deal_date.toordinal(), t.name)  # noqa: E731
    if not per_type:
        return sorted(sales, key=key)[:n]
    result: list[Transaction] = []
    for dt in sorted_deal_types(t.deal_type for t in sales):
        result.extend(sorted((t for t in sales if t.deal_type is dt), key=key)[:n])
    return result


def new_deals(
    transactions: Iterable[Transaction], since: datetime, *, include_cancelled: bool = False
) -> list[Transaction]:
    """``since`` 이후(포함) 처음 수집된 거래 (``first_seen_at`` 기준) — 수집 시각 최근 순.

    시간대 없는 datetime 은 한국 시간으로 본다. 해제된 거래는 기본으로 뺀다.
    """
    since_aware = _aware(since)
    txs = [
        t
        for t in transactions
        if t.first_seen_at is not None
        and _aware(t.first_seen_at) >= since_aware
        and (include_cancelled or not t.is_cancelled)
    ]
    txs.sort(key=lambda t: (_aware(t.first_seen_at), t.deal_date), reverse=True)  # type: ignore[arg-type]
    return txs


# ---------------------------------------------------------------------- 전월세
@dataclass
class RentStat:
    """전월세 요약 1행 (``ym`` 이 ``""`` 이면 기간 전체)."""

    deal_type: DealType
    ym: str
    count: int = 0
    jeonse_count: int = 0
    wolse_count: int = 0
    jeonse_mean_deposit: Optional[float] = None
    wolse_mean_deposit: Optional[float] = None
    wolse_mean_rent: Optional[float] = None
    contract_known: int = 0  # 계약구분(신규/갱신)이 적힌 건수
    renewal_count: int = 0  # 갱신계약 건수
    renewal_right_count: int = 0  # 갱신계약 중 갱신요구권 사용 건수

    @property
    def jeonse_share(self) -> Optional[float]:
        """전세 비중(%)."""
        return percent(self.jeonse_count, self.count)

    @property
    def wolse_share(self) -> Optional[float]:
        """월세 비중(%)."""
        return percent(self.wolse_count, self.count)

    @property
    def renewal_ratio(self) -> Optional[float]:
        """갱신계약 비율(%) — 계약구분이 적힌 거래 중 갱신."""
        return percent(self.renewal_count, self.contract_known)

    @property
    def renewal_right_ratio(self) -> Optional[float]:
        """갱신요구권 사용 비율(%) — 갱신계약 중 갱신요구권 사용."""
        return percent(self.renewal_right_count, self.renewal_count)


def _rent_stat(deal_type: DealType, ym: str, txs: Sequence[Transaction]) -> RentStat:
    jeonse = [t for t in txs if is_jeonse(t)]
    wolse = [t for t in txs if not is_jeonse(t)]
    renewals = [t for t in txs if (t.contract_type or "").strip() == RENEWAL]
    return RentStat(
        deal_type=deal_type,
        ym=ym,
        count=len(txs),
        jeonse_count=len(jeonse),
        wolse_count=len(wolse),
        jeonse_mean_deposit=mean_of(t.deposit for t in jeonse),
        wolse_mean_deposit=mean_of(t.deposit for t in wolse),
        wolse_mean_rent=mean_of(t.monthly_rent for t in wolse),
        contract_known=sum(1 for t in txs if (t.contract_type or "").strip()),
        renewal_count=len(renewals),
        renewal_right_count=sum(1 for t in renewals if t.renewal_right_used is True),
    )


def rent_summary(transactions: Iterable[Transaction], *, include_total: bool = True) -> list[RentStat]:
    """전월세 유형·계약월별 전세/월세 건수·비중, 평균 보증금·월세, 갱신계약·갱신요구권 사용 비율.

    거래가 있는 달만 돌려주고, ``include_total`` 이면 유형마다 기간 전체 행(``ym=""``)을 마지막에 붙인다.
    """
    groups: dict[DealType, dict[str, list[Transaction]]] = defaultdict(lambda: defaultdict(list))
    for tx in valid_rents(transactions):
        groups[tx.deal_type][tx.deal_ym].append(tx)
    result: list[RentStat] = []
    for dt in sorted_deal_types(groups):
        months = groups[dt]
        for ym in sorted(months):
            result.append(_rent_stat(dt, ym, months[ym]))
        if include_total:
            result.append(_rent_stat(dt, "", [t for ym in sorted(months) for t in months[ym]]))
    return result


# ---------------------------------------------------------------------- 전세가율
_RENT_OF_SALE: dict[DealType, DealType] = {
    sale: rent
    for sale in DealType
    if sale.is_sale
    for rent in DealType
    if rent.is_rent and rent.property_type is sale.property_type
}


@dataclass
class JeonseRatio:
    """같은 단지·면적타입의 전세가율 (전세 중위 보증금 ÷ 매매 중위가)."""

    sale_type: DealType
    rent_type: DealType
    complex_key: str
    area_type: int
    sigungu: str
    dong: str
    jibun: str
    name: str
    area_m2: Optional[float]
    sale_count: int
    sale_median: float
    jeonse_count: int
    jeonse_median: float

    @property
    def property_type(self) -> PropertyType:
        return self.sale_type.property_type

    @property
    def pyeong(self) -> Optional[float]:
        return _pyeong(self.area_m2)

    @property
    def ratio(self) -> Optional[float]:
        """전세가율(%)."""
        return self.jeonse_median / self.sale_median * 100 if self.sale_median else None


def jeonse_ratios(transactions: Iterable[Transaction], *, named_only: bool = True) -> list[JeonseRatio]:
    """매매와 전세가 모두 있는 (종류, 단지, 면적타입)마다 전세가율을 구한다.

    매매는 해제 제외, 전세는 월세가 0 이거나 없는 전월세다. 아파트 매매는 아파트 전월세와만 짝짓는다.
    결과는 종류 순 → 전세가율 높은 순.
    """
    sales: dict[tuple[PropertyType, str, int], list[Transaction]] = defaultdict(list)
    jeonse: dict[tuple[PropertyType, str, int], list[Transaction]] = defaultdict(list)
    for tx in transactions:
        if tx.is_cancelled or tx.area_type is None or (named_only and not tx.name):
            continue
        key = (tx.deal_type.property_type, tx.complex_key, tx.area_type)
        if tx.deal_type in _RENT_OF_SALE and tx.price is not None:
            sales[key].append(tx)
        elif is_jeonse(tx) and tx.deposit:
            jeonse[key].append(tx)

    result: list[JeonseRatio] = []
    for key in sales.keys() & jeonse.keys():
        s_txs, j_txs = sales[key], jeonse[key]
        sale_type = s_txs[0].deal_type
        first = s_txs[0]
        result.append(
            JeonseRatio(
                sale_type=sale_type,
                rent_type=_RENT_OF_SALE[sale_type],
                complex_key=key[1],
                area_type=key[2],
                sigungu=sigungu_of(first),
                dong=first.dong,
                jibun=first.jibun,
                name=first.name,
                area_m2=_representative_area(s_txs),
                sale_count=len(s_txs),
                sale_median=median_of(t.price for t in s_txs) or 0.0,
                jeonse_count=len(j_txs),
                jeonse_median=median_of(t.deposit for t in j_txs) or 0.0,
            )
        )
    result.sort(key=lambda r: (_PROPERTY_ORDER[r.property_type], -(r.ratio or 0.0), r.name, r.area_type))
    return result
