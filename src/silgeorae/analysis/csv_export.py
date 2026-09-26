"""CSV 내보내기 — 엑셀에서 한글이 깨지지 않도록 UTF-8 BOM(``utf-8-sig``)으로 쓴다.

* 숫자는 천 단위 쉼표 없이, 날짜는 ``YYYY-MM-DD``, 빈 값은 빈 칸.
* ``=``·``+``·``@`` 등으로 시작하는 글자는 엑셀이 수식으로 실행하지 않도록 앞에 ``'`` 를 붙인다.
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Union

from ..errors import ExportError
from ..models import Transaction
from .report import Report, Table, transactions_table

ENCODING = "utf-8-sig"

_FORMULA_START = ("=", "+", "@", "\t", "\r")
_NUMBER_TEXT = re.compile(r"^-?\d[\d,]*(\.\d+)?$")
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\s]+')

PathLike = Union[str, Path]


def csv_value(value: Any) -> str:
    """표 값 1개 → CSV 칸 문자열."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "O" if value else ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.10f}".rstrip("0").rstrip(".")
    text = str(value)
    if text.startswith(_FORMULA_START) or (text.startswith("-") and not _NUMBER_TEXT.match(text)):
        text = "'" + text  # 수식 주입 방지
    return text


def safe_filename(name: str, default: str = "table") -> str:
    """표 이름 → 파일 이름으로 쓸 수 있는 문자열 (공백·금지 문자는 ``_``)."""
    text = _UNSAFE_NAME.sub("_", name).strip("._")
    text = re.sub(r"_+", "_", text)
    return text[:80] or default


def write_table_csv(table: Table, path: PathLike) -> Path:
    """``Table`` 1개를 CSV 파일로 쓴다 (상위 폴더가 없으면 만든다)."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding=ENCODING, newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(table.columns)
            for row in table.rows:
                writer.writerow([csv_value(v) for v in row])
    except OSError as exc:
        raise ExportError(
            f"CSV 파일을 저장하지 못했습니다: {target} ({exc}). 엑셀 등에서 열려 있으면 닫고 다시 시도하세요."
        ) from exc
    return target


def export_csv(transactions: Iterable[Transaction], path: PathLike) -> Path:
    """거래 원자료(``transactions_table``)를 CSV 로 저장하고 경로를 돌려준다."""
    return write_table_csv(transactions_table(transactions), path)


def export_tables_csv(report: Report, directory: PathLike) -> list[Path]:
    """리포트의 표마다 CSV 1개씩 ``directory`` 에 저장한다 (``01_월별_추이.csv`` …)."""
    folder = Path(directory)
    width = max(2, len(str(len(report.tables))))
    paths = []
    for index, table in enumerate(report.tables, 1):
        name = f"{index:0{width}d}_{safe_filename(table.name, f'table{index}')}.csv"
        paths.append(write_table_csv(table, folder / name))
    if not paths:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ExportError(f"폴더를 만들지 못했습니다: {folder} ({exc})") from exc
    return paths
