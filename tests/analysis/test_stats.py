from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from silgeorae.analysis import stats
from silgeorae.models import DealType, PropertyType
from silgeorae.utils import KST

APT, APT_RENT, OFFI, OFFI_RENT, PRESALE, LAND = (
    DealType.APT_SALE,
    DealType.APT_RENT,
    DealType.OFFI_SALE,
    DealType.OFFI_RENT,
    DealType.PRESALE,
    DealType.LAND,
)


# ---------------------------------------------------------------------- 기본
@pytest.mark.parametrize(
    "area, band",
    [
        (59.99, "소형(60㎡ 이하)"),
        (60, "소형(60㎡ 이하)"),
        (60.0, "소형(60㎡ 이하)"),
        (60.01, "중소형(60~85㎡)"),
        (85, "중소형(60~85㎡)"),
        (85.01, "중대형(85~135㎡)"),
        (135, "중대형(85~135㎡)"),
        (135.5, "대형(135㎡ 초과)"),
        (None, "면적 미상"),
        (0, "면적 미상"),
    ],
)
def test_area_band_boundaries(area, band):
    assert stats.area_band(area) == band


def test_median_mean_percent_helpers():
    assert stats.median_of([3, None, 1, 2]) == 2
    assert stats.median_of([1, 2]) == 1.5
    assert stats.median_of([]) is None and stats.median_of([None]) is None
    assert stats.mean_of([1, 2, None]) == 1.5
    assert stats.mean_of([]) is None
    assert stats.percent(1, 4) == 25
    assert stats.percent(1, 0) is None


def test_filter_and_data_period(make_tx):
    txs = [make_tx(when="2024-12-31"), make_tx(when="2025-01-01"), make_tx(when="2025-03-05")]
    assert stats.data_period(txs) == ("202412", "202503")
    assert stats.data_period([]) is None
    assert [t.deal_ym for t in stats.filter_period(txs, "2025-01", "2025.02")] == ["202501"]
    assert len(stats.filter_period(txs, start_ym="202501")) == 2
    assert len(stats.filter_period(txs)) == 3


# ---------------------------------------------------------------------- 월별 추이
def test_monthly_trend_sales_excludes_cancelled(make_tx):
    txs = [
        make_tx(when="2025-01-03", price=90000, area_m2=33.05785),  # 10평 → 평당 9,000
        make_tx(when="2025-01-10", price=110000, area_m2=33.05785),
        make_tx(when="2025-01-20", price=130000, area_m2=66.1157),  # 20평 → 6,500
        make_tx(when="2025-01-25", price=999999, is_cancelled=True, cancel_date=date(2025, 2, 1)),
        make_tx(when="2025-03-02", price=100000),
    ]
    rows = stats.monthly_trend(txs, ["202501", "202502", "202503"])
    assert [(r.ym, r.count, r.cancelled) for r in rows] == [("202501", 3, 1), ("202502", 0, 0), ("202503", 1, 0)]
    jan = rows[0]
    assert jan.median_price == 110000
    assert jan.mean_price == pytest.approx(110000)
    assert jan.mean_price_per_pyeong == pytest.approx((9000 + 11000 + 6500) / 3)
    assert rows[1].median_price is None and rows[1].mean_price_per_pyeong is None
    assert rows[0].jeonse_count == 0 and rows[0].jeonse_median_deposit is None


def test_monthly_trend_rents_and_type_separation(make_tx):
    txs = [
        make_tx(APT_RENT, "2025-01-05", deposit=50000, monthly_rent=0),
        make_tx(APT_RENT, "2025-01-06", deposit=70000, monthly_rent=None),  # 월세 없음 → 전세
        make_tx(APT_RENT, "2025-01-07", deposit=10000, monthly_rent=100),
        make_tx(APT_RENT, "2025-01-08", deposit=20000, monthly_rent=200),
        make_tx(APT, "2025-01-09", price=200000),
    ]
    rows = stats.monthly_trend(txs)
    assert [(r.deal_type, r.ym) for r in rows] == [(APT, "202501"), (APT_RENT, "202501")]
    sale, rent = rows
    assert sale.count == 1 and sale.median_price == 200000
    assert rent.count == 4 and rent.median_price is None
    assert (rent.jeonse_count, rent.wolse_count) == (2, 2)
    assert rent.jeonse_median_deposit == 60000
    assert rent.wolse_mean_deposit == 15000
    assert rent.wolse_mean_rent == 150


