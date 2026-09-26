from __future__ import annotations

from datetime import date, datetime

import pytest

from silgeorae.analysis import Chart, Report, Table, build_report, format_cell, transactions_table
from silgeorae.analysis.report import FORMATS, SOURCE_NOTE, TRANSACTION_COLUMNS
from silgeorae.models import DealType
from silgeorae.utils import KST

APT, APT_RENT, OFFI = DealType.APT_SALE, DealType.APT_RENT, DealType.OFFI_SALE
TODAY = date(2025, 9, 26)  # 최근 달(2025.08~09) 표시가 테스트 자료와 겹치지 않게


def _sample(make_tx):
    ten_pyeong = 33.05785  # 정확히 10평
    return [
        make_tx(APT, "2025-01-05", price=82500, area_m2=ten_pyeong),
        make_tx(APT, "2025-01-20", price=80000, area_m2=ten_pyeong),
        make_tx(APT, "2025-02-11", price=90000, area_m2=ten_pyeong),  # 신고가 (이전 최고 82,500)
        make_tx(APT, "2025-02-12", price=40000, is_cancelled=True, cancel_date=date(2025, 3, 2), area_m2=ten_pyeong),
        make_tx(APT_RENT, "2025-01-07", deposit=50000, monthly_rent=0, area_m2=ten_pyeong, contract_type="신규"),
        make_tx(APT_RENT, "2025-02-07", deposit=45000, monthly_rent=0, area_m2=ten_pyeong, contract_type="갱신",
                renewal_right_used=True),
        make_tx(APT_RENT, "2025-02-08", deposit=5000, monthly_rent=120, area_m2=ten_pyeong),
    ]


# ---------------------------------------------------------------------- 모델
def test_models_helpers():
    t = Table(name="표", columns=["a", "b"], rows=[[1, 2], [3, 4]])
    assert t.formats == {} and t.description == ""
    assert t.column("b") == [2, 4]
    assert t.as_dicts() == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    c = Chart(title="c", kind="bar", labels=["2025.01"], series=[("x", [1])])
    assert c.unit == ""
    r = Report("t", datetime(2025, 1, 1, tzinfo=KST), None, [], [("k", "v")], [t], [c], [])
    assert r.table("표") is t and r.table("없음") is None
    assert r.kpi("k") == "v" and r.kpi("x") is None
    assert r.period_text == "-"


@pytest.mark.parametrize(
    "value, fmt, text",
    [
        (82500, "manwon", "82,500"),
        (82500.5, "manwon", "82,501"),
        (1234, "int", "1,234"),
        (65.33, "percent", "65.3%"),
        (84.9712, "float2", "84.97"),
        (25.66, "float1", "25.7"),
        (2015, "text", "2015"),
        (2015, "", "2015"),
        (84.9, "", "84.9"),
        (date(2025, 1, 3), "date", "2025-01-03"),
        (datetime(2025, 1, 3, 14, 5), "date", "2025-01-03 14:05"),
        (None, "manwon", ""),
        (True, "", "O"),
        ("강남구", "text", "강남구"),
    ],
)
def test_format_cell(value, fmt, text):
    assert format_cell(value, fmt) == text


# ---------------------------------------------------------------------- build_report
def test_build_report_empty_input():
    report = build_report([], today=TODAY)
    assert report.period is None
    assert [t.name for t in report.tables] == ["월별 추이"]
    assert report.tables[0].rows == []
    assert report.charts == []
    assert report.kpi("매매 건수") == "0건"
    assert report.kpi("매매 중위가") == "-"
    assert report.kpi("평균 평당가") == "-"
    assert report.kpi("전월세 건수") == "0건 (전세 0 · 월세 0)"
    assert report.kpi("기간") == "-" and report.kpi("지역") == "-"
    assert report.kpi("신규 등록 건수") is None
    assert SOURCE_NOTE in report.notes
    assert report.generated_at.tzinfo is not None


def test_build_report_empty_with_period_and_new_since():
    report = build_report([], period=("2025-01", "2025-03"), new_since=datetime(2025, 3, 1), today=TODAY)
    assert report.period == ("202501", "202503")
    assert report.kpi("기간") == "2025.01 ~ 2025.03"
    assert report.kpi("신규 등록 건수") == "0건"
    assert [t.name for t in report.tables] == ["월별 추이", "신규 등록 거래"]
    assert report.charts == []


def test_build_report_invalid_period():
    with pytest.raises(ValueError):
        build_report([], period=("2025-03", "2025-01"))
    with pytest.raises(ValueError):
        build_report([], period=("2025-13", "2025-14"))


