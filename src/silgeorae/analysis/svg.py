"""차트를 인라인 SVG 문자열로 그린다 (외부 라이브러리·스크립트·이미지 없음).

* 색·글꼴은 CSS 클래스(``s1``~``s8``)와 변수로만 지정한다 → HTML 의 밝은/어두운 테마를 따른다.
* ``"bar+line"`` 은 두 값의 척도가 달라 한 축에 겹치지 않고, x축(월)을 공유하는 위아래 두 칸으로 그린다:
  위 칸 = 선(중위가, 만원/억 눈금), 아래 칸 = 막대(거래량, 건 눈금). 각 칸이 자기 y축을 갖는다.
* 달마다 투명한 세로 띠가 있어 마우스를 올리면 그 달의 모든 값이 ``<title>`` 툴팁으로 보인다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from html import escape
from typing import Optional

from ..utils import format_manwon
from .report import Chart

WIDTH = 560  # viewBox 폭 (화면 폭에 맞춰 늘고 준다)
LEFT = 64  # y축 눈금 글자 자리
RIGHT_PLAIN = 12
RIGHT_LABEL = 60  # 선 끝 값 표시 자리
TOP_PAD = 6
CAPTION_H = 18
PANE_GAP = 14
X_BAND = 28
MAX_SLOTS = 8

_YM = re.compile(r"^(\d{2})(\d{2})\.(\d{2})$")


@dataclass
class _Pane:
    kind: str  # "bar" | "line"
    series: list[tuple[int, str, list[Optional[float]]]]  # (색 번호, 이름, 값)
    unit: str
    height: float
    caption: str
    top: float = 0.0
    lo: float = 0.0
    hi: float = 1.0
    ticks: list[float] = field(default_factory=list)

    @property
    def bottom(self) -> float:
        return self.top + self.height

    def y(self, value: float) -> float:
        span = (self.hi - self.lo) or 1.0
        return self.bottom - (value - self.lo) / span * self.height


# ---------------------------------------------------------------------- 수치 표기
def nice_ticks(lo: float, hi: float, target: int = 4, *, integer: bool = False) -> list[float]:
    """``lo``~``hi`` 를 덮는 보기 좋은 눈금 (1·2·2.5·5 × 10ⁿ 간격)."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return [0.0, 1.0]
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        pad = abs(hi) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    step = 10 * mag
    for m in (1, 2, 5, 10) if integer else (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            step = m * mag
            break
    if integer:
        step = max(1.0, float(round(step)))
    start = math.floor(lo / step + 1e-9) * step
    stop = math.ceil(hi / step - 1e-9) * step
    count = max(1, int(round((stop - start) / step)))
    return [start + i * step for i in range(count + 1)]


def manwon_tick(value: float) -> str:
    """만원 눈금 표기: ``0`` / ``5,000만`` / ``8억`` / ``12.5억``."""
    amount = int(round(value))
    if amount == 0:
        return "0"
    if abs(amount) >= 10000:
        return f"{amount / 10000:.2f}".rstrip("0").rstrip(".") + "억"
    return f"{amount:,}만"


def end_label(value: float, unit: str) -> str:
    """선 끝 값 표기 — 유효숫자 3자리 안팎 (``29.5억``, ``8.25억``, ``9,500만``)."""
    if unit != "만원":
        return tick_label(value, unit)
    amount = int(round(value))
    if abs(amount) >= 100000:
        return f"{amount / 10000:.1f}".rstrip("0").rstrip(".") + "억"
    return format_manwon(amount, short=True)


def tick_label(value: float, unit: str) -> str:
    if unit == "만원":
        return manwon_tick(value)
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def value_text(value: Optional[float], unit: str) -> str:
    """툴팁용 값 표기 (``8억 2,500만원``, ``12건``)."""
    if value is None:
        return "거래 없음"
    if unit == "만원":
        return format_manwon(value)
    number = f"{int(round(value)):,}" if abs(value - round(value)) < 1e-9 else f"{value:,.1f}"
    return f"{number}{unit}"


def short_label(label: str) -> str:
    """x축 표기 — ``2025.01`` → ``25.01``."""
    m = _YM.match(label)
    return f"{m.group(2)}.{m.group(3)}" if m else label[:10]


def _num(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------- 구성
def series_marks(chart: Chart) -> list[tuple[str, str, int]]:
    """범례용 (이름, ``"bar"``/``"line"``, 색 번호 1~8) 목록."""
    items = []
    for i, (name, _) in enumerate(chart.series):
        if chart.kind == "line" or (chart.kind == "bar+line" and i > 0):
            mark = "line"
        else:
            mark = "bar"
        items.append((name, mark, i % MAX_SLOTS + 1))
    return items


def series_unit(chart: Chart, index: int) -> str:
    """계열 단위 — ``bar+line`` 의 첫 계열(거래량)은 ``건``."""
    if chart.kind == "bar+line" and index == 0:
        return "건"
    return chart.unit


def _panes(chart: Chart) -> list[_Pane]:
    indexed = [(i % MAX_SLOTS + 1, name, list(values)) for i, (name, values) in enumerate(chart.series)]
    if chart.kind == "bar+line" and len(indexed) > 1:
        lines = indexed[1:]
        names = ", ".join(name for _, name, _ in lines)
        return [
            _Pane("line", lines, chart.unit, 150, f"{names}({chart.unit})" if chart.unit else names),
            _Pane("bar", indexed[:1], "건", 84, f"{indexed[0][1]}(건)"),
        ]
    kind = "line" if chart.kind == "line" else "bar"
    return [_Pane(kind, indexed, chart.unit, 190, f"단위: {chart.unit}" if chart.unit else "")]


def _scale(pane: _Pane) -> None:
    values = [v for _, _, vals in pane.series for v in vals if v is not None and math.isfinite(v)]
    if pane.kind == "bar":
        hi = max([0.0] + values)
        integer = all(float(v).is_integer() for v in values)
        pane.ticks = nice_ticks(0.0, hi if hi > 0 else 1.0, 3 if pane.height < 120 else 4, integer=integer)
    else:
        if not values:
            pane.ticks = [0.0, 1.0]
        else:
            pane.ticks = nice_ticks(min(values), max(values), 3 if pane.height < 170 else 4)
    pane.lo, pane.hi = pane.ticks[0], pane.ticks[-1]


# ---------------------------------------------------------------------- 그리기
def _bar_path(x: float, y_top: float, width: float, y_base: float) -> str:
    """위쪽 모서리만 둥근(4px) 막대 경로 — 바닥은 직각."""
    height = y_base - y_top
    r = max(0.0, min(4.0, width / 2, height))
    return (
        f"M{_num(x)},{_num(y_base)}V{_num(y_top + r)}"
        f"Q{_num(x)},{_num(y_top)} {_num(x + r)},{_num(y_top)}"
        f"H{_num(x + width - r)}"
        f"Q{_num(x + width)},{_num(y_top)} {_num(x + width)},{_num(y_top + r)}"
        f"V{_num(y_base)}Z"
    )


def render_chart_svg(chart: Chart) -> str:
    """``Chart`` → ``<svg>`` 문자열 (폭은 CSS 로 100%, 높이는 비율 유지)."""
    n = len(chart.labels)
    title = escape(chart.title)
    if n == 0 or not chart.series:
        return (
            f'<svg class="chart-svg" viewBox="0 0 {WIDTH} 80" role="img" aria-label="{title}">'
            f'<text class="empty" x="{WIDTH / 2}" y="44" text-anchor="middle">자료 없음</text></svg>'
        )
    panes = _panes(chart)
    has_line = any(p.kind == "line" for p in panes)
    right = RIGHT_LABEL if has_line else RIGHT_PLAIN
    x0 = LEFT
    plot_w = WIDTH - LEFT - right
    slot = plot_w / n

    y = TOP_PAD
    for pane in panes:
        pane.top = y + CAPTION_H
        y = pane.bottom + PANE_GAP
        _scale(pane)
    height = panes[-1].bottom + X_BAND

    out: list[str] = [
        f'<svg class="chart-svg" viewBox="0 0 {WIDTH} {_num(height)}" role="img" aria-label="{title}">'
    ]

    # 1) 눈금선·축 글자·칸 제목
    for pane in panes:
        out.append(f'<text class="cap" x="{x0}" y="{_num(pane.top - 7)}">{escape(pane.caption)}</text>')
        for tick in pane.ticks:
            ty = pane.y(tick)
            cls = "base" if (pane.kind == "bar" and tick == pane.lo) else "grid"
            out.append(f'<line class="{cls}" x1="{x0}" x2="{_num(x0 + plot_w)}" y1="{_num(ty)}" y2="{_num(ty)}"/>')
            out.append(
                f'<text x="{x0 - 8}" y="{_num(ty + 4)}" text-anchor="end">{escape(tick_label(tick, pane.unit))}</text>'
            )

    # 2) 달별 마우스 띠 (표시보다 아래에 깔고, 표시는 pointer-events 없음)
    band_top, band_bottom = panes[0].top, panes[-1].bottom
    for i, label in enumerate(chart.labels):
        lines = [label]
        for s_idx, (name, values) in enumerate(chart.series):
            value = values[i] if i < len(values) else None
            lines.append(f"{name} {value_text(value, series_unit(chart, s_idx))}")
        out.append(
            f'<rect class="hit" x="{_num(x0 + slot * i)}" y="{_num(band_top)}" width="{_num(slot)}" '
            f'height="{_num(band_bottom - band_top)}"><title>{escape(chr(10).join(lines))}</title></rect>'
        )

    # 3) 막대·선
    out.append('<g class="marks">')
    for pane in panes:
        if pane.kind == "bar":
            out.extend(_bars(pane, x0, slot, n))
        else:
            out.extend(_lines(pane, x0, slot, n))
    out.append("</g>")

    # 4) x축 글자 (최근 달 기준으로 간격을 맞춘다, 좁은 화면에서는 x-alt 를 숨긴다)
    label_w = max(len(short_label(label)) for label in chart.labels) * 6.4
    step = max(1, math.ceil((label_w + 8) / slot))
    base_y = panes[-1].bottom + 17
    shown = 0
    for i in range(n - 1, -1, -1):
        if (n - 1 - i) % step:
            continue
        cls = ' class="x-alt"' if shown % 2 else ""
        cx = x0 + slot * (i + 0.5)
        out.append(
            f'<text{cls} x="{_num(cx)}" y="{_num(base_y)}" text-anchor="middle">{escape(short_label(chart.labels[i]))}</text>'
        )
        shown += 1
    out.append("</svg>")
    return "".join(out)


def _bars(pane: _Pane, x0: float, slot: float, n: int) -> list[str]:
    k = len(pane.series)
    gap = 2.0
    group = min(slot * (0.72 if k == 1 else 0.84), 24.0 * k + gap * (k - 1))
    bar_w = max(1.0, (group - gap * (k - 1)) / k)
    base = pane.y(max(pane.lo, 0.0))
    parts = []
    for i in range(n):
        left = x0 + slot * i + (slot - group) / 2
        for j, (color, _, values) in enumerate(pane.series):
            value = values[i] if i < len(values) else None
            if value is None or value <= 0:
                continue
            bx = left + j * (bar_w + gap)
            parts.append(f'<path class="bar s{color}" d="{_bar_path(bx, pane.y(value), bar_w, base)}"/>')
    return parts


def _lines(pane: _Pane, x0: float, slot: float, n: int) -> list[str]:
    parts = []
    for color, _, values in pane.series:
        points = [
            (x0 + slot * (i + 0.5), pane.y(values[i])) if i < len(values) and values[i] is not None else None
            for i in range(n)
        ]
        # None 에서 끊어 여러 조각으로 그린다
        segments: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for point in points:
            if point is None:
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(point)
        if current:
            segments.append(current)
        for seg in segments:
            if len(seg) == 1:
                px, py = seg[0]
                parts.append(f'<circle class="dot s{color}" cx="{_num(px)}" cy="{_num(py)}" r="3"/>')
                continue
            d = "M" + " L".join(f"{_num(px)},{_num(py)}" for px, py in seg)
            parts.append(f'<path class="ln s{color}" d="{d}"/>')
        # 마지막 값: 끝점 + 값 표시
        last = next((i for i in range(n - 1, -1, -1) if points[i] is not None), None)
        if last is not None:
            px, py = points[last]  # type: ignore[misc]
            parts.append(f'<circle class="dot s{color}" cx="{_num(px)}" cy="{_num(py)}" r="4"/>')
            text = end_label(values[last], pane.unit)  # type: ignore[arg-type]
            parts.append(f'<text class="end" x="{_num(px + 8)}" y="{_num(py + 4)}">{escape(text)}</text>')
    return parts


def chart_rows(chart: Chart) -> tuple[list[str], list[list[str]]]:
    """차트의 표 보기 (머리글, 행) — 접근성·인쇄용."""
    header = ["월"]
    for i, (name, _) in enumerate(chart.series):
        unit = series_unit(chart, i)
        header.append(f"{name}({unit})" if unit else name)
    rows = []
    for i, label in enumerate(chart.labels):
        row = [label]
        for _, values in chart.series:
            value = values[i] if i < len(values) else None
            row.append("" if value is None else tick_label(value, ""))
        rows.append(row)
    return header, rows