def test_monthly_trend_deal_types_filter_keeps_empty_types(make_tx):
    rows = stats.monthly_trend([make_tx()], ["202501"], deal_types=[OFFI, APT])
    assert [(r.deal_type, r.count) for r in rows] == [(APT, 1), (OFFI, 0)]


# ---------------------------------------------------------------------- 단지별 · 법정동별 · 면적대별
def test_complex_summary_groups_and_sorts(make_tx):
    txs = [
        make_tx(when="2025-01-03", price=100000, floor=3),
        make_tx(when="2025-02-03", price=120000, area_m2=84.99),
        make_tx(when="2025-03-03", price=120000, area_m2=84.97),  # 같은 최고가 → 먼저 기록한 날
        make_tx(when="2025-03-20", price=90000, area_m2=84.97),
        make_tx(when="2025-03-25", price=500000, is_cancelled=True),  # 해제 → 제외
        make_tx(when="2025-02-01", price=70000, area_m2=59.99),  # 다른 면적타입
        make_tx(when="2025-02-01", name="가나다", jibun="1", price=50000),
        make_tx(LAND, "2025-02-01", name="", jibun="1**", price=3000, area_m2=330.0),  # 이름 없음 → 제외
    ]
    result = stats.complex_summary(txs)
    assert [(s.name, s.area_type, s.count) for s in result] == [
        ("한빛마을1단지", 84, 4),
        ("가나다", 84, 1),
        ("한빛마을1단지", 59, 1),
    ]
    top = result[0]
    assert top.latest_date == date(2025, 3, 20) and top.latest_price == 90000
    assert top.max_price == 120000 and top.max_date == date(2025, 2, 3)
    assert top.min_price == 90000 and top.min_date == date(2025, 3, 20)
    assert top.area_m2 == 84.97  # 가장 많이 거래된 면적
    assert top.pyeong == pytest.approx(25.70, abs=0.01)
    assert top.build_year == 2015 and top.sigungu == "강남구"
    assert len(stats.complex_summary(txs, named_only=False)) == 4


def test_dong_and_area_band_summary(make_tx):
    txs = [
        make_tx(dong="대치동", price=100000, area_m2=59.0),
        make_tx(dong="대치동", price=200000, area_m2=84.0),
        make_tx(dong="대치동", price=300000, area_m2=84.5),
        make_tx(dong="역삼동", price=50000, area_m2=140.0),
        make_tx(dong="역삼동", price=1, is_cancelled=True),
        make_tx(OFFI, dong="역삼동", price=30000, area_m2=30.0),
        make_tx(LAND, dong="역삼동", price=90000, area_m2=500.0),  # 토지는 면적대 제외
    ]
    dongs = stats.dong_summary(txs)
    assert [(d.deal_type, d.dong, d.count, d.median_price) for d in dongs] == [
        (APT, "대치동", 3, 200000),
        (APT, "역삼동", 1, 50000),
        (OFFI, "역삼동", 1, 30000),
        (LAND, "역삼동", 1, 90000),
    ]
    bands = stats.area_band_summary(txs)
    assert [(b.deal_type, b.band, b.count) for b in bands] == [
        (APT, "소형(60㎡ 이하)", 1),
        (APT, "중소형(60~85㎡)", 2),
        (APT, "대형(135㎡ 초과)", 1),
        (OFFI, "소형(60㎡ 이하)", 1),
    ]
    assert bands[1].share == pytest.approx(50.0)
    assert bands[1].median_price == 250000


# ---------------------------------------------------------------------- 신고가
def test_new_high_rules(make_tx):
    txs = [
        make_tx(when="2025-01-10", price=100000),  # 첫 거래 → 신고가 아님
        make_tx(when="2025-02-10", price=100000),  # 같은 값 → 아님
        make_tx(when="2025-03-10", price=99000),
        make_tx(when="2025-04-10", price=100001),  # 이전 최고 100,000 초과 → 신고가
        make_tx(when="2025-05-10", price=150000, is_cancelled=True),  # 해제 → 비교 기준에도 안 들어감
        make_tx(when="2025-06-10", price=120000),  # 100,001 초과 → 신고가
    ]
    highs = stats.find_new_highs(txs)
    assert [(h.tx.deal_date, h.prev_price, h.prev_date) for h in highs] == [
        (date(2025, 6, 10), 100001, date(2025, 4, 10)),
        (date(2025, 4, 10), 100000, date(2025, 1, 10)),
    ]
    assert highs[0].increase == 19999
    assert highs[0].increase_pct == pytest.approx(19999 / 100001 * 100)


