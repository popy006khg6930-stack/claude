from datetime import date

import pytest

from silgeorae.models import DealType, PropertyType, TradeType, Transaction, assign_seq


def make_tx(**kw):
    base = dict(
        deal_type=DealType.APT_SALE,
        lawd_cd="11680",
        deal_date=date(2025, 1, 3),
        dong="대치동",
        jibun="316",
        name="한빛마을1단지",
        floor=12,
        area_m2=84.97,
        price=285000,
    )
    base.update(kw)
    return Transaction(**base)


def test_deal_type_metadata():
    assert DealType.APT_SALE.property_type is PropertyType.APT
    assert DealType.APT_RENT.trade_type is TradeType.RENT
    assert DealType.APT_SALE.label == "아파트 매매"
    assert DealType.OFFI_RENT.label == "오피스텔 전월세"
    assert DealType.APT_SALE.api_service == "RTMSDataSvcAptTradeDev"
    assert str(DealType.RH_SALE) == "rh_sale"
    assert f"{DealType.SH_RENT}" == "sh_rent"
    assert all(dt.api_service.startswith("RTMSDataSvc") for dt in DealType)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("apt_sale", DealType.APT_SALE),
        ("아파트", DealType.APT_SALE),
        ("아파트 매매", DealType.APT_SALE),
        ("아파트전월세", DealType.APT_RENT),
        ("아파트 전세", DealType.APT_RENT),
        ("APT-RENT", DealType.APT_RENT),
        ("오피스텔", DealType.OFFI_SALE),
        ("빌라 월세", DealType.RH_RENT),
        ("단독", DealType.SH_SALE),
        ("분양권", DealType.PRESALE),
        ("토지", DealType.LAND),
    ],
)
def test_deal_type_parse(text, expected):
    assert DealType.parse(text) is expected


def test_deal_type_parse_unknown():
    with pytest.raises(ValueError):
        DealType.parse("우주정거장")


def test_deal_type_parse_many():
    assert DealType.parse_many("apt_sale, apt_rent,apt_sale") == [DealType.APT_SALE, DealType.APT_RENT]
    assert DealType.parse_many(["all"]) == list(DealType)
    housing = DealType.parse_many("housing")
    assert len(housing) == 8 and DealType.LAND not in housing


def test_key_stable_and_ignores_mutable_fields():
    a = make_tx()
    b = make_tx(is_cancelled=True, cancel_date=date(2025, 1, 20), registration_date=date(2025, 2, 1))
    assert a.key == b.key
    assert len(a.key) == 24
    assert make_tx(price=285001).key != a.key


def test_assign_seq_distinguishes_identical_deals():
    txs = assign_seq([make_tx(), make_tx(), make_tx(floor=3)])
    assert [t.seq for t in txs] == [0, 1, 0]
    assert len({t.key for t in txs}) == 3


def test_derived_values():
    tx = make_tx()
    assert tx.deal_ym == "202501"
    assert tx.area_type == 84
    assert tx.pyeong == pytest.approx(25.70, abs=0.01)
    assert tx.price_per_pyeong == pytest.approx(285000 / (84.97 / 3.305785))
    assert tx.price_text == "28억 5,000만원"
    assert tx.rent_kind == ""
    assert tx.complex_key == "11680|대치동|316|한빛마을1단지"


def test_rent_kind_and_price_text():
    jeonse = make_tx(deal_type=DealType.APT_RENT, price=None, deposit=150000, monthly_rent=0)
    wolse = make_tx(deal_type=DealType.APT_RENT, price=None, deposit=10000, monthly_rent=150)
    assert jeonse.rent_kind == "전세" and jeonse.price_text == "전세 15억원"
    assert wolse.rent_kind == "월세" and wolse.price_text == "월세 1억원/150만원"
    assert jeonse.price_per_pyeong is None


def test_dict_roundtrip():
    tx = make_tx(cancel_date=date(2025, 1, 20), is_cancelled=True, extra={"landLeaseholdGbn": "N"})
    again = Transaction.from_dict(tx.to_dict())
    assert again == tx
    assert again.key == tx.key
