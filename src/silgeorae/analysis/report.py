"""리포트 모델과 조립 — 통계(``stats``) 결과를 KPI·표·차트로 묶는다.

``build_report`` 가 만든 ``Report`` 를 ``export_excel`` / ``export_html`` / ``export_tables_csv`` 가 파일로 쓴다.

* 표의 금액 값은 만원 단위 정수, 날짜는 ``date``, 비율은 백분율 숫자(65.3 = 65.3%)다.
* ``Table.formats`` 가 열마다 표시 형식을 알려 준다 (``FORMATS`` 참고, 없는 열은 ``"text"``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional, Sequence

from ..models import DealType, Transaction
from ..utils import KST, format_manwon, month_range, now_kst, parse_ym, today_kst, ym_add, ym_label, ym_of
from . import stats

FORMATS: tuple[str, ...] = ("int", "manwon", "float1", "float2", "percent", "date", "text")
"""``Table.formats`` 에 쓸 수 있는 값."""

NUMERIC_FORMATS = frozenset({"int", "manwon", "float1", "float2", "percent"})

SOURCE_NOTE = "자료: 국토교통부 실거래가 공개시스템(공공데이터포털 OpenAPI)"

LAG_REMARK = "집계 중"
"""월별 추이 '비고' — 신고기한(계약 후 30일)이 지나지 않아 건수가 늘어날 수 있는 달."""


# ---------------------------------------------------------------------- 모델
@dataclass
class Table:
    """이름 붙은 표 1개 (엑셀 시트 1장 · CSV 파일 1개 · HTML 섹션 1개)."""

    name: str  # 31자 이하, 리포트 안에서 고유
    columns: list[str]
    rows: list[list[Any]]
    description: str = ""
    formats: dict[str, str] = field(default_factory=dict)  # 열 → "int" | "manwon" | "float1" | ...

    def column(self, name: str) -> list[Any]:
        """열 이름으로 그 열의 값 목록을 꺼낸다."""
        idx = self.columns.index(name)
        return [row[idx] for row in self.rows]

    def as_dicts(self) -> list[dict[str, Any]]:
        """행들을 ``{열: 값}`` dict 목록으로."""
        return [dict(zip(self.columns, row)) for row in self.rows]


@dataclass
class Chart:
    """차트 1개 — 값은 차트 단위 그대로, 값이 없는 달은 None.

    * ``"bar"`` / ``"line"``: 모든 계열이 막대 / 선이며 단위는 ``unit``.
    * ``"bar+line"``: 첫 계열은 막대(거래량, 건), 나머지 계열은 선(단위 ``unit``, 예: 만원).
    """

    title: str
    kind: str  # "bar" | "line" | "bar+line"
    labels: list[str]
    series: list[tuple[str, list[Optional[float]]]]
    unit: str = ""


@dataclass
class Report:
    """한 번의 분석 결과 — 내보내기 함수들의 입력."""

    title: str
    generated_at: datetime
    period: Optional[tuple[str, str]]  # ("YYYYMM", "YYYYMM"), 자료가 없으면 None
    regions: list[str]
    kpis: list[tuple[str, str]]  # (이름, 표시 문자열)
    tables: list[Table]
    charts: list[Chart]
    notes: list[str]

    def table(self, name: str) -> Optional[Table]:
        """이름으로 표를 찾는다 (없으면 None)."""
        return next((t for t in self.tables if t.name == name), None)

    def kpi(self, label: str) -> Optional[str]:
        """이름으로 KPI 값을 찾는다 (없으면 None)."""
        return next((value for name, value in self.kpis if name == label), None)

    @property
    def period_text(self) -> str:
        """``"2024.01 ~ 2025.12"`` (기간이 없으면 ``"-"``)."""
        if not self.period:
            return "-"
        return f"{ym_label(self.period[0])} ~ {ym_label(self.period[1])}"


# ---------------------------------------------------------------------- 값 표시
def round_half_up(value: float) -> int:
    """사사오입 정수 (``round`` 의 은행가 반올림 대신)."""
    return int(math.floor(value + 0.5))


def _int_or_none(value: Optional[float]) -> Optional[int]:
    return None if value is None else round_half_up(value)


def _round_or_none(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


def format_cell(value: Any, fmt: str = "") -> str:
    """표 값 1개를 화면 표시용 문자열로 바꾼다 (HTML·콘솔 공용, None 은 빈 문자열).

    >>> format_cell(82500, "manwon"), format_cell(65.33, "percent"), format_cell(2015, "text")
    ('82,500', '65.3%', '2015')
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "O" if value else ""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return ""
        if fmt in ("int", "manwon"):
            return f"{round_half_up(value):,}"
        if fmt == "float1":
            return f"{value:,.1f}"
        if fmt == "float2":
            return f"{value:,.2f}"
        if fmt == "percent":
            return f"{value:,.1f}%"
        if isinstance(value, float):
            return f"{value:.2f}".rstrip("0").rstrip(".")
        return str(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _kst_text(value: Optional[datetime]) -> str:
    """수집 시각 표시 (시간대가 있으면 한국 시간으로 바꿔서)."""
    if value is None:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone(KST)
    return value.strftime("%Y-%m-%d %H:%M")


def _area(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


def _count_text(n: int) -> str:
    return f"{n:,}건"


def _region_names(regions: Iterable[Any]) -> list[str]:
    names = []
    for region in regions:
        # Region 객체를 넘겨도 된다 (이름 속성 사용)
        name = getattr(region, "name", None) if not isinstance(region, str) else region
        text = str(name if name else region).strip()
        if text and text not in names:
            names.append(text)
    return names


def _regions_text(names: Sequence[str]) -> str:
    if not names:
        return "-"
    if len(names) <= 4:
        return ", ".join(names)
    return f"{', '.join(names[:3])} 외 {len(names) - 3}곳"


# ---------------------------------------------------------------------- 거래내역 표
_TX_COLUMNS: tuple[tuple[str, str], ...] = (
    ("유형", "text"),
    ("시군구", "text"),
    ("법정동", "text"),
    ("지번", "text"),
    ("단지명", "text"),
    ("동", "text"),
    ("층", "int"),
    ("전용면적(㎡)", "float2"),
    ("평", "float1"),
    ("계약일", "date"),
    ("거래금액(만원)", "manwon"),
    ("보증금(만원)", "manwon"),
    ("월세(만원)", "manwon"),
    ("전월세", "text"),
    ("건축년도", "text"),
    ("거래방식", "text"),
    ("중개사소재지", "text"),
    ("해제여부", "text"),
    ("해제일", "date"),
    ("등기일자", "date"),
    ("매도자", "text"),
    ("매수자", "text"),
    ("계약구분", "text"),
    ("계약기간", "text"),
    ("갱신요구권", "text"),
    ("종전보증금(만원)", "manwon"),
    ("종전월세(만원)", "manwon"),
    ("도로명주소", "text"),
    ("최초수집", "text"),
)

TRANSACTION_COLUMNS: tuple[str, ...] = tuple(name for name, _ in _TX_COLUMNS)
"""``transactions_table`` 의 열 이름."""

_RENEWAL_TEXT = {True: "사용", False: "미사용", None: ""}


def _formats(columns: Iterable[tuple[str, str]]) -> dict[str, str]:
    """(열, 형식) 목록 → formats dict. "text" 는 숫자 값이 있는 열(건축년도 등)만 남긴다."""
    return {name: fmt for name, fmt in columns if fmt != "text" or name in ("건축년도",)}


def _transaction_row(tx: Transaction) -> list[Any]:
    return [
        tx.deal_type.label,
        stats.sigungu_of(tx),
        tx.dong,
        tx.jibun,
        tx.name,
        tx.building_dong,
        tx.floor,
        _area(tx.area_m2),
        tx.pyeong,
        tx.deal_date,
        tx.price,
        tx.deposit,
        tx.monthly_rent,
        tx.rent_kind,
        tx.build_year,
        tx.deal_method,
        tx.agent_location,
        "O" if tx.is_cancelled else "",
        tx.cancel_date,
        tx.registration_date,
        tx.seller,
        tx.buyer,
        tx.contract_type,
        tx.contract_term,
        _RENEWAL_TEXT.get(tx.renewal_right_used, ""),
        tx.prev_deposit,
        tx.prev_monthly_rent,
        tx.road_address,
        _kst_text(tx.first_seen_at),
    ]


def transactions_table(transactions: Iterable[Transaction], name: str = "거래내역") -> Table:
    """거래 1건 = 1행인 원자료 표 (유형 순 → 최근 계약일 순). 금액은 만원, 해제여부는 ``"O"``."""
    txs = sorted(
        transactions,
        key=lambda t: (
            stats.type_order(t.deal_type),
            -t.deal_date.toordinal(),
            t.lawd_cd,
            t.dong,
            t.name,
            t.floor if t.floor is not None else 0,
        ),
    )
    return Table(
        name=name,
        columns=list(TRANSACTION_COLUMNS),
        rows=[_transaction_row(tx) for tx in txs],
        description="수집된 거래 원자료 (금액 단위 만원, 해제여부 O = 해제된 거래)",
        formats=_formats(_TX_COLUMNS),
    )


# ---------------------------------------------------------------------- 분석 표
def _table(name: str, spec: Sequence[tuple[str, str]], rows: list[list[Any]], description: str) -> Table:
    return Table(name=name, columns=[c for c, _ in spec], rows=rows, description=description, formats=_formats(spec))


def _monthly_table(monthly: list[stats.MonthlyStat], has_sales: bool, has_rents: bool, lag: set[str]) -> Table:
    spec: list[tuple[str, str]] = [("유형", "text"), ("계약월", "text"), ("거래 건수", "int"), ("해제 건수", "int")]
    if has_sales:
        spec += [("중위가(만원)", "manwon"), ("평균가(만원)", "manwon"), ("평균 평당가(만원)", "manwon")]
    if has_rents:
        spec += [
            ("전세 건수", "int"),
            ("월세 건수", "int"),
            ("전세 중위 보증금(만원)", "manwon"),
            ("월세 평균 보증금(만원)", "manwon"),
            ("월세 평균 월세(만원)", "manwon"),
        ]
    spec.append(("비고", "text"))
    rows = []
    for s in monthly:
        row: list[Any] = [s.deal_type.label, ym_label(s.ym), s.count, s.cancelled]
        rent = s.deal_type.is_rent
        if has_sales:
            row += [
                None if rent else _int_or_none(s.median_price),
                None if rent else _int_or_none(s.mean_price),
                None if rent else _int_or_none(s.mean_price_per_pyeong),
            ]
        if has_rents:
            row += (
                [
                    s.jeonse_count,
                    s.wolse_count,
                    _int_or_none(s.jeonse_median_deposit),
                    _int_or_none(s.wolse_mean_deposit),
                    _int_or_none(s.wolse_mean_rent),
                ]
                if rent
                else [None] * 5
            )
        row.append(LAG_REMARK if s.ym in lag else "")
        rows.append(row)
    return _table(
        "월별 추이",
        spec,
        rows,
        "유형·계약월별 거래 건수와 가격 (계약일 기준, 해제 거래 제외 — 해제 건수만 따로 셈)",
    )


def _complex_table(items: list[stats.ComplexStat]) -> Table:
    spec = [
        ("유형", "text"),
        ("시군구", "text"),
        ("법정동", "text"),
        ("지번", "text"),
        ("단지명", "text"),
        ("전용면적(㎡)", "float2"),
        ("평", "float1"),
        ("건수", "int"),
        ("최근 거래일", "date"),
        ("최근 거래가(만원)", "manwon"),
        ("최고가(만원)", "manwon"),
        ("최고가 거래일", "date"),
        ("최저가(만원)", "manwon"),
        ("평균 평당가(만원)", "manwon"),
        ("건축년도", "text"),
    ]
    rows = [
        [
            s.deal_type.label,
            s.sigungu,
            s.dong,
            s.jibun,
            s.name,
            _area(s.area_m2),
            s.pyeong,
            s.count,
            s.latest_date,
            s.latest_price,
            s.max_price,
            s.max_date,
            s.min_price,
            _int_or_none(s.mean_price_per_pyeong),
            s.build_year,
        ]
        for s in items
    ]
    return _table("단지별 요약", spec, rows, "단지·면적타입(전용면적 정수부)별 매매 요약 — 거래 많은 순")


def _dong_table(items: list[stats.DongStat]) -> Table:
    spec = [
        ("유형", "text"),
        ("시군구", "text"),
        ("법정동", "text"),
        ("건수", "int"),
        ("중위가(만원)", "manwon"),
        ("평균 평당가(만원)", "manwon"),
    ]
    rows = [
        [s.deal_type.label, s.sigungu, s.dong, s.count, _int_or_none(s.median_price), _int_or_none(s.mean_price_per_pyeong)]
        for s in items
    ]
    return _table("법정동별", spec, rows, "법정동별 매매 건수와 가격 — 유형별 거래 많은 순")


def _area_band_table(items: list[stats.AreaBandStat]) -> Table:
    spec = [
        ("유형", "text"),
        ("면적대", "text"),
        ("건수", "int"),
        ("비중(%)", "percent"),
        ("중위가(만원)", "manwon"),
        ("평균 평당가(만원)", "manwon"),
    ]
    rows = [
        [
            s.deal_type.label,
            s.band,
            s.count,
            _round_or_none(s.share, 1),
            _int_or_none(s.median_price),
            _int_or_none(s.mean_price_per_pyeong),
        ]
        for s in items
    ]
    return _table("면적대별", spec, rows, "전용면적 구간별 매매 건수와 가격 (주거용만, 비중은 유형 안에서)")


def _new_high_table(items: list[stats.NewHigh]) -> Table:
    spec = [
        ("유형", "text"),
        ("시군구", "text"),
        ("법정동", "text"),
        ("단지명", "text"),
        ("전용면적(㎡)", "float2"),
        ("평", "float1"),
        ("층", "int"),
        ("계약일", "date"),
        ("거래금액(만원)", "manwon"),
        ("이전 최고가(만원)", "manwon"),
        ("이전 최고가 거래일", "date"),
        ("상승액(만원)", "manwon"),
        ("상승률(%)", "percent"),
    ]
    rows = [
        [
            h.tx.deal_type.label,
            stats.sigungu_of(h.tx),
            h.tx.dong,
            h.tx.name,
            _area(h.tx.area_m2),
            h.tx.pyeong,
            h.tx.floor,
            h.tx.deal_date,
            h.tx.price,
            h.prev_price,
            h.prev_date,
            h.increase,
            _round_or_none(h.increase_pct, 1),
        ]
        for h in items
    ]
    return _table("신고가", spec, rows, "같은 단지·면적타입에서 이전 최고가(수집된 자료 기준)를 넘은 매매 — 최근 순")


def _cancelled_table(items: list[Transaction]) -> Table:
    spec = [
        ("유형", "text"),
        ("시군구", "text"),
        ("법정동", "text"),
        ("단지명", "text"),
        ("전용면적(㎡)", "float2"),
        ("층", "int"),
        ("계약일", "date"),
        ("거래금액(만원)", "manwon"),
        ("해제일", "date"),
        ("해제까지(일)", "int"),
    ]
    rows = [
        [
            t.deal_type.label,
            stats.sigungu_of(t),
            t.dong,
            t.name,
            _area(t.area_m2),
            t.floor,
            t.deal_date,
            t.price,
            t.cancel_date,
            (t.cancel_date - t.deal_date).days if t.cancel_date else None,
        ]
        for t in items
    ]
    return _table("해제 거래", spec, rows, "해제(취소)된 거래 — 건수·가격 통계에서는 제외, 해제일 최근 순")


def _rent_table(items: list[stats.RentStat]) -> Table:
    spec = [
        ("유형", "text"),
        ("계약월", "text"),
        ("전월세 건수", "int"),
        ("전세 건수", "int"),
        ("월세 건수", "int"),
        ("전세 비중(%)", "percent"),
        ("월세 비중(%)", "percent"),
        ("전세 평균 보증금(만원)", "manwon"),
        ("월세 평균 보증금(만원)", "manwon"),
        ("평균 월세(만원)", "manwon"),
        ("갱신계약 비율(%)", "percent"),
        ("갱신요구권 사용 비율(%)", "percent"),
    ]
    rows = [
        [
            s.deal_type.label,
            ym_label(s.ym) if s.ym else "전체",
            s.count,
            s.jeonse_count,
            s.wolse_count,
            _round_or_none(s.jeonse_share, 1),
            _round_or_none(s.wolse_share, 1),
            _int_or_none(s.jeonse_mean_deposit),
            _int_or_none(s.wolse_mean_deposit),
            _int_or_none(s.wolse_mean_rent),
            _round_or_none(s.renewal_ratio, 1),
            _round_or_none(s.renewal_right_ratio, 1),
        ]
        for s in items
    ]
    return _table(
        "전월세 요약",
        spec,
        rows,
        "전세·월세 비중, 평균 보증금·월세, 갱신계약 비율(계약구분 공개 거래 중)·갱신요구권 사용 비율(갱신계약 중)",
    )


def _jeonse_table(items: list[stats.JeonseRatio]) -> Table:
    spec = [
        ("유형", "text"),
        ("시군구", "text"),
        ("법정동", "text"),
        ("단지명", "text"),
        ("전용면적(㎡)", "float2"),
        ("평", "float1"),
        ("매매 건수", "int"),
        ("매매 중위가(만원)", "manwon"),
        ("전세 건수", "int"),
        ("전세 중위 보증금(만원)", "manwon"),
        ("전세가율(%)", "percent"),
    ]
    rows = [
        [
            r.property_type.label,
            r.sigungu,
            r.dong,
            r.name,
            _area(r.area_m2),
            r.pyeong,
            r.sale_count,
            _int_or_none(r.sale_median),
            r.jeonse_count,
            _int_or_none(r.jeonse_median),
            _round_or_none(r.ratio, 1),
        ]
        for r in items
    ]
    return _table("전세가율", spec, rows, "같은 단지·면적타입의 전세 중위 보증금 ÷ 매매 중위가 × 100 (분석 기간 내 거래)")


def _top_table(items: list[Transaction], top_n: int) -> Table:
    spec = [
        ("순위", "int"),
        ("유형", "text"),
        ("시군구", "text"),
        ("법정동", "text"),
        ("단지명", "text"),
        ("전용면적(㎡)", "float2"),
        ("평", "float1"),
        ("층", "int"),
        ("계약일", "date"),
        ("거래금액(만원)", "manwon"),
        ("평당가(만원)", "manwon"),
    ]
    rows = []
    rank = 0
    previous: Optional[DealType] = None
    for t in items:
        rank = rank + 1 if t.deal_type is previous else 1
        previous = t.deal_type
        rows.append(
            [
                rank,
                t.deal_type.label,
                stats.sigungu_of(t),
                t.dong,
                t.name,
                _area(t.area_m2),
                t.pyeong,
                t.floor,
                t.deal_date,
                t.price,
                _int_or_none(t.price_per_pyeong),
            ]
        )
    return _table(f"고가 거래 TOP{top_n}", spec, rows, f"유형별 거래금액 상위 {top_n}건 (해제 거래 제외)")


def _new_deals_table(items: list[Transaction], since: datetime) -> Table:
    spec = [
        ("유형", "text"),
        ("시군구", "text"),
        ("법정동", "text"),
        ("단지명", "text"),
        ("전용면적(㎡)", "float2"),
        ("층", "int"),
        ("계약일", "date"),
        ("거래금액(만원)", "manwon"),
        ("보증금(만원)", "manwon"),
        ("월세(만원)", "manwon"),
        ("최초수집", "text"),
    ]
    rows = [
        [
            t.deal_type.label,
            stats.sigungu_of(t),
            t.dong,
            t.name,
            _area(t.area_m2),
            t.floor,
            t.deal_date,
            t.price,
            t.deposit,
            t.monthly_rent,
            _kst_text(t.first_seen_at),
        ]
        for t in items
    ]
    return _table("신규 등록 거래", spec, rows, f"{_kst_text(since)} 이후 처음 수집된 거래 (해제 제외) — 수집 최근 순")


# ---------------------------------------------------------------------- KPI · 차트 · 참고
def _per_type_text(sales: list[Transaction], value_of, fmt) -> str:
    """매매 유형별 값 문자열 — 유형이 하나면 값만, 여럿이면 ``"아파트 8억원 · 오피스텔 2억원"``."""
    parts = []
    types = stats.sorted_deal_types(t.deal_type for t in sales)
    for dt in types:
        value = value_of([t for t in sales if t.deal_type is dt])
        if value is None:
            continue
        text = fmt(value)
        parts.append(text if len(types) == 1 else f"{dt.property_type.label} {text}")
    return " · ".join(parts) if parts else "-"


def _kpis(
    txs: list[Transaction],
    period: Optional[tuple[str, str]],
    region_names: list[str],
    high_count: int,
    new_count: Optional[int],
) -> list[tuple[str, str]]:
    sales = stats.valid_sales(txs)
    rents = stats.valid_rents(txs)
    jeonse = sum(1 for t in rents if stats.is_jeonse(t))
    kpis = [
        ("기간", f"{ym_label(period[0])} ~ {ym_label(period[1])}" if period else "-"),
        ("지역", _regions_text(region_names)),
        ("매매 건수", _count_text(len(sales))),
        ("해제 건수", _count_text(sum(1 for t in txs if t.is_cancelled))),
        (
            "매매 중위가",
            _per_type_text(sales, lambda g: stats.median_of(t.price for t in g), format_manwon),
        ),
        (
            "평균 평당가",
            _per_type_text(
                sales,
                lambda g: stats.mean_of(t.price_per_pyeong for t in g),
                lambda v: f"{round_half_up(v):,}만원/평",
            ),
        ),
        ("전월세 건수", f"{_count_text(len(rents))} (전세 {jeonse:,} · 월세 {len(rents) - jeonse:,})"),
        ("신고가 건수", _count_text(high_count)),
    ]
    if new_count is not None:
        kpis.append(("신규 등록 건수", _count_text(new_count)))
    return kpis


def _charts(monthly: list[stats.MonthlyStat], types: list[DealType], months: list[str]) -> list[Chart]:
    labels = [ym_label(m) for m in months]
    charts = []
    for dt in types:
        by_ym = {s.ym: s for s in monthly if s.deal_type is dt}
        rows = [by_ym.get(m) or stats.MonthlyStat(deal_type=dt, ym=m) for m in months]
        if dt.is_rent:
            charts.append(
                Chart(
                    title=f"{dt.label} 월별 전세·월세 거래량",
                    kind="bar",
                    labels=list(labels),
                    series=[("전세", [s.jeonse_count for s in rows]), ("월세", [s.wolse_count for s in rows])],
                    unit="건",
                )
            )
        else:
            charts.append(
                Chart(
                    title=f"{dt.label} 월별 거래량·중위가",
                    kind="bar+line",
                    labels=list(labels),
                    series=[
                        ("거래량", [s.count for s in rows]),
                        ("중위가", [_int_or_none(s.median_price) for s in rows]),
                    ],
                    unit="만원",
                )
            )
    return charts


def _lag_months(today: date) -> set[str]:
    """신고기한(계약 후 30일)이 끝나지 않은 달 — 이번 달과 지난달."""
    this_month = ym_of(today)
    return {this_month, ym_add(this_month, -1)}


def _notes(
    months: list[str],
    lag: set[str],
    history_start: Optional[str],
    has_sales: bool,
    has_rents: bool,
    has_jeonse: bool,
) -> list[str]:
    lag_in_period = sorted(m for m in lag if m in set(months))
    lag_text = ""
    if lag_in_period:
        span = ym_label(lag_in_period[0])
        if len(lag_in_period) > 1:
            span += f"~{ym_label(lag_in_period[-1])}"
        lag_text = f" ({span} 계약분 집계 중)"
    notes = [
        SOURCE_NOTE,
        "모든 통계는 계약일 기준입니다.",
        f"실거래 신고기한이 계약 후 30일이므로 최근 1~2개월 거래는 앞으로 더 늘어날 수 있습니다.{lag_text}",
        "해제(취소)된 거래는 건수·가격 통계에서 제외하고 '해제 거래' 표에만 표시합니다.",
        "금액 단위는 만원입니다(예: 82,500 = 8억 2,500만원). 평당가는 전용면적 기준(1평 = 3.305785㎡)입니다.",
    ]
    if has_sales:
        since = f"({ym_label(history_start)}~)" if history_start else ""
        notes.append(
            "신고가는 같은 단지·같은 면적타입(전용면적 정수부)에서 그 이전 최고가를 넘은 거래이며, "
            f"비교 기준은 수집된 자료{since}입니다. 유형이 다른 거래는 섞어 계산하지 않습니다."
        )
    if has_rents:
        notes.append("전세는 월세가 0인 전월세 거래입니다. 갱신계약 비율은 계약구분(신규/갱신)이 공개된 거래 중 비율입니다.")
    if has_jeonse:
        notes.append("전세가율 = 같은 단지·면적타입의 전세 중위 보증금 ÷ 매매 중위가 × 100 (분석 기간 내 거래)")
    return notes


# ---------------------------------------------------------------------- 조립
def build_report(
    transactions: Iterable[Transaction],
    *,
    title: str = "부동산 실거래 정리",
    regions: Iterable[Any] = (),
    period: Optional[Sequence[Any]] = None,
    new_since: Optional[datetime] = None,
    top_n: int = 20,
    today: Optional[date] = None,
) -> Report:
    """거래 목록을 분석해 ``Report`` 를 만든다.

    * ``period=(시작, 끝)`` 연월(양끝 포함)을 주면 표·KPI·차트는 그 기간 거래만 쓰고,
      **기간 이전 거래는 신고가 비교 기준(이력)으로만** 쓴다. None 이면 자료의 최소~최대 연월.
    * 유형이 다른 거래는 섞지 않는다 (모든 표에 ``유형`` 열).
    * ``new_since`` 를 주면 그 뒤에 처음 수집된 거래 표·KPI 를 더한다.
    * ``today`` 는 신고기한이 끝나지 않은 달(이번 달·지난달)을 표시하는 데 쓴다 (기본: 오늘, 한국 시간).
    """
    all_txs = list(transactions)
    if period is not None:
        start: Optional[str] = parse_ym(period[0])
        end: Optional[str] = parse_ym(period[1])
        months = month_range(start, end)  # type: ignore[arg-type]  (시작 > 끝 이면 ValueError)
    else:
        span = stats.data_period(all_txs)
        start, end = span if span else (None, None)
        months = month_range(start, end) if span else []  # type: ignore[arg-type]

    if start is None or end is None:
        txs: list[Transaction] = []
        history: list[Transaction] = []
        report_period: Optional[tuple[str, str]] = None
    else:
        txs = [t for t in all_txs if start <= t.deal_ym <= end]
        history = [t for t in all_txs if t.deal_ym <= end]
        report_period = (start, end)

    types = stats.sorted_deal_types(t.deal_type for t in txs)
    has_sales = any(dt.is_sale for dt in types)
    has_rents = any(dt.is_rent for dt in types)
    lag = _lag_months(today or today_kst())
    region_names = _region_names(regions)
    if not region_names:
        counts: dict[str, int] = {}
        for t in txs:
            name = stats.sigungu_of(t)
            counts[name] = counts.get(name, 0) + 1
        region_names = sorted(counts, key=lambda n: (-counts[n], n))

    monthly = stats.monthly_trend(txs, months, deal_types=types)
    tables = [_monthly_table(monthly, has_sales, has_rents, lag)]

    complexes = stats.complex_summary(txs)
    if complexes:
        tables.append(_complex_table(complexes))
    dongs = stats.dong_summary(txs)
    if dongs:
        tables.append(_dong_table(dongs))
    bands = stats.area_band_summary(txs)
    if bands:
        tables.append(_area_band_table(bands))

    highs = stats.find_new_highs(history, start_ym=start, end_ym=end) if report_period else []
    has_named_sales = any(t.name and t.area_type for t in stats.valid_sales(txs))
    if has_named_sales:  # 비교할 단지 매매가 있으면 0건이어도 표를 남긴다
        tables.append(_new_high_table(highs))
    cancelled = stats.cancelled_deals(txs)
    if has_sales or cancelled:  # 매매 자료가 있으면 0건이어도 남긴다
        tables.append(_cancelled_table(cancelled))

    rent_stats = stats.rent_summary(txs)
    if rent_stats:
        tables.append(_rent_table(rent_stats))
    ratios = stats.jeonse_ratios(txs)
    if ratios:
        tables.append(_jeonse_table(ratios))
    top = stats.top_sales(txs, top_n)
    if top:
        tables.append(_top_table(top, top_n))

    new_count: Optional[int] = None
    if new_since is not None:
        fresh = stats.new_deals(txs, new_since)
        new_count = len(fresh)
        tables.append(_new_deals_table(fresh, new_since))

    history_start = min((t.deal_ym for t in history), default=None)
    return Report(
        title=title,
        generated_at=now_kst(),
        period=report_period,
        regions=region_names,
        kpis=_kpis(txs, report_period, region_names, len(highs), new_count),
        tables=tables,
        charts=_charts(monthly, types, months),
        notes=_notes(months, lag, history_start, has_sales, has_rents, bool(ratios)),
    )