def test_kpi_strings(make_tx):
    report = build_report(_sample(make_tx), regions=["서울 강남구"], today=TODAY)
    kpis = dict(report.kpis)
    assert list(kpis) == ["기간", "지역", "매매 건수", "해제 건수", "매매 중위가", "평균 평당가", "전월세 건수", "신고가 건수"]
    assert kpis["기간"] == "2025.01 ~ 2025.02"
    assert kpis["지역"] == "서울 강남구"
    assert kpis["매매 건수"] == "3건"
    assert kpis["해제 건수"] == "1건"
    assert kpis["매매 중위가"] == "8억 2,500만원"
    assert kpis["평균 평당가"] == "8,417만원/평"  # (8,250 + 8,000 + 9,000) / 3
    assert kpis["전월세 건수"] == "3건 (전세 2 · 월세 1)"
    assert kpis["신고가 건수"] == "1건"
    assert report.regions == ["서울 강남구"]


def test_kpi_exact_price_per_pyeong_and_new_since(make_tx):
    txs = [
        make_tx(when="2025-01-05", price=73210, area_m2=33.05785, first_seen_at=datetime(2025, 2, 1, 10, tzinfo=KST)),
        make_tx(when="2025-01-06", price=73210, area_m2=33.05785, first_seen_at=datetime(2025, 1, 10, 10, tzinfo=KST)),
    ]
    report = build_report(txs, new_since=datetime(2025, 2, 1), today=TODAY)
    assert report.kpi("평균 평당가") == "7,321만원/평"
    assert report.kpi("신규 등록 건수") == "1건"
    table = report.table("신규 등록 거래")
    assert table is not None and len(table.rows) == 1
    assert table.as_dicts()[0]["최초수집"] == "2025-02-01 10:00"


def test_regions_derived_from_data_when_not_given(make_tx):
    txs = [make_tx(sigungu="서초구", lawd_cd="11650"), make_tx(), make_tx(), make_tx(sigungu="", lawd_cd="41135")]
    report = build_report(txs, today=TODAY)
    assert report.regions == ["강남구", "41135", "서초구"]
    assert report.kpi("지역") == "강남구, 41135, 서초구"
    many = build_report(txs, regions=["a", "b", "c", "d", "e"], today=TODAY)
    assert many.kpi("지역") == "a, b, c 외 2곳"


def test_period_filtering_and_history(make_tx):
    txs = [
        make_tx(when="2024-11-05", price=100000),  # 이력 (기간 전)
        make_tx(when="2024-12-05", price=100000, name="다른단지"),
        make_tx(when="2025-01-05", price=99000),
        make_tx(when="2025-02-05", price=101000),  # 이력 기준 신고가
        make_tx(when="2025-03-05", price=200000),  # 기간 뒤 → 전부 제외
        make_tx(APT_RENT, "2024-12-01", deposit=1),
    ]
    report = build_report(txs, period=("2025-01", "2025-02"), today=TODAY)
    assert report.period == ("202501", "202502")
    monthly = report.table("월별 추이")
    assert monthly.column("계약월") == ["2025.01", "2025.02"]
    assert monthly.column("유형") == ["아파트 매매", "아파트 매매"]  # 기간 안에 전월세 없음
    assert "전세 건수" not in monthly.columns
    assert report.kpi("매매 건수") == "2건"
    highs = report.table("신고가")
    assert highs.as_dicts() == [
        {
            "유형": "아파트 매매",
            "시군구": "강남구",
            "법정동": "대치동",
            "단지명": "한빛마을1단지",
            "전용면적(㎡)": 84.97,
            "평": 25.7,
            "층": 10,
            "계약일": date(2025, 2, 5),
            "거래금액(만원)": 101000,
            "이전 최고가(만원)": 100000,
            "이전 최고가 거래일": date(2024, 11, 5),
            "상승액(만원)": 1000,
            "상승률(%)": 1.0,
        }
    ]
    complexes = report.table("단지별 요약")
    assert complexes.column("단지명") == ["한빛마을1단지"] and complexes.column("건수") == [2]
    assert report.table("전월세 요약") is None and report.table("전세가율") is None
    assert any("2024.11~" in note for note in report.notes)  # 신고가 비교 기준 시작


def test_table_set_and_formats(make_tx):
    report = build_report(_sample(make_tx), today=TODAY, top_n=5)
    names = [t.name for t in report.tables]
    assert names == [
        "월별 추이",
        "단지별 요약",
        "법정동별",
        "면적대별",
        "신고가",
        "해제 거래",
        "전월세 요약",
        "전세가율",
        "고가 거래 TOP5",
    ]
    assert len(set(names)) == len(names) and all(len(n) <= 31 for n in names)
    for table in report.tables:
        assert "유형" in table.columns, table.name
        assert set(table.formats) <= set(table.columns), table.name
        assert set(table.formats.values()) <= set(FORMATS), table.name
        assert all(len(row) == len(table.columns) for row in table.rows), table.name
    monthly = report.table("월별 추이")
    assert monthly.formats["중위가(만원)"] == "manwon" and monthly.formats["거래 건수"] == "int"
    assert report.table("전세가율").formats["전세가율(%)"] == "percent"
    assert report.table("단지별 요약").formats["최근 거래일"] == "date"


