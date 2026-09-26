"""콘솔 출력 도우미 — 한글 폭(2칸)을 고려한 표, 수집 결과·거래 한 줄 표시.

터미널에서 한글·한자(동아시아 전각 문자)는 2칸을 차지하므로 ``len()`` 대신
``display_width()`` 로 폭을 계산해 열을 맞춘다.
"""

from __future__ import annotations

import re
import shutil
import sys
import unicodedata
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Iterable, Optional, Sequence, TextIO

from .models import Transaction
from .utils import KST, ym_label

if TYPE_CHECKING:
    from .pipeline import CollectResult, CollectSummary

ELLIPSIS = "…"
_NUMERIC_RE = re.compile(r"^[-+]?[\d,]+(\.\d+)?%?$")


# --------------------------------------------------------------------------- 폭 계산
def char_width(ch: str) -> int:
    """글자 하나의 표시 폭 (전각 2, 결합 문자 0, 그 밖 1)."""
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    if unicodedata.category(ch) in ("Cc", "Cf"):
        return 0
    return 1


def display_width(text: str) -> int:
    """문자열의 터미널 표시 폭."""
    return sum(char_width(ch) for ch in text)


def truncate(text: str, width: int) -> str:
    """표시 폭이 ``width`` 를 넘으면 잘라서 끝에 ``…`` 를 붙인다."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    limit = width - 1  # 말줄임표 1칸
    for ch in text:
        w = char_width(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out) + ELLIPSIS


def pad(text: str, width: int, align: str = "l") -> str:
    """표시 폭 기준으로 공백을 채운다. ``align``: ``l`` / ``r`` / ``c``."""
    gap = max(0, width - display_width(text))
    if align == "r":
        return " " * gap + text
    if align == "c":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


# --------------------------------------------------------------------------- 표
def format_cell(value: Any) -> str:
    """표 칸에 넣을 문자열 (정수는 천 단위 쉼표, 날짜는 ISO)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}"
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(KST)
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).replace("\n", " ")


def _is_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and bool(_NUMERIC_RE.match(value.strip()))


def format_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    align: Optional[str | Sequence[str]] = None,
    max_width: Optional[int | Sequence[Optional[int]]] = 30,
    max_rows: Optional[int] = None,
    indent: str = "",
    fit: Optional[int] = None,
    shrink: Sequence[int] = (),
) -> str:
    """표를 문자열로 만든다.

    * ``align`` — 열마다 ``l``/``r``/``c`` (예: ``"llr"``). 생략하면 숫자 열만 오른쪽 정렬.
    * ``max_width`` — 칸 최대 폭(전체 공통 또는 열마다). 넘치면 ``…`` 로 자른다.
    * ``max_rows`` — 이보다 많으면 앞부분만 보이고 ``… 외 N행`` 을 붙인다.
    * ``fit`` — 표 전체 폭이 이보다 넓으면 ``shrink`` 열(번호)을 넓은 것부터 줄인다.
    """
    row_list = [list(row) for row in rows]
    hidden = 0
    if max_rows is not None and len(row_list) > max_rows:
        hidden = len(row_list) - max_rows
        row_list = row_list[:max_rows]
    ncols = len(headers)
    for row in row_list:
        if len(row) < ncols:
            row.extend([None] * (ncols - len(row)))

    if align is None:
        aligns = []
        for col in range(ncols):
            values = [row[col] for row in row_list if row[col] not in (None, "")]
            aligns.append("r" if values and all(_is_numeric(v) for v in values) else "l")
    else:
        aligns = [(align[i] if i < len(align) else "l") for i in range(ncols)]

    if max_width is None or isinstance(max_width, int):
        limits: list[Optional[int]] = [max_width] * ncols
    else:
        limits = [(max_width[i] if i < len(max_width) else None) for i in range(ncols)]

    cells = []
    for row in row_list:
        line = []
        for col in range(ncols):
            text = format_cell(row[col])
            if limits[col]:
                text = truncate(text, limits[col])
            line.append(text)
        cells.append(line)

    head = [truncate(str(h), limits[i]) if limits[i] else str(h) for i, h in enumerate(headers)]
    widths = [display_width(h) for h in head]
    for line in cells:
        for col, text in enumerate(line):
            widths[col] = max(widths[col], display_width(text))

    if fit is not None and shrink:
        total = sum(widths) + 2 * (ncols - 1) + display_width(indent)
        floors = {c: min(8, max(4, display_width(str(headers[c])))) for c in shrink if 0 <= c < ncols}
        while total > fit:
            candidates = [c for c in floors if widths[c] > floors[c]]
            if not candidates:
                break
            widest = max(candidates, key=lambda c: widths[c])
            widths[widest] -= 1
            total -= 1
        for c in floors:
            head[c] = truncate(head[c], widths[c])
            for line in cells:
                line[c] = truncate(line[c], widths[c])

    def render(values: Sequence[str]) -> str:
        return "  ".join(pad(v, widths[i], aligns[i]) for i, v in enumerate(values)).rstrip()

    lines = [render(head), "  ".join("-" * w for w in widths)]
    lines.extend(render(line) for line in cells)
    if hidden:
        lines.append(f"{ELLIPSIS} 외 {hidden:,}행")
    return "\n".join(indent + line for line in lines)


def print_table(headers: Sequence[str], rows: Iterable[Sequence[Any]], *, file: Optional[TextIO] = None, **kwargs: Any) -> None:
    """``format_table`` 결과를 출력한다."""
    print(format_table(headers, rows, **kwargs), file=file or sys.stdout)


