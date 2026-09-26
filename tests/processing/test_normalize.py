from __future__ import annotations

import logging
from datetime import date

import pytest

from silgeorae.errors import NormalizationError
from silgeorae.models import DealType, Transaction
from silgeorae.processing import normalize_item, normalize_items


# ---------------------------------------------------------------------- 픽스처 파일별


def test_apt_trade_fixture(load_items):
    txs = normalize_items(DealType.APT_SALE, load_items("apt_trade"), lawd_cd="11680", sigungu="강남구")
    assert len(txs) == 3
    first, cancelled, direct = txs
    assert all(isinstance(t, Transaction) and t.deal_type is DealType.APT_SALE for t in txs)

    assert first.lawd_cd == "11680" and first.sigungu == "강남구"
    assert first.deal_date == date(2025, 1, 3)
    assert (first.dong, first.jibun, first.name) == ("대치동", "316", "한빛마을1단지")
    assert first.price == 285000
    assert first.area_m2 == 84.97 and isinstance(first.area_m2, float)
    assert first.floor == 12 and first.build_year == 2015
    assert first.building_dong == "101" and first.complex_id == "11680-9001"
    assert first.registration_date == date(2025, 2, 10)
    assert first.deal_method == "중개거래" and first.agent_location == "서울 강남구"
    assert first.seller == "개인" and first.buyer == "개인"
    assert first.road_address == "샘플로 51"
    assert first.is_cancelled is False and first.cancel_date is None
    assert first.deposit is None and first.monthly_rent is None and first.contract_type == ""
    assert first.extra == {
        "bonbun": "0316",
        "bubun": "0000",
        "landCd": "1",
        "landLeaseholdGbn": "N",
        "roadNmCd": "3121022",
        "roadNmSeq": "01",
        "roadNmSggCd": "11680",
        "roadNmbCd": "0",
        "umdCd": "10600",
    }

    assert cancelled.price == 312000  # "  312,000"
    assert cancelled.is_cancelled is True
    assert cancelled.cancel_date == date(2025, 1, 28)
    assert cancelled.agent_location == "서울 강남구, 서울 서초구"
    assert cancelled.registration_date is None
    assert cancelled.seller == "법인" and cancelled.area_m2 == 114.8 and cancelled.floor == 20

    assert direct.deal_method == "직거래"
    assert direct.agent_location == "" and direct.registration_date is None
    assert direct.building_dong == "" and direct.road_address == ""
    assert direct.jibun == "12-3" and direct.price == 98500
    assert direct.extra == {"bonbun": "0012", "bubun": "0003", "landCd": "1", "landLeaseholdGbn": "N", "umdCd": "10300"}

    assert [t.seq for t in txs] == [0, 0, 0]
    assert len({t.key for t in txs}) == 3


def test_apt_rent_fixture(load_items):
    jeonse, renewal, blank = normalize_items(DealType.APT_RENT, load_items("apt_rent"), lawd_cd="11680")
    assert jeonse.deposit == 150000 and jeonse.monthly_rent == 0 and jeonse.rent_kind == "전세"
    assert jeonse.price is None
    assert jeonse.contract_type == "신규" and jeonse.contract_term == "25.02~27.02"
    assert jeonse.renewal_right_used is None
    assert jeonse.prev_deposit is None and jeonse.prev_monthly_rent is None

    assert renewal.contract_type == "갱신" and renewal.renewal_right_used is True
    assert renewal.prev_deposit == 100000 and renewal.prev_monthly_rent == 100
    assert renewal.deposit == 100000 and renewal.monthly_rent == 120 and renewal.rent_kind == "월세"
    assert renewal.deal_date == date(2025, 1, 21) and renewal.floor == 15

    assert blank.contract_type == "" and blank.contract_term == ""
    assert blank.deposit == 45000 and blank.name == "푸른숲" and blank.dong == "개포동"
    assert all(t.extra == {} for t in (jeonse, renewal, blank))
    assert all(t.deal_method == "" and t.registration_date is None for t in (jeonse, renewal, blank))