def test_rent_only_input_omits_sale_tables(make_tx):
    txs = [make_tx(APT_RENT, deposit=50000), make_tx(APT_RENT, deposit=1000, monthly_rent=50)]
    report = build_report(txs, today=TODAY)
    assert [t.name for t in report.tables] == ["월별 추이", "전월세 요약"]
    assert "중위가(만원)" not in report.table("월별 추이").columns
    assert [c.kind for c in report.charts] == ["bar"]
    assert report.kpi("매매 중위가") == "-"


def test_sales_without_events_keep_event_tables_empty(make_tx):
    report = build_report([make_tx(when="2025-01-05")], today=TODAY)
    assert report.table("신고가").rows == []
    assert report.table("해제 거래").rows == []
    assert report.table("전월세 요약") is None


def test_mixed_deal_types_are_not_blended(make_tx):
    txs = [
        make_tx(APT, "2025-01-05", price=100000),
        make_tx(APT, "2025-01-06", price=90000),
        # 같은 단지 키·면적타입이지만 오피스텔 — 섞으면 150,000 이 아파트 최고가를 넘는 '신고가'가 된다
        make_tx(OFFI, "2025-01-07", price=150000, area_m2=84.97),
        make_tx(OFFI, "2025-01-20", price=20000, area_m2=84.97),
    ]
    report = build_report(txs, today=TODAY)
    monthly = report.table("월별 추이").as_dicts()
    assert [(r["유형"], r["거래 건수"], r["중위가(만원)"]) for r in monthly] == [
        ("아파트 매매", 2, 95000),
        ("오피스텔 매매", 2, 85000),
    ]
    assert report.kpi("매매 중위가") == "아파트 9억 5,000만원 · 오피스텔 8억 5,000만원"
    assert report.kpi("매매 건수") == "4건"
    complexes = report.table("단지별 요약").as_dicts()
    assert sorted((r["유형"], r["건수"]) for r in complexes) == [("아파트 매매", 2), ("오피스텔 매매", 2)]
    assert report.table("신고가").rows == []
    assert report.kpi("신고가 건수") == "0건"
    top = report.table("고가 거래 TOP20").as_dicts()
    assert [(r["순위"], r["유형"]) for r in top] == [(1, "아파트 매매"), (2, "아파트 매매"), (1, "오피스텔 매매"), (2, "오피스텔 매매")]
    assert [c.title for c in report.charts] == ["아파트 매매 월별 거래량·중위가", "오피스텔 매매 월별 거래량·중위가"]


def test_charts_cover_whole_period(make_tx):
    txs = _sample(make_tx) + [make_tx(APT, "2025-04-03", price=70000)]
    report = build_report(txs, today=TODAY)
    sale, rent = report.charts
    labels = ["2025.01", "2025.02", "2025.03", "2025.04"]
    assert (sale.kind, sale.unit, sale.labels) == ("bar+line", "만원", labels)
    assert sale.series == [("거래량", [2, 1, 0, 1]), ("중위가", [81250, 90000, None, 70000])]
    assert (rent.kind, rent.unit, rent.labels) == ("bar", "건", labels)
    assert rent.series == [("전세", [1, 1, 0, 0]), ("월세", [0, 1, 0, 0])]


def test_monthly_table_rows_and_lag_remark(make_tx):
    txs = [make_tx(when="2025-08-10"), make_tx(when="2025-09-01"), make_tx(APT_RENT, "2025-09-02", deposit=1000, monthly_rent=10)]
    report = build_report(txs, period=("2025-07", "2025-09"), today=TODAY)
    rows = report.table("월별 추이").as_dicts()
    assert [(r["유형"], r["계약월"], r["비고"]) for r in rows] == [
        ("아파트 매매", "2025.07", ""),
        ("아파트 매매", "2025.08", "집계 중"),
        ("아파트 매매", "2025.09", "집계 중"),
        ("아파트 전월세", "2025.07", ""),
        ("아파트 전월세", "2025.08", "집계 중"),
        ("아파트 전월세", "2025.09", "집계 중"),
    ]
    assert rows[0]["거래 건수"] == 0 and rows[0]["중위가(만원)"] is None
    assert rows[5]["월세 건수"] == 1 and rows[5]["월세 평균 월세(만원)"] == 10
    assert rows[5]["중위가(만원)"] is None and rows[1]["전세 건수"] is None
    assert any("2025.08~2025.09" in note for note in report.notes)


