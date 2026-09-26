from datetime import date

import pytest

from silgeorae.utils import (
    format_area,
    format_manwon,
    month_range,
    parse_ym,
    recent_months,
    ym_add,
    ym_label,
    ym_to_date,
)


@pytest.mark.parametrize(
    "value, expected",
    [("202501", "202501"), (202501, "202501"), ("2025-01", "202501"), ("2025.1", "202501"),
     ("2025/12", "202512"), ("2025년 3월", "202503"), (" 2025-07 ", "202507")],
)
def test_parse_ym(value, expected):
    assert parse_ym(value) == expected


@pytest.mark.parametrize("value", ["2025-13", "25-01", "abc", "", "202500"])
def test_parse_ym_invalid(value):
    with pytest.raises(ValueError):
        parse_ym(value)


def test_month_math():
    assert ym_add("202501", -1) == "202412"
    assert ym_add("202412", 1) == "202501"
    assert ym_add("202503", -15) == "202312"
    assert month_range("2024-11", "2025-02") == ["202411", "202412", "202501", "202502"]
    assert recent_months(3, today=date(2025, 1, 15)) == ["202411", "202412", "202501"]
    assert ym_to_date("202502") == date(2025, 2, 1)
    assert ym_label("202502") == "2025.02"
    with pytest.raises(ValueError):
        month_range("202502", "202501")


@pytest.mark.parametrize(
    "value, expected",
    [(82500, "8억 2,500만원"), (5000, "5,000만원"), (100000, "10억원"), (0, "0원"),
     (None, "-"), (1234567, "123억 4,567만원")],
)
def test_format_manwon(value, expected):
    assert format_manwon(value) == expected


def test_format_manwon_short_and_area():
    assert format_manwon(82500, short=True) == "8.25억"
    assert format_manwon(100000, short=True) == "10억"
    assert format_manwon(9500, short=True) == "9,500만"
    assert format_area(84.97) == "84.97㎡(25.7평)"
