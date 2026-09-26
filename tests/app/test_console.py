from __future__ import annotations

from datetime import date, datetime

from silgeorae.console import (
    area_text,
    display_width,
    floor_text,
    format_summary,
    format_table,
    format_task_line,
    format_transactions,
    pad,
    transaction_note,
    transaction_row,
    truncate,
)
from silgeorae.models import DealType, Transaction
from silgeorae.pipeline import CollectResult, CollectSummary, CollectTask
from silgeorae.processing import PartitionResult
from silgeorae.utils import KST


def make_tx(**kw):
    base = dict(
        deal_type=DealType.APT_SALE, lawd_cd="11680", deal_date=date(2025, 1, 3), sigungu="강남구",
        dong="대치동", jibun="316", name="한빛마을1단지", floor=12, area_m2=84.97, price=285000,
    )
    base.update(kw)
    return Transaction(**base)


def test_display_width_counts_korean_as_two_columns():
    assert display_width("abc") == 3
    assert display_width("한글") == 4
    assert display_width("84.97㎡") == 7
    assert display_width("강남구 대치동") == 13
    assert display_width("é") == 1  # 결합 문자는 0칸


def test_truncate_and_pad_use_display_width():
    assert truncate("abc", 5) == "abc"
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("한빛마을1단지", 7) == "한빛마…"
    assert display_width(truncate("한빛마을1단지", 6)) <= 6
    assert truncate("아무거나", 0) == ""
    assert pad("한글", 6) == "한글  "
    assert pad("한글", 6, "r") == "  한글"
    assert pad("한글", 6, "c") == " 한글 "


def test_format_table_aligns_korean_and_numbers():
    text = format_table(["이름", "건수", "메모"], [["한빛마을1단지", 1234, "신규"], ["푸른숲", 5, None]])
    lines = text.split("\n")
    assert len(lines) == 4
    assert len({display_width(line.rstrip()) for line in lines[:2]}) == 1  # 머리글과 구분선 폭이 같다
    assert "1,234" in lines[2]
    # 숫자 열은 오른쪽 정렬 → 두 행의 건수 끝 위치가 같다
    end = lambda line, token: display_width(line[: line.index(token) + len(token)])  # noqa: E731
    assert end(lines[2], "1,234") == end(lines[3], "5")
    # 한글 이름 뒤 열도 같은 위치에서 시작한다
    assert display_width(lines[2][: lines[2].index("1,234")]) + display_width("1,234") == end(lines[3], "5")


def test_format_table_max_rows_and_widths():
    rows = [[f"행{i}", i] for i in range(10)]
    text = format_table(["이름", "값"], rows, max_rows=3, max_width=[3, None])
    assert text.endswith("… 외 7행")
    assert text.count("\n") == 2 + 3
    long = format_table(["이름"], [["아주아주아주긴이름입니다"]], max_width=10)
    assert "…" in long and all(display_width(line) <= 10 for line in long.split("\n"))


def test_format_table_fits_terminal_width_by_shrinking_columns():
    rows = [["2025-01-03", "서울특별시 강남구 대치동 아주 긴 주소", "한빛마을1단지 아주 긴 이름", "28억"]]
    text = format_table(["날짜", "지역", "이름", "가격"], rows, max_width=None, fit=50, shrink=(1, 2))
    assert max(display_width(line) for line in text.split("\n")) <= 50
    assert "2025-01-03" in text and "28억" in text


def test_transaction_row_and_note():
    tx = make_tx()
    row = transaction_row(tx)
    assert row[:4] == ["2025-01-03", "아파트 매매", "강남구 대치동", "한빛마을1단지"]
    assert row[4] == "84.97" and row[5] == "12" and row[6] == "28억 5,000만원"
    assert transaction_note(make_tx(is_cancelled=True, cancel_date=date(2025, 1, 28))) == "해제 2025-01-28"
    assert transaction_note(make_tx(deal_method="직거래")) == "직거래"
    rent = make_tx(deal_type=DealType.APT_RENT, price=None, deposit=100000, monthly_rent=120,
                   contract_type="갱신", renewal_right_used=True)
    assert transaction_note(rent) == "갱신(요구권)"
    assert transaction_row(rent)[6] == "월세 10억원/120만원"
    seen = make_tx(first_seen_at=datetime(2025, 1, 5, 7, 30, tzinfo=KST))
    assert transaction_row(seen, with_first_seen=True)[-1] == "01-05 07:30"
    assert area_text(114.8) == "114.8" and area_text(None) == ""
    assert floor_text(-1) == "지하1층" and floor_text(3) == "3층"


def test_format_transactions_fits_width():
    txs = [make_tx(name="아주 긴 가상 단지 이름 한빛마을 제1단지", dong="대치동")] * 3
    text = format_transactions(txs, max_rows=2, width=90)
    lines = text.split("\n")
    assert lines[-1] == "… 외 1행"
    assert all(display_width(line) <= 90 for line in lines)
    assert "28억 5,000만원" in text


def _result(status, **kw):
    return CollectResult(CollectTask(DealType.APT_SALE, "11680", "202501"), status, **kw)


def test_format_task_line():
    part = PartitionResult(inserted=12, updated=1, removed=0, newly_cancelled=1)
    line = format_task_line(3, 24, _result("ok", item_count=312, partition=part), "서울특별시 강남구")
    assert line == "[3/24] 아파트 매매 · 서울특별시 강남구 · 2025.01 → 312건 (신규 12, 변경 1, 삭제 0, 해제 1)"
    assert format_task_line(1, 2, _result("skipped", note="이미 수집됨")).endswith("→ 건너뜀 (이미 수집됨)")
    assert "11680" in format_task_line(1, 2, _result("skipped"))
    assert format_task_line(2, 2, _result("error", error="서버 오류")).endswith("→ 오류: 서버 오류")
    merged = format_task_line(1, 1, _result("ok", item_count=10, partition=part, note="삭제 없이 병합했습니다"))
    assert merged.endswith("· 삭제 없이 병합했습니다")


def test_format_summary():
    part = PartitionResult(inserted=5, updated=2, removed=1, newly_cancelled=1)
    summary = CollectSummary(
        results=[_result("ok", item_count=7, partition=part), _result("error", error="x")],
        aborted=True, abort_reason="인증키 오류", api_calls=3, planned=5,
    )
    text = format_summary(summary)
    assert "작업 2/5건" in text and "수집 1" in text and "오류 1" in text and "API 호출 3회" in text
    assert "신규 5" in text and "새로 해제 1" in text
    assert "수집이 중단되었습니다: 인증키 오류 (남은 작업 3건)" in text
