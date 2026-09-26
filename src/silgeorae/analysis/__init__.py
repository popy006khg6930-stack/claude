"""분석·리포트 — 거래 목록을 통계표·차트로 정리하고 엑셀·CSV·HTML 로 내보낸다.

사용 예::

    report = build_report(store.query(start_ym="202401"), regions=["강남구"], period=("202401", "202512"))
    export_excel(report, "reports/강남구.xlsx", transactions=txs)   # openpyxl 필요
    export_html(report, "reports/강남구.html")                      # 파일 하나로 열리는 리포트
    export_tables_csv(report, "reports/csv")                        # 표마다 CSV 1개

순수 통계 함수는 ``silgeorae.analysis.stats`` 에 있다.
"""

from __future__ import annotations

from .csv_export import export_csv, export_tables_csv
from .excel import export_excel
from .html import export_html
from .report import Chart, Report, Table, build_report, format_cell, transactions_table

__all__ = [
    "Table",
    "Chart",
    "Report",
    "build_report",
    "transactions_table",
    "format_cell",
    "export_excel",
    "export_csv",
    "export_tables_csv",
    "export_html",
]
