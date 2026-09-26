"""단일 파일 HTML 리포트 — 외부 CSS·JS·글꼴·이미지 없이 파일 하나로 열린다.

* 차트는 파이썬에서 그린 인라인 SVG (``svg`` 모듈), 스타일은 인라인 CSS.
* 밝은/어두운 테마(``prefers-color-scheme``, ``data-theme`` 로 강제 가능), 휴대폰 화면, 인쇄를 지원한다.
* 모든 글자는 ``html.escape`` 로 이스케이프한다. 표는 최대 ``MAX_ROWS`` 행만 보여 준다.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any, Union

from ..errors import ExportError
from ..utils import KST, format_manwon
from .report import NUMERIC_FORMATS, Chart, Report, Table, format_cell
from .svg import chart_rows, render_chart_svg, series_marks

MAX_ROWS = 500
CAP_NOTE = "상위 500행만 표시 — 전체는 엑셀/CSV 참고"

FONT_STACK = 'Pretendard, "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif'

# 색 토큰 — 밝은 테마 / 어두운 테마 (차트 계열 색은 같은 색상의 테마별 단계)
LIGHT_TOKENS: dict[str, str] = {
    "bg": "#f6f6f3",
    "surface": "#fcfcfb",
    "ink": "#0b0b0b",
    "ink-2": "#52514e",
    "muted": "#6f6d68",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "border": "rgba(11, 11, 11, 0.10)",
    "head": "#efeee9",
    "zebra": "rgba(11, 11, 11, 0.025)",
    "hover": "rgba(42, 120, 214, 0.09)",
    "link": "#1c5cab",
    "s1": "#2a78d6",
    "s2": "#eb6834",
    "s3": "#1baf7a",
    "s4": "#eda100",
    "s5": "#e87ba4",
    "s6": "#008300",
    "s7": "#4a3aa7",
    "s8": "#e34948",
}
DARK_TOKENS: dict[str, str] = {
    "bg": "#0d0d0d",
    "surface": "#1a1a19",
    "ink": "#ffffff",
    "ink-2": "#c3c2b7",
    "muted": "#9d9b94",
    "grid": "#2c2c2a",
    "axis": "#4a4a46",
    "border": "rgba(255, 255, 255, 0.10)",
    "head": "#242423",
    "zebra": "rgba(255, 255, 255, 0.03)",
    "hover": "rgba(57, 135, 229, 0.16)",
    "link": "#86b6ef",
    "s1": "#3987e5",
    "s2": "#d95926",
    "s3": "#199e70",
    "s4": "#c98500",
    "s5": "#d55181",
    "s6": "#008300",
    "s7": "#9085e9",
    "s8": "#e66767",
}

PathLike = Union[str, Path]

_BASE_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:__FONT__;font-size:15px;line-height:1.55;word-break:keep-all;overflow-wrap:break-word}
.page{max-width:1200px;margin:0 auto;padding:28px 16px 56px}
.head h1{font-size:1.6rem;line-height:1.3;margin:0 0 8px;letter-spacing:-0.01em}
.meta{margin:0;display:flex;flex-wrap:wrap;gap:4px 16px;color:var(--ink-2);font-size:.9rem}
.meta b{color:var(--ink);font-weight:600}
.toc{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0 24px;padding:0;list-style:none}
.toc a{display:inline-block;padding:3px 11px;border:1px solid var(--border);border-radius:999px;background:var(--surface);color:var(--ink-2);text-decoration:none;font-size:.82rem}
.toc a:hover,.toc a:focus-visible{color:var(--ink);border-color:var(--axis)}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin:0 0 30px}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 14px;min-width:0}
.kpi-label{font-size:.8rem;color:var(--ink-2)}
.kpi-value{margin-top:4px;font-size:1.3rem;font-weight:650;line-height:1.3}
.kpi-value.long{font-size:1rem;font-weight:600}
.section{margin:0 0 34px}
.section-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px;margin:0 0 4px}
h2{font-size:1.15rem;line-height:1.35;margin:0}
.count{font-size:.8rem;color:var(--muted);font-variant-numeric:tabular-nums}
.desc{margin:0 0 10px;color:var(--ink-2);font-size:.88rem}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:14px;margin-top:10px}
.chart{margin:0;min-width:0;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px 10px}
.chart h3{margin:0;font-size:.98rem;line-height:1.4}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin:6px 0 2px;padding:0;list-style:none;font-size:.8rem;color:var(--ink-2)}
.legend li{display:inline-flex;align-items:center;gap:6px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px}
.sw.ln{width:16px;height:2px;border-radius:1px}
.chart-svg{display:block;width:100%;height:auto;overflow:visible;margin-top:6px}
.chart-svg text{font-size:11px;fill:var(--muted);font-family:inherit}
.chart-svg .cap{fill:var(--ink-2)}
.chart-svg .end{fill:var(--ink-2);font-weight:600}
.chart-svg .empty{font-size:13px}
.chart-svg .grid,.chart-svg .base{stroke-width:1;vector-effect:non-scaling-stroke;shape-rendering:crispEdges}
.chart-svg .grid{stroke:var(--grid)}
.chart-svg .base{stroke:var(--axis)}
.chart-svg .hit{fill:transparent}
.chart-svg .hit:hover{fill:var(--hover)}
.chart-svg .marks,.chart-svg text{pointer-events:none}
.chart-svg .ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round;vector-effect:non-scaling-stroke}
.chart-svg .dot{stroke:var(--surface);stroke-width:2;vector-effect:non-scaling-stroke}
details.data{margin-top:6px;font-size:.82rem}
details.data summary{cursor:pointer;color:var(--ink-2);padding:2px 0}
details.data .table-wrap{margin-top:6px;max-height:320px}
.table-wrap{overflow:auto;max-height:min(72vh,680px);border:1px solid var(--border);border-radius:10px;background:var(--surface);-webkit-overflow-scrolling:touch}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:.84rem}
th,td{padding:7px 10px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--border)}
thead th{position:sticky;top:0;z-index:1;background:var(--head);color:var(--ink);font-weight:600}
tbody tr:nth-child(even) td{background:var(--zebra)}
tbody tr:hover td{background:var(--hover)}
tbody tr:last-child td{border-bottom:0}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums}
td.empty{padding:18px;text-align:center;color:var(--muted)}
.cap-note{margin:8px 0 0;font-size:.8rem;color:var(--ink-2)}
.notes{margin-top:40px;padding-top:16px;border-top:1px solid var(--border);color:var(--ink-2);font-size:.86rem}
.notes h2{font-size:1rem;color:var(--ink)}
.notes ul{margin:8px 0 0;padding-left:1.2em}
.notes li{margin:3px 0}
.gen{margin:14px 0 0;color:var(--muted);font-size:.78rem}
a{color:var(--link)}
@media (max-width:560px){
.page{padding:20px 16px 44px}
.head h1{font-size:1.3rem}
.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
.kpi-value{font-size:1.12rem}
.chart{padding:12px 12px 8px}
.chart-svg text{font-size:17px}
.chart-svg .x-alt{display:none}
th,td{padding:6px 8px}
}
@media print{
body{background:#fff;font-size:10.5pt}
.page{max-width:none;padding:0}
.toc,details.data{display:none}
.table-wrap{max-height:none;overflow:visible;border:0;border-radius:0}
thead th{position:static}
thead{display:table-header-group}
tr,.kpi,.chart{break-inside:avoid}
h2,.section-head{break-after:avoid}
a{color:inherit;text-decoration:none}
.chart-svg .hit:hover{fill:transparent}
}
@page{margin:12mm}
"""