def test_offi_trade_fixture(load_items):
    raw = load_items("offi_trade")[0]
    (tx,) = normalize_items(DealType.OFFI_SALE, [raw])
    assert tx.name == "은하수타워"
    assert tx.sigungu == "강남구"  # 원본 sggNm
    assert tx.lawd_cd == "11680"  # 원본 sggCd (조회 코드 인자 없음)
    assert tx.area_m2 == 29.5 and tx.price == 31500 and tx.floor == 9 and tx.dong == "역삼동"
    assert tx.build_year == 2019 and tx.extra == {}
    # 인자로 준 시군구 이름이 우선
    assert normalize_item(DealType.OFFI_SALE, raw, sigungu="서울특별시 강남구").sigungu == "서울특별시 강남구"


def test_rh_trade_fixture(load_items):
    (tx,) = normalize_items(DealType.RH_SALE, load_items("rh_trade"), lawd_cd="11440")
    assert tx.floor == -1  # 지하
    assert tx.land_area_m2 == 28.1
    assert tx.house_type == "다세대"
    assert tx.name == "샘플빌라" and tx.jibun == "401-12" and tx.dong == "망원동"
    assert tx.area_m2 == 49.8 and tx.price == 42000 and tx.build_year == 2004
    assert tx.registration_date == date(2025, 1, 30)
    assert tx.extra == {}


def test_sh_trade_fixture(load_items):
    (tx,) = normalize_items(DealType.SH_SALE, load_items("sh_trade"), lawd_cd="11440")
    assert tx.area_m2 == 298.72  # 연면적
    assert tx.land_area_m2 == 165.3  # 대지면적
    assert tx.jibun == "3**"  # 마스킹 그대로
    assert tx.house_type == "다가구" and tx.name == ""
    assert tx.price == 155000 and tx.build_year == 1990 and tx.floor is None
    assert tx.extra == {}


def test_legacy_korean_tags(load_items):
    (tx,) = normalize_items(DealType.APT_SALE, load_items("legacy_apt_trade"))
    assert tx.lawd_cd == "11110"  # 지역코드
    assert tx.deal_date == date(2021, 12, 6)
    assert tx.price == 82500
    assert tx.dong == "사직동"  # " 사직동" → 앞 공백 제거
    assert tx.name == "샘플힐스" and tx.jibun == "9"
    assert tx.building_dong == "102"  # <동>
    assert tx.floor == 7 and tx.area_m2 == 84.99 and tx.build_year == 2008
    assert tx.deal_method == "중개거래" and tx.agent_location == "서울 종로구"
    assert tx.seller == "개인" and tx.buyer == "개인"
    assert tx.is_cancelled is False and tx.cancel_date is None and tx.registration_date is None
    assert tx.extra == {}


def test_legacy_rent_tags():
    raw = {
        "년": "2021", "월": "12", "일": "6", "지역코드": "11110", "법정동": "사직동", "아파트": "샘플힐스",
        "지번": "9", "전용면적": "84.99", "층": "7", "건축년도": "2008",
        "보증금액": "50,000", "월세금액": "100", "계약구분": "갱신", "계약기간": "21.12~23.12",
        "갱신요구권사용": "사용", "종전계약보증금": "45,000", "종전계약월세": "90",
    }
    tx = normalize_item(DealType.APT_RENT, raw)
    assert (tx.deposit, tx.monthly_rent) == (50000, 100)
    assert tx.contract_type == "갱신" and tx.contract_term == "21.12~23.12"
    assert tx.renewal_right_used is True
    assert (tx.prev_deposit, tx.prev_monthly_rent) == (45000, 90)
    assert tx.name == "샘플힐스" and tx.area_m2 == 84.99 and tx.extra == {}

    older = normalize_item(DealType.APT_RENT, {"년": "2019", "월": "3", "일": "2", "보증금": "3,000", "월세": "50"}, lawd_cd="11110")
    assert (older.deposit, older.monthly_rent) == (3000, 50)