def test_new_high_same_day_deals_compare_only_with_earlier_days(make_tx):
    txs = [
        make_tx(when="2025-01-10", price=100000),
        make_tx(when="2025-01-10", price=110000),  # 같은 날 첫 거래끼리는 비교하지 않음
        make_tx(when="2025-02-01", price=105000),
        make_tx(when="2025-02-01", price=111000),  # 기준 110,000 → 신고가
        make_tx(when="2025-02-01", price=112000),  # 같은 날 111,000 과는 비교 안 함 → 110,000 기준 신고가
    ]
    highs = stats.find_new_highs(txs)
    assert sorted(h.tx.price for h in highs) == [111000, 112000]
    assert {h.prev_price for h in highs} == {110000}


def test_new_high_history_seeds_baseline_but_only_period_reported(make_tx):
    txs = [
        make_tx(when="2024-10-05", price=80000),
        make_tx(when="2024-11-05", price=100000),  # 기간 전 신고가 → 보고하지 않음, 기준은 된다
        make_tx(when="2025-01-05", price=95000),  # 이력보다 낮음 → 아님
        make_tx(when="2025-02-05", price=101000),  # 이력 최고 100,000 초과 → 신고가
        make_tx(when="2025-04-05", price=200000),  # 기간 뒤 → 보고 안 함
    ]
    highs = stats.find_new_highs(txs, start_ym="2025-01", end_ym="2025-03")
    assert [(h.tx.price, h.prev_price, h.prev_date) for h in highs] == [(101000, 100000, date(2024, 11, 5))]
    assert len(stats.find_new_highs(txs)) == 3


def test_new_high_groups_do_not_mix(make_tx):
    txs = [
        make_tx(when="2025-01-05", price=100000),
        make_tx(when="2025-02-05", price=150000, area_m2=59.9),  # 다른 면적타입 → 첫 거래
        make_tx(PRESALE, "2025-03-05", price=200000),  # 분양권은 아파트 매매와 섞지 않음
        make_tx(OFFI, "2025-03-06", price=300000),  # 오피스텔도 별도
        make_tx(when="2025-03-07", price=120000, name="다른단지"),
        make_tx(LAND, "2025-01-01", name="", jibun="1**", price=1000),
        make_tx(LAND, "2025-02-01", name="", jibun="1**", price=5000),  # 이름 없는 토지 → 판정 안 함
    ]
    assert stats.find_new_highs(txs) == []


# ---------------------------------------------------------------------- 해제 · 상위 · 신규
def test_cancelled_deals_listed_newest_first(make_tx):
    txs = [
        make_tx(when="2025-01-05", is_cancelled=True, cancel_date=date(2025, 2, 1)),
        make_tx(when="2025-01-06"),
        make_tx(when="2025-01-07", is_cancelled=True, cancel_date=date(2025, 3, 1)),
        make_tx(when="2025-01-08", is_cancelled=True),  # 해제일 없음 → 계약일로 정렬
    ]
    result = stats.cancelled_deals(txs)
    assert [t.deal_date.day for t in result] == [7, 5, 8]


def test_top_sales_per_type(make_tx):
    txs = [make_tx(price=p) for p in (100, 300, 200)] + [
        make_tx(price=999, is_cancelled=True),
        make_tx(OFFI, price=50),
        make_tx(APT_RENT, deposit=10**6),
    ]
    top = stats.top_sales(txs, 2)
    assert [(t.deal_type, t.price) for t in top] == [(APT, 300), (APT, 200), (OFFI, 50)]
    assert [t.price for t in stats.top_sales(txs, 1, per_type=False)] == [300]
    assert stats.top_sales(txs, 0) == []


