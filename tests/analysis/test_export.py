from __future__ import annotations

import csv
import re
import sys
from datetime import date, datetime

import pytest

from silgeorae.analysis import (
    Chart,
    Report,
    Table,
    build_report,
    export_csv,
    export_excel,
    export_html,
    export_tables_csv,
)
from silgeorae.analysis.csv_export import csv_value, safe_filename
from silgeorae.analysis.excel import MISSING_OPENPYXL, column_widths, sheet_title, text_width
from silgeorae.analysis.html import CAP_NOTE, render_html
from silgeorae.analysis.report import TRANSACTION_COLUMNS
from silgeorae.analysis.svg import manwon_tick, nice_ticks, render_chart_svg, short_label
from silgeorae.errors import ExportError
from silgeorae.models import DealType
from silgeorae.utils import KST

APT, APT_RENT = DealType.APT_SALE, DealType.APT_RENT
TODAY = date(2025, 9, 26)
XSS = "<script>alert(1)</script>"


@pytest.fixture
def txs(make_tx):
    return [
        make_tx(APT, "2025-01-03", price=285000, dong="대치동", first_seen_at=datetime(2025, 2, 1, 9, 30, tzinfo=KST)),
        make_tx(APT, "2025-02-03", price=295000, registration_date=date(2025, 3, 10)),
        make_tx(APT, "2025-02-10", price=150000, is_cancelled=True, cancel_date=date(2025, 2, 20)),
        make_tx(APT, "2025-02-11", price=99000, name=XSS, jibun="7"),
        make_tx(APT_RENT, "2025-01-08", deposit=150000, monthly_rent=0, contract_type="신규"),
        make_tx(APT_RENT, "2025-02-21", deposit=100000, monthly_rent=120, contract_type="갱신", renewal_right_used=True),
    ]


@pytest.fixture
def report(txs):
    return build_report(txs, title="강남구 실거래 정리", regions=["서울 강남구"], today=TODAY, new_since=datetime(2025, 1, 1))


# ---------------------------------------------------------------------- CSV
def test_export_csv_bom_header_and_roundtrip(tmp_path, txs):
    path = export_csv(txs, tmp_path / "nested" / "dir" / "거래.csv")
    assert path == tmp_path / "nested" / "dir" / "거래.csv" and path.exists()
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw[3:].decode("utf-8").startswith("유형,시군구,법정동,지번,단지명")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == list(TRANSACTION_COLUMNS)
    assert len(rows) == 1 + len(txs)
    records = [dict(zip(rows[0], r)) for r in rows[1:]]
    first = next(r for r in records if r["계약일"] == "2025-01-03")
    assert first["거래금액(만원)"] == "285000" and first["전용면적(㎡)"] == "84.97" and first["평"] == "25.7"
    assert first["최초수집"] == "2025-02-01 09:30" and first["보증금(만원)"] == ""
    cancelled = next(r for r in records if r["해제여부"] == "O")
    assert cancelled["해제일"] == "2025-02-20"
    assert any(r["단지명"] == XSS for r in records)
    rent = next(r for r in records if r["월세(만원)"] == "120")
    assert rent["전월세"] == "월세" and rent["갱신요구권"] == "사용"