def test_sh_rent_contract_area_and_offi_legacy_name():
    sh = normalize_item(
        DealType.SH_RENT,
        {"년": "2021", "월": "5", "일": "1", "계약면적": "120.5", "주택유형": "다가구", "보증금액": "20,000", "월세금액": "0"},
        lawd_cd="11440",
    )
    assert sh.area_m2 == 120.5 and sh.house_type == "다가구" and sh.name == "" and sh.rent_kind == "전세"
    assert normalize_item(DealType.SH_RENT, {"dealYear": "2025", "dealMonth": "1", "dealDay": "2", "totalFloorAr": "99.1"},
                          lawd_cd="11440").area_m2 == 99.1

    offi = normalize_item(DealType.OFFI_RENT, {"년": "2021", "월": "5", "일": "1", "단지": "샘플오피스텔", "시군구": "마포구"},
                          lawd_cd="11440")
    assert offi.name == "샘플오피스텔" and offi.sigungu == "마포구"


def test_land_commercial_presale_types():
    land = normalize_item(
        DealType.LAND,
        {
            "sggCd": "41135", "sggNm": "성남시 분당구", "umdNm": "정자동", "jibun": "1**", "jimok": "대",
            "landUse": "제2종일반주거지역", "dealArea": "330.5", "dealingGbn": "중개거래", "dealYear": "2025",
            "dealMonth": "1", "dealDay": "9", "dealAmount": "120,000", "cdealType": " ", "cdealDay": " ",
            "estateAgentSggNm": "경기 성남시 분당구", "shareDealingType": "지분",
        },
    )
    assert land.area_m2 == 330.5 and land.land_area_m2 is None and land.name == ""
    assert land.price == 120000 and land.sigungu == "성남시 분당구" and land.jibun == "1**"
    assert land.extra == {"jimok": "대", "landUse": "제2종일반주거지역", "shareDealingType": "지분"}

    commercial = normalize_item(
        DealType.COMMERCIAL,
        {
            "sggCd": "11680", "sggNm": "강남구", "umdNm": "역삼동", "jibun": "7**", "buildingType": "일반",
            "buildingUse": "제2종근린생활", "landUse": "일반상업", "floor": " ", "dealYear": "2025", "dealMonth": "1",
            "dealDay": "20", "dealAmount": "1,250,000", "buildYear": "1995", "buildingAr": "812.4",
            "plottageAr": "301.2", "cdealType": " ", "dealingGbn": "중개거래", "shareDealingType": " ",
            "estateAgentSggNm": "서울 강남구", "slerGbn": "법인", "buyerGbn": "개인",
        },
    )
    assert commercial.area_m2 == 812.4 and commercial.land_area_m2 == 301.2
    assert commercial.price == 1250000 and commercial.floor is None and commercial.name == ""
    assert commercial.seller == "법인"
    assert commercial.extra == {"buildingType": "일반", "buildingUse": "제2종근린생활", "landUse": "일반상업"}

    industrial = normalize_item(
        DealType.INDUSTRIAL,
        {"sggCd": "41590", "dealYear": "2025", "dealMonth": "1", "dealDay": "7", "buildingAr": "1,500.5",
         "plottageAr": "3,000", "dealAmount": "450,000"},
    )
    assert industrial.area_m2 == 1500.5 and industrial.land_area_m2 == 3000.0

    presale = normalize_item(
        DealType.PRESALE,
        {"sggCd": "11680", "umdNm": "개포동", "jibun": "189", "aptNm": "샘플포레", "floor": "15",
         "excluUseAr": "59.98", "ownershipGbn": "분", "dealYear": "2025", "dealMonth": "1", "dealDay": "14",
         "dealAmount": "150,000"},
    )
    assert presale.name == "샘플포레" and presale.area_m2 == 59.98 and presale.price == 150000
    assert presale.extra == {"ownershipGbn": "분"}


# ---------------------------------------------------------------------- 개별 규칙


