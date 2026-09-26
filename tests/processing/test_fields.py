from __future__ import annotations

from datetime import date, datetime

import pytest

from silgeorae.models import DealType
from silgeorae.processing.fields import (
    aliases_for,
    clean_text,
    consumed_keys,
    key_index,
    parse_cancel_flag,
    parse_float,
    parse_int,
    parse_short_date,
    parse_used_flag,
    pick,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, ""),
        ("  대치동 ", "대치동"),
        ("\u3000대치동\u3000", "대치동"),  # 전각 공백
        ("\xa0x\t\n", "x"),
        ("\ufeff\u200b사직동\u200b ", "사직동"),  # BOM·제로폭 공백
        (" ", ""),
        ("서울 강남구, 서울 서초구", "서울 강남구, 서울 서초구"),  # 안쪽 공백은 그대로
        (123, "123"),
        (b" abc ", "abc"),
    ],
)
def test_clean_text(value, expected):
    assert clean_text(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("  285,000", 285000),
        ("    82,500", 82500),
        ("285000", 285000),
        ("-1", -1),
        ("0", 0),
        ("00051", 51),
        ("\u3000 1,234 ", 1234),
        ("12.0", 12),
        ("", None),
        (" ", None),
        (None, None),
        ("abc", None),
        ("84.97", None),
        (7, 7),
        (7.0, 7),
        (7.5, None),
        (True, None),
        (float("nan"), None),
    ],
)
def test_parse_int(value, expected):
    assert parse_int(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("84.97", 84.97),
        (" 1,234.5 ", 1234.5),
        ("-0.5", -0.5),
        ("298.72", 298.72),
        (3, 3.0),
        (2.5, 2.5),
        ("", None),
        (" ", None),
        (None, None),
        ("x", None),
        ("nan", None),
        ("inf", None),
        (False, None),
    ],
)
def test_parse_float(value, expected):
    result = parse_float(value)
    assert result == expected
    if expected is not None:
        assert isinstance(result, float)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("25.01.28", date(2025, 1, 28)),
        ("25.1.8", date(2025, 1, 8)),
        (" 25.02.10 ", date(2025, 2, 10)),
        ("2025.01.28", date(2025, 1, 28)),
        ("2025-01-28", date(2025, 1, 28)),
        ("2025/1/8", date(2025, 1, 8)),
        ("20250128", date(2025, 1, 28)),
        ("2025년 1월 28일", date(2025, 1, 28)),
        (date(2025, 1, 2), date(2025, 1, 2)),
        (datetime(2025, 1, 2, 3, 4), date(2025, 1, 2)),
        ("", None),
        (" ", None),
        (None, None),
        ("25.13.01", None),
        ("25.02.30", None),
        ("abc", None),
        ("2025", None),
        ("미정", None),
    ],
)
def test_parse_short_date(value, expected):
    assert parse_short_date(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [("사용", True), (" 사용 ", True), ("Y", True), ("미사용", False), ("N", False), ("", None), (" ", None), (None, None), ("?", None)],
)
def test_parse_used_flag(value, expected):
    assert parse_used_flag(value) is expected


@pytest.mark.parametrize(
    "value, expected",
    [("O", True), ("o", True), (" O ", True), ("Y", True), ("y", True), ("", False), (" ", False), (None, False), ("N", False), ("0", False)],
)
def test_parse_cancel_flag(value, expected):
    assert parse_cancel_flag(value) is expected


def test_pick_first_non_blank():
    raw = {"deposit": " ", "보증금액": " 5,000 ", "보증금": "1"}
    assert pick(raw, ("deposit", "보증금액", "보증금")) == ("보증금액", "5,000")
    assert pick(raw, ("없는키",)) == ("", "")


def test_aliases_new_english_keys_first():
    for deal_type in DealType:
        table = aliases_for(deal_type)
        for field_name, keys in table.items():
            assert keys and keys[0].isascii(), (deal_type, field_name, keys)


def test_aliases_per_type():
    apt = aliases_for(DealType.APT_SALE)
    assert apt["price"] == ("dealAmount", "거래금액")
    assert apt["name"] == ("aptNm", "아파트", "단지")
    assert apt["area_m2"] == ("excluUseAr", "전용면적")
    assert apt["building_dong"] == ("aptDong", "동")
    assert apt["complex_id"] == ("aptSeq", "일련번호")
    assert apt["dong"] == ("umdNm", "법정동")
    assert apt["deal_year"] == ("dealYear", "년")
    assert "deposit" not in apt and "land_area_m2" not in apt and "contract_type" not in apt

    rent = aliases_for(DealType.APT_RENT)
    assert "price" not in rent
    assert rent["deposit"] == ("deposit", "보증금액", "보증금")
    assert rent["monthly_rent"] == ("monthlyRent", "월세금액", "월세")
    assert rent["renewal_right_used"] == ("useRRRight", "갱신요구권사용")

    assert aliases_for(DealType.PRESALE)["name"][0] == "aptNm"
    assert aliases_for(DealType.OFFI_RENT)["name"] == ("offiNm", "단지")
    assert aliases_for(DealType.RH_SALE)["name"] == ("mhouseNm", "연립다세대")
    assert aliases_for(DealType.RH_SALE)["land_area_m2"] == ("landAr", "대지권면적")
    assert "land_area_m2" not in aliases_for(DealType.RH_RENT)
    for deal_type in (DealType.SH_SALE, DealType.SH_RENT, DealType.LAND, DealType.COMMERCIAL, DealType.INDUSTRIAL):
        assert "name" not in aliases_for(deal_type)
    assert aliases_for(DealType.SH_SALE)["area_m2"] == ("totalFloorAr", "연면적")
    assert aliases_for(DealType.SH_RENT)["area_m2"] == ("totalFloorAr", "계약면적", "연면적")
    assert aliases_for(DealType.LAND)["area_m2"] == ("dealArea", "거래면적")
    for deal_type in (DealType.SH_SALE, DealType.COMMERCIAL, DealType.INDUSTRIAL):
        assert aliases_for(deal_type)["land_area_m2"] == ("plottageAr", "대지면적")
    for deal_type in (DealType.COMMERCIAL, DealType.INDUSTRIAL):
        assert aliases_for(deal_type)["area_m2"] == ("buildingAr", "건물면적")
    assert aliases_for("offi_sale") is aliases_for(DealType.OFFI_SALE)


def test_key_index_and_consumed_keys():
    index = key_index(DealType.APT_SALE)
    assert index["dealAmount"] == ("price", 0)
    assert index["거래금액"] == ("price", 1)
    assert index["sggCd"] == ("lawd_cd", 0)
    consumed = consumed_keys(DealType.APT_SALE)
    assert {"sggCd", "지역코드", "sggNm", "roadNm", "roadNmBonbun", "roadNmBubun"} <= consumed
    assert not {"umdCd", "bonbun", "landLeaseholdGbn", "roadNmbCd", "deposit"} & consumed
    assert "dealAmount" not in consumed_keys(DealType.APT_RENT)


def test_coerce_deal_type():
    from silgeorae.processing.fields import coerce_deal_type

    assert coerce_deal_type(DealType.LAND) is DealType.LAND
    assert coerce_deal_type("apt_rent") is DealType.APT_RENT
    assert coerce_deal_type("오피스텔 전월세") is DealType.OFFI_RENT
    with pytest.raises(ValueError):
        coerce_deal_type("우주정거장")