# --------------------------------------------------------------------------- 거래
TRANSACTION_HEADERS = ["계약일", "유형", "지역", "단지·건물", "전용㎡", "층", "가격", "비고"]
TRANSACTION_ALIGN = "llllrrrll"  # 마지막 l 은 '수집 시각' 열
TRANSACTION_WIDTHS = [10, 14, 22, 20, 7, 4, 26, 24]
TRANSACTION_SHRINK = (2, 3, 7, 1)  # 화면이 좁으면 줄일 열: 지역, 단지·건물, 비고, 유형


def terminal_width(default: int = 120) -> int:
    """터미널 폭 (알 수 없으면 ``default``)."""
    return shutil.get_terminal_size((default, 24)).columns


def area_text(area_m2: Optional[float]) -> str:
    """``84.97`` → ``"84.97"``, ``114.8`` → ``"114.8"`` (끝의 0 제거)."""
    if area_m2 is None:
        return ""
    return f"{area_m2:.2f}".rstrip("0").rstrip(".")


def floor_text(floor: Optional[int]) -> str:
    """``12`` → ``"12층"``, ``-1`` → ``"지하1층"``."""
    if floor is None:
        return ""
    return f"지하{-floor}층" if floor < 0 else f"{floor}층"


def transaction_note(tx: Transaction) -> str:
    """비고 칸: 해제·갱신계약·직거래 등."""
    parts = []
    if tx.is_cancelled:
        parts.append(f"해제 {tx.cancel_date.isoformat()}" if tx.cancel_date else "해제")
    if tx.is_rent:
        if tx.contract_type:
            parts.append(f"{tx.contract_type}(요구권)" if tx.renewal_right_used else tx.contract_type)
        elif tx.renewal_right_used:
            parts.append("갱신요구권")
    elif tx.deal_method == "직거래":
        parts.append("직거래")
    return ", ".join(parts)


def transaction_headers(*, with_first_seen: bool = False) -> list[str]:
    return TRANSACTION_HEADERS + (["수집 시각"] if with_first_seen else [])


def transaction_row(tx: Transaction, *, with_first_seen: bool = False) -> list[Any]:
    """거래 1건을 표 한 줄로 (``TRANSACTION_HEADERS`` 순서)."""
    region = " ".join(p for p in (tx.sigungu, tx.dong) if p) or tx.lawd_cd
    row: list[Any] = [
        tx.deal_date.isoformat(),
        tx.deal_type.label,
        region,
        tx.name or "-",
        area_text(tx.area_m2),
        "" if tx.floor is None else str(tx.floor),
        tx.price_text,
        transaction_note(tx),
    ]
    if with_first_seen:
        seen = tx.first_seen_at
        if seen is not None and seen.tzinfo is not None:
            seen = seen.astimezone(KST)
        row.append(seen.strftime("%m-%d %H:%M") if seen is not None else "")
    return row


def format_transactions(
    transactions: Sequence[Transaction],
    *,
    max_rows: Optional[int] = 50,
    with_first_seen: bool = False,
    width: Optional[int] = None,
) -> str:
    """거래 목록 표 (터미널 폭에 맞춰 지역·단지명·비고 열을 줄인다)."""
    widths: list[Optional[int]] = list(TRANSACTION_WIDTHS) + ([11] if with_first_seen else [])
    return format_table(
        transaction_headers(with_first_seen=with_first_seen),
        [transaction_row(tx, with_first_seen=with_first_seen) for tx in transactions],
        align=TRANSACTION_ALIGN[: len(widths)],
        max_width=widths,
        max_rows=max_rows,
        fit=width if width is not None else terminal_width(),
        shrink=TRANSACTION_SHRINK,
    )


# --------------------------------------------------------------------------- 수집 결과
def format_task_line(index: int, total: int, result: "CollectResult", region_name: str = "") -> str:
    """``[3/24] 아파트 매매 · 서울특별시 강남구 · 2025.01 → 312건 (신규 12, 변경 1, 삭제 0, 해제 1)``"""
    task = result.task
    head = f"[{index}/{total}] {task.deal_type.label} · {region_name or task.lawd_cd} · {ym_label(task.deal_ym)}"
    if result.status == "ok":
        tail = f"{result.item_count:,}건"
        part = result.partition
        if part is not None:
            tail += (
                f" (신규 {part.inserted:,}, 변경 {part.updated:,}, "
                f"삭제 {part.removed:,}, 해제 {part.newly_cancelled:,})"
            )
        if result.invalid:
            tail += f" · 해석 실패 {result.invalid:,}건 건너뜀"
        if result.note:
            tail += f" · {result.note}"
    elif result.status == "skipped":
        tail = f"건너뜀 ({result.note})" if result.note else "건너뜀"
    else:
        tail = f"오류: {result.error}"
    return f"{head} → {tail}"


def format_summary(summary: "CollectSummary") -> str:
    """수집 실행 요약 (여러 줄)."""
    done = len(summary.results)
    lines = [
        f"작업 {done:,}/{summary.planned:,}건: 수집 {summary.ok:,} · 건너뜀 {summary.skipped:,} · "
        f"오류 {summary.errors:,} (API 호출 {summary.api_calls:,}회)",
        f"받은 거래 {summary.item_count:,}건 → 신규 {summary.inserted:,} · 변경 {summary.updated:,} · "
        f"삭제 {summary.removed:,} · 새로 해제 {summary.newly_cancelled:,}",
    ]
    if summary.invalid:
        lines.append(f"해석하지 못해 건너뛴 레코드 {summary.invalid:,}건 (-v 로 자세히)")
    if summary.aborted:
        lines.append(f"수집이 중단되었습니다: {summary.abort_reason} (남은 작업 {summary.remaining:,}건)")
    return "\n".join(lines)