def test_lawd_cd_argument_wins_and_raw_code_kept(load_items):
    raw = load_items("apt_trade")[0]
    tx = normalize_item(DealType.APT_SALE, raw, lawd_cd="11690")
    assert tx.lawd_cd == "11690"
    assert tx.extra["sggCd"] == "11680"
    assert "sggCd" not in normalize_item(DealType.APT_SALE, raw, lawd_cd="11680").extra
    assert normalize_item(DealType.APT_SALE, raw).lawd_cd == "11680"  # 인자 없으면 원본 sggCd

    legacy = load_items("legacy_apt_trade")[0]
    assert normalize_item(DealType.APT_SALE, legacy, lawd_cd="11140").extra["sggCd"] == "11110"


def test_missing_region_code():
    raw = {"dealYear": "2025", "dealMonth": "1", "dealDay": "3", "dealAmount": "1,000"}
    with pytest.raises(NormalizationError, match="시군구 코드"):
        normalize_item(DealType.APT_SALE, raw)
    assert normalize_item(DealType.APT_SALE, raw, lawd_cd="11680").lawd_cd == "11680"


@pytest.mark.parametrize(
    "changes",
    [
        {"dealDay": None},  # 없음
        {"dealDay": " "},  # 빈 값
        {"dealYear": "이천이십오"},  # 숫자 아님
        {"dealMonth": "13"},
        {"dealMonth": "2", "dealDay": "30"},
        {"dealYear": "0"},
    ],
)
def test_bad_deal_date_raises(load_items, changes):
    raw = dict(load_items("apt_trade")[0])
    for key, value in changes.items():
        if value is None:
            del raw[key]
        else:
            raw[key] = value
    with pytest.raises(NormalizationError) as info:
        normalize_item(DealType.APT_SALE, raw, lawd_cd="11680")
    message = str(info.value)
    assert "계약일" in message
    assert "한빛마을1단지" in message  # 원본 레코드 요약 포함


def test_non_mapping_record():
    with pytest.raises(NormalizationError, match="dict"):
        normalize_item(DealType.APT_SALE, ["not", "a", "dict"], lawd_cd="11680")


def test_normalize_items_skips_bad_records(load_items):
    good = load_items("apt_trade")
    missing = dict(good[0])
    del missing["dealDay"]
    feb30 = dict(good[0], dealMonth="2", dealDay="30")
    raws = [good[0], missing, "not a dict", feb30, good[2]]

    errors: list = []
    txs = normalize_items(DealType.APT_SALE, raws, lawd_cd="11680", on_error=lambda raw, exc: errors.append((raw, exc)))
    assert [t.name for t in txs] == ["한빛마을1단지", "푸른숲"]
    assert len(errors) == 3
    assert errors[0][0] is missing
    assert all(isinstance(exc, NormalizationError) for _, exc in errors)

    with pytest.raises(NormalizationError):
        normalize_items(DealType.APT_SALE, raws, lawd_cd="11680", strict=True)


def test_normalize_items_logs_warning_without_callback(load_items, caplog):
    good = load_items("apt_trade")[0]
    bad = dict(good, dealMonth="")
    with caplog.at_level(logging.WARNING, logger="silgeorae.processing"):
        txs = normalize_items(DealType.APT_SALE, iter([bad, good]), lawd_cd="11680")
    assert len(txs) == 1
    assert "건너뜁니다" in caplog.text


def test_identical_records_get_distinct_keys(load_items):
    items = load_items("apt_trade")
    raws = [items[0], dict(items[0]), items[2]]
    txs = normalize_items(DealType.APT_SALE, raws, lawd_cd="11680")
    assert [t.seq for t in txs] == [0, 1, 0]
    assert txs[0].key != txs[1].key
    assert len({t.key for t in txs}) == 3
    again = normalize_items(DealType.APT_SALE, raws, lawd_cd="11680")
    assert [t.key for t in again] == [t.key for t in txs]  # 다시 받아도 같은 키


def test_build_year_zero_or_negative_is_none(load_items):
    raw = load_items("offi_trade")[0]
    for value in ("0", "-1"):
        tx = normalize_item(DealType.OFFI_SALE, dict(raw, buildYear=value))
        assert tx.build_year is None
        assert "buildYear" not in tx.extra