def test_new_deals_since_handles_timezones(make_tx):
    since_naive = datetime(2025, 2, 1, 9, 0)  # 한국 시간으로 본다
    txs = [
        make_tx(when="2025-01-01", first_seen_at=datetime(2025, 2, 1, 0, 0, tzinfo=timezone.utc)),  # = 09:00 KST
        make_tx(when="2025-01-02", first_seen_at=datetime(2025, 1, 31, 23, 0, tzinfo=timezone.utc)),  # 08:00 KST
        make_tx(when="2025-01-03", first_seen_at=datetime(2025, 2, 3, 10, 0)),
        make_tx(when="2025-01-04", first_seen_at=datetime(2025, 2, 3, 11, 0, tzinfo=KST), is_cancelled=True),
        make_tx(when="2025-01-05"),  # 수집 시각 없음
    ]
    result = stats.new_deals(txs, since_naive)
    assert [t.deal_date.day for t in result] == [3, 1]
    assert len(stats.new_deals(txs, since_naive.replace(tzinfo=KST), include_cancelled=True)) == 3
    assert stats.new_deals(txs, datetime.now(KST) + timedelta(days=1)) == []


# ---------------------------------------------------------------------- 전월세 · 전세가율
def test_rent_summary_ratios(make_tx):
    txs = [
        make_tx(APT_RENT, "2025-01-05", deposit=50000, monthly_rent=0, contract_type="신규"),
        make_tx(APT_RENT, "2025-01-06", deposit=60000, monthly_rent=None, contract_type="갱신", renewal_right_used=True),
        make_tx(APT_RENT, "2025-01-07", deposit=10000, monthly_rent=100, contract_type="갱신"),
        make_tx(APT_RENT, "2025-01-08", deposit=20000, monthly_rent=150, contract_type=""),
        make_tx(APT_RENT, "2025-02-08", deposit=30000, monthly_rent=0, contract_type="신규"),
        make_tx(APT, "2025-01-09"),  # 매매는 무시
    ]
    rows = stats.rent_summary(txs)
    assert [(r.deal_type, r.ym, r.count) for r in rows] == [
        (APT_RENT, "202501", 4),
        (APT_RENT, "202502", 1),
        (APT_RENT, "", 5),
    ]
    jan = rows[0]
    assert (jan.jeonse_count, jan.wolse_count) == (2, 2)
    assert jan.jeonse_share == 50 and jan.wolse_share == 50
    assert jan.jeonse_mean_deposit == 55000
    assert jan.wolse_mean_deposit == 15000 and jan.wolse_mean_rent == 125
    assert jan.contract_known == 3 and jan.renewal_count == 2
    assert jan.renewal_ratio == pytest.approx(200 / 3)
    assert jan.renewal_right_ratio == 50
    total = rows[-1]
    assert total.jeonse_count == 3 and total.jeonse_share == 60
    assert stats.rent_summary(txs, include_total=False)[-1].ym == "202502"
    empty = stats.RentStat(deal_type=APT_RENT, ym="")
    assert empty.jeonse_share is None and empty.renewal_ratio is None and empty.renewal_right_ratio is None


def test_jeonse_ratio_math_and_pairing(make_tx):
    txs = [
        make_tx(APT, price=100000),
        make_tx(APT, price=120000),
        make_tx(APT, price=110000),
        make_tx(APT, price=300000, is_cancelled=True),  # 해제 → 제외
        make_tx(APT_RENT, deposit=60000, monthly_rent=0),
        make_tx(APT_RENT, deposit=70000, monthly_rent=None),
        make_tx(APT_RENT, deposit=10000, monthly_rent=100),  # 월세 → 제외
        make_tx(APT_RENT, deposit=40000, monthly_rent=0, area_m2=59.9),  # 다른 면적타입, 매매 없음
        make_tx(APT, price=90000, name="매매만"),  # 전세 없음
        make_tx(OFFI_RENT, deposit=90000, monthly_rent=0),  # 오피스텔 전세는 아파트 매매와 짝짓지 않음
    ]
    result = stats.jeonse_ratios(txs)
    assert len(result) == 1
    r = result[0]
    assert r.property_type is PropertyType.APT
    assert (r.sale_type, r.rent_type) == (APT, APT_RENT)
    assert (r.sale_count, r.sale_median, r.jeonse_count, r.jeonse_median) == (3, 110000, 2, 65000)
    assert r.ratio == pytest.approx(65000 / 110000 * 100)
    assert r.area_type == 84 and r.name == "한빛마을1단지"