def test_rent_and_jeonse_tables(make_tx):
    report = build_report(_sample(make_tx), today=TODAY)
    rent = report.table("전월세 요약").as_dicts()
    assert [r["계약월"] for r in rent] == ["2025.01", "2025.02", "전체"]
    total = rent[-1]
    assert (total["전월세 건수"], total["전세 건수"], total["월세 건수"]) == (3, 2, 1)
    assert total["전세 비중(%)"] == 66.7 and total["월세 비중(%)"] == 33.3
    assert total["갱신계약 비율(%)"] == 50.0 and total["갱신요구권 사용 비율(%)"] == 100.0
    assert total["전세 평균 보증금(만원)"] == 47500 and total["평균 월세(만원)"] == 120
    jeonse = report.table("전세가율").as_dicts()
    assert len(jeonse) == 1
    assert jeonse[0]["유형"] == "아파트"
    assert (jeonse[0]["매매 중위가(만원)"], jeonse[0]["전세 중위 보증금(만원)"]) == (82500, 47500)
    assert jeonse[0]["전세가율(%)"] == round(47500 / 82500 * 100, 1)


def test_cancelled_table_lists_cancellations(make_tx):
    report = build_report(_sample(make_tx), today=TODAY)
    rows = report.table("해제 거래").as_dicts()
    assert len(rows) == 1
    assert rows[0]["해제일"] == date(2025, 3, 2) and rows[0]["해제까지(일)"] == 18
    assert rows[0]["거래금액(만원)"] == 40000


def test_notes_have_source_and_caveats(make_tx):
    notes = " ".join(build_report(_sample(make_tx), today=TODAY).notes)
    for phrase in ("국토교통부 실거래가 공개시스템(공공데이터포털 OpenAPI)", "계약일 기준", "30일", "해제", "만원", "전세가율"):
        assert phrase in notes


def test_region_objects_are_accepted(make_tx):
    class FakeRegion:
        name = "서울특별시 강남구"

    report = build_report([make_tx()], regions=[FakeRegion(), "서울특별시 강남구"], today=TODAY)
    assert report.regions == ["서울특별시 강남구"]


# ---------------------------------------------------------------------- 거래내역 표
def test_transactions_table_columns_and_values(make_tx):
    txs = [
        make_tx(APT_RENT, "2025-01-10", deposit=10000, monthly_rent=120, contract_type="갱신", renewal_right_used=True,
                contract_term="25.02~27.02", prev_deposit=9000, prev_monthly_rent=100),
        make_tx(APT, "2025-01-05", price=285000, is_cancelled=True, cancel_date=date(2025, 1, 28),
                registration_date=None, deal_method="중개거래", building_dong="101", road_address="샘플로 51",
                first_seen_at=datetime(2025, 1, 31, 15, 30, tzinfo=KST)),
        make_tx(APT, "2025-01-20", price=290000),
    ]
    table = transactions_table(txs)
    assert table.name == "거래내역"
    assert table.columns == list(TRANSACTION_COLUMNS)
    assert table.columns[:10] == ["유형", "시군구", "법정동", "지번", "단지명", "동", "층", "전용면적(㎡)", "평", "계약일"]
    assert table.columns[-1] == "최초수집"
    rows = table.as_dicts()
    # 유형 순 → 최근 계약일 순
    assert [(r["유형"], r["계약일"]) for r in rows] == [
        ("아파트 매매", date(2025, 1, 20)),
        ("아파트 매매", date(2025, 1, 5)),
        ("아파트 전월세", date(2025, 1, 10)),
    ]
    sale = rows[1]
    assert sale["거래금액(만원)"] == 285000 and sale["해제여부"] == "O" and sale["해제일"] == date(2025, 1, 28)
    assert sale["동"] == "101" and sale["도로명주소"] == "샘플로 51" and sale["전월세"] == ""
    assert sale["최초수집"] == "2025-01-31 15:30" and sale["평"] == 25.7 and sale["전용면적(㎡)"] == 84.97
    rent = rows[2]
    assert (rent["보증금(만원)"], rent["월세(만원)"], rent["전월세"]) == (10000, 120, "월세")
    assert (rent["계약구분"], rent["갱신요구권"], rent["종전보증금(만원)"]) == ("갱신", "사용", 9000)
    assert rows[0]["해제여부"] == "" and rows[0]["최초수집"] == ""
    assert table.formats["거래금액(만원)"] == "manwon" and table.formats["계약일"] == "date"
    assert table.formats["전용면적(㎡)"] == "float2" and table.formats["층"] == "int"
    assert table.formats.get("건축년도") == "text"
    assert transactions_table([], name="원자료").name == "원자료"