@pytest.mark.parametrize("flag, expected", [("O", True), ("o", True), ("Y", True), (" ", False), ("", False)])
def test_cancel_flag_variants(load_items, flag, expected):
    raw = dict(load_items("apt_trade")[0], cdealType=flag)
    assert normalize_item(DealType.APT_SALE, raw, lawd_cd="11680").is_cancelled is expected


def test_sale_and_rent_fields_are_separated(load_items):
    rent_raw = dict(load_items("apt_rent")[0], dealAmount="99,000")
    rent = normalize_item(DealType.APT_RENT, rent_raw, lawd_cd="11680")
    assert rent.price is None
    assert rent.extra["dealAmount"] == "99,000"  # 쓰지 않은 원본 필드는 extra 로

    sale_raw = dict(load_items("apt_trade")[0], deposit="1,000", contractType="신규")
    sale = normalize_item(DealType.APT_SALE, sale_raw, lawd_cd="11680")
    assert sale.deposit is None and sale.contract_type == ""
    assert sale.extra["deposit"] == "1,000" and sale.extra["contractType"] == "신규"


def test_road_address_variants(load_items):
    base = load_items("apt_trade")[0]

    def road(**changes):
        return normalize_item(DealType.APT_SALE, dict(base, **changes), lawd_cd="11680").road_address

    assert road() == "샘플로 51"
    assert road(roadNmBonbun="00012", roadNmBubun="00003") == "샘플로 12-3"
    assert road(roadNmbCd="1") == "샘플로 지하 51"
    assert road(roadNmBonbun=" ", roadNmBubun=" ") == "샘플로"
    assert road(roadNmBonbun="00000") == "샘플로"
    assert road(roadNm=" ") == ""
    legacy = {"년": "2021", "월": "1", "일": "2", "도로명": "사직로", "도로명건물본번호코드": "00009", "도로명건물부번호코드": "00001"}
    assert normalize_item(DealType.APT_SALE, legacy, lawd_cd="11110").road_address == "사직로 9-1"


def test_unparsable_values_kept_in_extra(load_items):
    raw = dict(load_items("apt_trade")[0], rgstDate="미정", floor="B1", excluUseAr="약 85")
    tx = normalize_item(DealType.APT_SALE, raw, lawd_cd="11680")
    assert tx.registration_date is None and tx.floor is None and tx.area_m2 is None
    assert tx.extra["rgstDate"] == "미정"
    assert tx.extra["floor"] == "B1"
    assert tx.extra["excluUseAr"] == "약 85"


def test_text_fields_cleaned_and_raw_not_mutated(load_items):
    raw = dict(load_items("apt_trade")[0], aptNm="\u3000한빛마을1단지 ", umdNm=" 대치동\u3000", dealingGbn=" ")
    snapshot = dict(raw)
    tx = normalize_item(DealType.APT_SALE, raw, lawd_cd=" 11680 ")
    assert tx.name == "한빛마을1단지" and tx.dong == "대치동" and tx.deal_method == ""
    assert tx.lawd_cd == "11680"
    assert raw == snapshot


def test_deal_type_given_as_text(load_items):
    raw = load_items("apt_rent")[0]
    assert normalize_item("apt_rent", raw, lawd_cd="11680").deal_type is DealType.APT_RENT
    assert normalize_items("아파트 전월세", [raw], lawd_cd="11680")[0].deal_type is DealType.APT_RENT
    with pytest.raises(ValueError):
        normalize_items("우주정거장", [raw])


def test_unknown_cancel_flag_kept_in_extra(load_items):
    base = load_items("apt_trade")[0]
    odd = normalize_item(DealType.APT_SALE, dict(base, cdealType="해제?"), lawd_cd="11680")
    assert odd.is_cancelled is False and odd.extra["cdealType"] == "해제?"
    plain = normalize_item(DealType.APT_SALE, dict(base, cdealType="N"), lawd_cd="11680")
    assert plain.is_cancelled is False and "cdealType" not in plain.extra