def test_csv_value_and_formula_guard():
    assert csv_value(None) == "" and csv_value(True) == "O" and csv_value(False) == ""
    assert csv_value(date(2025, 1, 3)) == "2025-01-03"
    assert csv_value(84.97) == "84.97" and csv_value(1234567.5) == "1234567.5" and csv_value(3.0) == "3"
    assert csv_value(-1) == "-1" and csv_value("-1") == "-1"
    assert csv_value("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert csv_value("+82") == "'+82" and csv_value("@SUM") == "'@SUM" and csv_value("-cmd") == "'-cmd"
    assert csv_value("한빛마을1단지") == "한빛마을1단지"


def test_export_tables_csv(tmp_path, report):
    paths = export_tables_csv(report, tmp_path / "tables")
    assert len(paths) == len(report.tables)
    assert [p.name for p in paths][:2] == ["01_월별_추이.csv", "02_단지별_요약.csv"]
    for path, table in zip(paths, report.tables):
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        assert rows[0] == table.columns
        assert len(rows) == 1 + len(table.rows)
    assert safe_filename('a/b:c*d?"e" f') == "a_b_c_d_e_f"
    assert safe_filename("...") == "table"


def test_export_tables_csv_empty_report_creates_folder(tmp_path):
    empty = Report("t", datetime(2025, 1, 1), None, [], [], [], [], [])
    assert export_tables_csv(empty, tmp_path / "x") == []
    assert (tmp_path / "x").is_dir()


# ---------------------------------------------------------------------- 엑셀
def test_export_excel_structure(tmp_path, report, txs):
    openpyxl = pytest.importorskip("openpyxl")
    path = export_excel(report, tmp_path / "out" / "리포트.xlsx", transactions=txs)
    assert path.exists()
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["요약"] + [t.name for t in report.tables] + ["거래내역", "차트"]

    summary = wb["요약"]
    assert summary["A1"].value == "강남구 실거래 정리" and summary["A1"].font.bold
    texts = [c.value for row in summary.iter_rows() for c in row if c.value is not None]
    assert "생성일시" in texts and "매매 건수" in texts and "서울 강남구" in texts
    assert report.kpi("매매 중위가") in texts
    assert any(str(t).startswith("· 자료: 국토교통부") for t in texts)
    links = {c.value: c.hyperlink.location for row in summary.iter_rows() for c in row if c.hyperlink}
    assert links["월별 추이"] == "'월별 추이'!A1"

    ws = wb["월별 추이"]
    header = [c.value for c in ws[1]]
    assert header == report.table("월별 추이").columns
    assert all(c.font.bold for c in ws[1])
    assert ws["A1"].fill.fgColor.rgb.endswith("E6EDF7")
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == f"A1:{ws.cell(1, len(header)).column_letter}{ws.max_row}"
    count_col = header.index("거래 건수") + 1
    median_col = header.index("중위가(만원)") + 1
    assert isinstance(ws.cell(2, count_col).value, int)
    assert isinstance(ws.cell(2, median_col).value, int)
    assert ws.cell(2, median_col).number_format == "#,##0"

    deals = wb["거래내역"]
    head = [c.value for c in deals[1]]
    assert head == list(TRANSACTION_COLUMNS)
    date_cell = deals.cell(2, head.index("계약일") + 1)
    assert isinstance(date_cell.value, datetime) and date_cell.is_date
    assert date_cell.number_format == "yyyy-mm-dd"
    price_cell = deals.cell(2, head.index("거래금액(만원)") + 1)
    assert isinstance(price_cell.value, int) and price_cell.number_format == "#,##0"
    area_cell = deals.cell(2, head.index("전용면적(㎡)") + 1)
    assert isinstance(area_cell.value, float) and area_cell.number_format == "0.00"
    names = [deals.cell(r, head.index("단지명") + 1).value for r in range(2, deals.max_row + 1)]
    assert XSS in names

    jeonse = wb["전세가율"]
    jhead = [c.value for c in jeonse[1]]
    assert jeonse.cell(2, jhead.index("전세가율(%)") + 1).number_format == '0.0"%"'

    chart_sheet = wb["차트"]
    assert len(chart_sheet._charts) == 3  # 매매: 중위가 선 + 거래량 막대, 전월세: 막대
    assert chart_sheet["A1"].value == report.charts[0].title

    widths = {k: v.width for k, v in ws.column_dimensions.items()}
    assert widths["A"] >= text_width("아파트 전월세")


def test_export_excel_without_transactions_and_charts(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    report = build_report([], today=TODAY)
    path = export_excel(report, tmp_path / "empty.xlsx")
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["요약", "월별 추이"]
    assert [c.value for c in wb["월별 추이"][1]][:2] == ["유형", "계약월"]


def test_export_excel_sanitizes_values_and_sheet_names(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    tables = [
        Table("a/b[c]:d*e?f\\g", ["값", "시각"], [["=1+1", datetime(2025, 1, 2, 3, 4, tzinfo=KST)], ["bad\x01text", None]],
              formats={"시각": "date"}),
        Table("요약", ["x"], [[1]]),  # 요약 시트와 이름이 겹침
        Table("가" * 40, ["x"], [[float("nan")]]),
    ]
    report = Report("t", datetime(2025, 1, 1, tzinfo=KST), None, [], [], tables, [], [])
    wb = openpyxl.load_workbook(export_excel(report, tmp_path / "s.xlsx"))
    assert wb.sheetnames == ["요약", "abcdefg", "요약 (2)", "가" * 31]
    ws = wb["abcdefg"]
    assert ws["A2"].value == "=1+1" and ws["A2"].data_type == "s"  # 수식으로 실행되지 않음
    assert ws["B2"].value == datetime(2025, 1, 2, 3, 4)  # 시간대 제거(한국 시간)
    assert ws["A3"].value == "badtext"
    assert wb["가" * 31]["A2"].value is None


def test_export_excel_requires_openpyxl(tmp_path, report, monkeypatch):
    monkeypatch.setitem(sys.modules, "openpyxl", None)  # import openpyxl → ImportError
    target = tmp_path / "x.xlsx"
    with pytest.raises(ExportError) as info:
        export_excel(report, target)
    assert str(info.value) == MISSING_OPENPYXL
    assert "pip install openpyxl" in str(info.value)
    assert not target.exists()


def test_excel_helpers():
    assert text_width("abc") == 3 and text_width("강남구") == 6 and text_width("ＡＢ") == 4
    used: set[str] = set()
    assert sheet_title("차트", used) == "차트"
    assert sheet_title("차트", used) == "차트 (2)"
    assert sheet_title("'인용'", used) == "인용"
    assert sheet_title("[]:*?/\\", used) == "Sheet"
    assert len(sheet_title("x" * 50, used)) == 31
    widths = column_widths(Table("t", ["짧음", "n"], [["아주 긴 단지 이름입니다", 1234567]], formats={"n": "int"}))
    assert widths[0] == text_width("아주 긴 단지 이름입니다") + 2
    assert widths[1] == 11  # "1,234,567" + 2


# ---------------------------------------------------------------------- HTML
def test_export_html_single_file(tmp_path, report):
    path = export_html(report, tmp_path / "sub" / "report.html")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("<!DOCTYPE html>")
    assert "<title>강남구 실거래 정리</title>" in text
    assert '<meta name="viewport"' in text and 'lang="ko"' in text
    for label, value in report.kpis:
        assert value in text, label
    assert text.count("<svg") == len(report.charts) == 2
    assert "prefers-color-scheme:dark" in text and "@media print" in text
    assert "Pretendard" in text and "Malgun Gothic" in text
    for table in report.tables:
        assert f"<h2>{table.name}</h2>" in text
    assert "자료: 국토교통부 실거래가 공개시스템" in text


def test_export_html_escapes_and_has_no_external_resources(report):
    text = render_html(report)
    assert XSS not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<script" not in text.lower()
    assert not re.search(r"""(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""", text, flags=re.I)
    assert "<link" not in text.lower() and "@import" not in text and "url(" not in text


def test_export_html_caps_rows():
    rows = [[i, f"단지{i}"] for i in range(600)]
    table = Table("단지별 요약", ["건수", "단지명"], rows, formats={"건수": "int"})
    report = Report("큰 표", datetime(2025, 1, 1, tzinfo=KST), ("202501", "202501"), ["강남구"], [], [table], [], [])
    text = render_html(report)
    assert CAP_NOTE == "상위 500행만 표시 — 전체는 엑셀/CSV 참고"
    assert CAP_NOTE in text and "전체 600행" in text
    tbody = text.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert tbody.count("<tr>") == 500
    assert "단지499" in text and "단지500" not in text


def test_html_table_formatting(report):
    text = render_html(report)
    assert '<td class="num" title="28억 5,000만원">285,000</td>' in text
    assert "<td>2025-01-03</td>" in text
    assert report.table("신고가").rows and "해당 자료가 없습니다." not in text  # 빈 표가 없는 리포트
    assert '<th scope="col" class="num">거래금액(만원)</th>' in text


def test_html_empty_report():
    text = render_html(build_report([], today=TODAY))
    assert "해당 자료가 없습니다." in text and "<svg" not in text


# ---------------------------------------------------------------------- SVG
def test_svg_helpers():
    assert nice_ticks(0, 18, 3, integer=True) == [0, 10, 20]
    assert nice_ticks(0, 3, 4, integer=True) == [0, 1, 2, 3]
    assert nice_ticks(78000, 125000, 4) == [60000, 80000, 100000, 120000, 140000]
    assert nice_ticks(5, 5) == [4.5, 4.75, 5.0, 5.25, 5.5]  # 값이 하나면 ±10% 여유
    assert nice_ticks(82500, 82500)[0] < 82500 < nice_ticks(82500, 82500)[-1]
    assert manwon_tick(0) == "0" and manwon_tick(5000) == "5,000만"
    assert manwon_tick(80000) == "8억" and manwon_tick(125000) == "12.5억"
    assert short_label("2025.01") == "25.01" and short_label("전체") == "전체"


def test_svg_bar_line_chart():
    chart = Chart(
        title="아파트 <매매>",
        kind="bar+line",
        labels=["2025.01", "2025.02", "2025.03", "2025.04"],
        series=[("거래량", [3, 0, 2, 1]), ("중위가", [82500, None, 90000, 95000])],
        unit="만원",
    )
    svg = render_chart_svg(chart)
    assert svg.startswith('<svg class="chart-svg" viewBox="0 0 ') and svg.endswith("</svg>")
    assert 'aria-label="아파트 &lt;매매&gt;"' in svg
    assert "중위가(만원)" in svg and "거래량(건)" in svg
    assert svg.count('class="hit"') == 4
    assert "<title>2025.01\n거래량 3건\n중위가 8억 2,500만원</title>" in svg
    assert "중위가 거래 없음" in svg
    assert svg.count('class="bar s1"') == 3  # 0건 달은 막대 없음
    assert svg.count('class="ln s2"') == 1  # 2025.03~04 한 조각 (2025.01 은 점)
    assert svg.count('class="dot s2"') == 2  # 외딴 점 + 끝점
    assert "9.5억" in svg  # 끝 값 표시
    assert ">8억<" in svg or ">10억<" in svg  # 억 눈금


def test_svg_grouped_bars_and_empty():
    chart = Chart("전월세", "bar", ["2025.01", "2025.02"], [("전세", [2, 1]), ("월세", [1, 0])], unit="건")
    svg = render_chart_svg(chart)
    assert svg.count('class="bar s1"') == 2 and svg.count('class="bar s2"') == 1
    assert "단위: 건" in svg and "전세 2건" in svg
    assert "자료 없음" in render_chart_svg(Chart("빈 차트", "bar", [], []))