def _tokens(tokens: dict[str, str], scheme: str) -> str:
    body = ";".join(f"--{name}:{value}" for name, value in tokens.items())
    return f"color-scheme:{scheme};{body}"


def page_css() -> str:
    """리포트 CSS (색 토큰 + 레이아웃)."""
    light, dark = _tokens(LIGHT_TOKENS, "light"), _tokens(DARK_TOKENS, "dark")
    series = "".join(
        f".sw.s{i},.chart-svg .bar.s{i},.chart-svg .dot.s{i}{{background:var(--s{i});fill:var(--s{i})}}"
        f".chart-svg .ln.s{i}{{stroke:var(--s{i})}}"
        for i in range(1, 9)
    )
    return (
        f":root{{{light}}}"
        f'@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{dark}}}}}'
        f':root[data-theme="dark"]{{{dark}}}'
        + _BASE_CSS.replace("__FONT__", FONT_STACK)
        + series
        # 인쇄는 항상 밝은 색 (마지막에 두어 어두운 테마보다 우선)
        + f'@media print{{:root,:root:not([data-theme="light"]),:root[data-theme="dark"]{{{light}}}}}'
    )


# ---------------------------------------------------------------------- 조각
def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _generated_text(value: datetime) -> str:
    if value.tzinfo is not None:
        return value.astimezone(KST).strftime("%Y-%m-%d %H:%M") + " KST"
    return value.strftime("%Y-%m-%d %H:%M")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def table_html(table: Table, *, max_rows: int = MAX_ROWS, css_class: str = "") -> str:
    """``Table`` → ``<table>`` (스크롤 감싸개 포함). 숫자는 오른쪽 정렬, 만원 값은 억 표기 툴팁."""
    numeric = [table.formats.get(col, "") in NUMERIC_FORMATS for col in table.columns]
    num_attr = ' class="num"'
    head = "".join(
        f'<th scope="col"{num_attr if numeric[i] else ""}>{_e(col)}</th>' for i, col in enumerate(table.columns)
    )
    body: list[str] = []
    for row in table.rows[:max_rows]:
        cells = []
        for i, col in enumerate(table.columns):
            value = row[i] if i < len(row) else None
            fmt = table.formats.get(col, "")
            attrs = ""
            if numeric[i] or _is_number(value):
                attrs = ' class="num"'
            if fmt == "manwon" and _is_number(value):
                attrs += f' title="{_e(format_manwon(value))}"'
            cells.append(f"<td{attrs}>{_e(format_cell(value, fmt))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body.append(f'<tr><td class="empty" colspan="{max(1, len(table.columns))}">해당 자료가 없습니다.</td></tr>')
    cls = f"table-wrap {css_class}".strip()
    return f'<div class="{cls}" tabindex="0"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _chart_html(chart: Chart) -> str:
    marks = series_marks(chart)
    legend = ""
    if len(marks) > 1:
        items = "".join(
            f'<li><span class="sw s{slot}{" ln" if mark == "line" else ""}" aria-hidden="true"></span>{_e(name)}</li>'
            for name, mark, slot in marks
        )
        legend = f'<ul class="legend">{items}</ul>'
    header, rows = chart_rows(chart)
    data_table = Table(name=chart.title, columns=header, rows=[list(r) for r in rows])
    return (
        '<figure class="chart">'
        f"<figcaption><h3>{_e(chart.title)}</h3>{legend}</figcaption>"
        f"{render_chart_svg(chart)}"
        f'<details class="data"><summary>표로 보기</summary>{table_html(data_table)}</details>'
        "</figure>"
    )


def _kpis_html(report: Report) -> str:
    cards = []
    for label, value in report.kpis:
        if label in ("기간", "지역"):  # 머리말에 이미 표시
            continue
        long_cls = " long" if len(value) > 14 else ""
        cards.append(
            f'<div class="kpi"><div class="kpi-label">{_e(label)}</div>'
            f'<div class="kpi-value{long_cls}">{_e(value)}</div></div>'
        )
    return f'<section class="kpis" aria-label="주요 지표">{"".join(cards)}</section>' if cards else ""


def render_html(report: Report, *, max_rows: int = MAX_ROWS) -> str:
    """``Report`` → HTML 문서 문자열."""
    region = report.kpi("지역") or (", ".join(report.regions) if report.regions else "-")
    toc: list[str] = []
    sections: list[str] = []

    if report.charts:
        toc.append('<li><a href="#sec-charts">차트</a></li>')
        figures = "".join(_chart_html(c) for c in report.charts)
        sections.append(
            '<section class="section" id="sec-charts"><div class="section-head"><h2>차트</h2></div>'
            '<p class="desc">차트에 마우스를 올리면 그 달의 값이 보입니다. 정확한 값은 각 차트의 ‘표로 보기’에 있습니다.</p>'
            f'<div class="charts">{figures}</div></section>'
        )

    for index, table in enumerate(report.tables, 1):
        anchor = f"sec-{index}"
        toc.append(f'<li><a href="#{anchor}">{_e(table.name)}</a></li>')
        total = len(table.rows)
        note = ""
        if total > max_rows:
            cap = CAP_NOTE if max_rows == MAX_ROWS else f"상위 {max_rows:,}행만 표시 — 전체는 엑셀/CSV 참고"
            note = f'<p class="cap-note">{_e(cap)} (전체 {total:,}행)</p>'
        desc = f'<p class="desc">{_e(table.description)}</p>' if table.description else ""
        sections.append(
            f'<section class="section" id="{anchor}">'
            f'<div class="section-head"><h2>{_e(table.name)}</h2><span class="count">{total:,}행</span></div>'
            f"{desc}{table_html(table, max_rows=max_rows)}{note}</section>"
        )

    toc.append('<li><a href="#sec-notes">참고</a></li>')
    notes = "".join(f"<li>{_e(n)}</li>" for n in report.notes)
    generated = _generated_text(report.generated_at)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        '<meta name="generator" content="silgeorae">\n'
        f"<title>{_e(report.title)}</title>\n"
        f"<style>{page_css()}</style>\n</head>\n<body>\n"
        '<div class="page">\n'
        '<header class="head">'
        f"<h1>{_e(report.title)}</h1>"
        '<p class="meta">'
        f"<span>기간 <b>{_e(report.period_text)}</b></span>"
        f"<span>지역 <b>{_e(region)}</b></span>"
        f"<span>생성 {_e(generated)}</span>"
        "</p></header>\n"
        f'<nav aria-label="목차"><ul class="toc">{"".join(toc)}</ul></nav>\n'
        f"<main>{_kpis_html(report)}{''.join(sections)}</main>\n"
        '<footer class="notes" id="sec-notes"><h2>참고</h2>'
        f"<ul>{notes}</ul>"
        f'<p class="gen">silgeorae 로 {_e(generated)}에 만든 리포트입니다.</p></footer>\n'
        "</div>\n</body>\n</html>\n"
    )


def export_html(report: Report, path: PathLike) -> Path:
    """리포트를 외부 리소스 없는 단일 HTML 파일로 저장하고 경로를 돌려준다."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_html(report), encoding="utf-8")
    except OSError as exc:
        raise ExportError(f"HTML 파일을 저장하지 못했습니다: {target} ({exc})") from exc
    return target
