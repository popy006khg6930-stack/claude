from datetime import date

import pytest

from silgeorae.demo import DEMO_DEAL_TYPES, DEMO_REGIONS, generate_demo_data, run_demo
from silgeorae.models import DealType
from silgeorae.processing import TransactionStore, normalize_items
from silgeorae.utils import recent_months

SALE, RENT = DealType.APT_SALE, DealType.APT_RENT
TODAY = date(2025, 3, 15)
MONTHS = recent_months(24, TODAY)


@pytest.fixture(scope="module")
def data():
    return generate_demo_data(MONTHS, as_of=TODAY)


def all_items(data, deal_type):
    return [item for (dt, _, _), items in data.items() if dt is deal_type for item in items]


def test_every_partition_is_present(data):
    codes = {region.lawd_cd for region in DEMO_REGIONS}
    assert codes == {"11680", "11440", "41135"}
    assert set(data) == {(dt, code, ym) for dt in DEMO_DEAL_TYPES for code in codes for ym in MONTHS}


def test_deterministic_and_seed_dependent():
    months = MONTHS[-3:]
    assert generate_demo_data(months) == generate_demo_data(months)
    assert generate_demo_data(months, seed=1) != generate_demo_data(months, seed=2)
    # 같은 달은 기간을 바꿔도 똑같다
    one = generate_demo_data(["202501"])
    many = generate_demo_data(["202412", "202501", "202502"])
    assert all(one[key] == many[key] for key in one)


def test_volume_is_reasonable(data):
    total = sum(len(items) for items in data.values())
    assert 600 <= total <= 2500
    assert len(all_items(data, SALE)) > 200 and len(all_items(data, RENT)) > 300


def test_items_look_like_real_api_records(data, fixture_tags):
    sales, rents = all_items(data, SALE), all_items(data, RENT)
    assert set(sales[0]) == fixture_tags("apt_trade.xml")
    assert set(rents[0]) == fixture_tags("apt_rent.xml")
    for item in sales[:50] + rents[:50]:
        assert all(isinstance(value, str) for value in item.values())
    assert any("," in item["dealAmount"] for item in sales)
    assert any("," in item["deposit"] for item in rents)
    assert {item["dealingGbn"] for item in sales} == {"중개거래", "직거래"}
    assert {item["umdNm"] for item in sales} >= {"대치동", "개포동", "역삼동", "아현동", "공덕동", "정자동", "서현동"}
    assert {round(float(item["excluUseAr"])) for item in sales} >= {60, 85, 115}


def test_cancellations_registrations_and_rent_mix(data):
    sales, rents = all_items(data, SALE), all_items(data, RENT)
    cancelled = [item for item in sales if item["cdealType"] == "O"]
    assert 0.01 <= len(cancelled) / len(sales) <= 0.08
    assert all(len(item["cdealDay"]) == 8 and item["cdealDay"][2] == "." for item in cancelled)  # YY.MM.DD
    assert sum(1 for item in sales if item["rgstDate"].strip()) > len(sales) / 2
    assert any(item["monthlyRent"] != "0" for item in rents)
    renewals = [item for item in rents if item["contractType"] == "갱신"]
    assert renewals and any(item["useRRRight"] == "사용" for item in renewals)
    assert all(item["preDeposit"].strip() for item in renewals)


def test_prices_follow_region_levels(data):
    def median_84(code):
        values = sorted(
            int(item["dealAmount"].replace(",", ""))
            for (dt, c, _), items in data.items() if dt is SALE and c == code
            for item in items if item["excluUseAr"].startswith("84")
        )
        return values[len(values) // 2]

    assert median_84("11680") > median_84("11440") > 100_000


def test_as_of_hides_future_reports_and_later_cancellations():
    full = generate_demo_data(MONTHS[-2:], as_of=None)
    early = generate_demo_data(MONTHS[-2:], as_of=date(2025, 2, 10))
    assert sum(map(len, early.values())) < sum(map(len, full.values()))
    for items in early.values():
        for item in items:
            deal = date(int(item["dealYear"]), int(item["dealMonth"]), int(item["dealDay"]))
            assert deal <= date(2025, 2, 10)


def test_items_normalize_without_errors(data):
    errors = []
    for (dt, code, _), items in list(data.items())[:24]:
        normalize_items(dt, items, lawd_cd=code, on_error=lambda raw, exc: errors.append(exc))
    assert errors == []


def test_run_demo_end_to_end(tmp_path):
    result = run_demo(tmp_path / "demo", months=6, today=TODAY)
    assert result.db_path == tmp_path / "demo" / "demo.db" and result.db_path.is_file()
    names = {path.name for path in result.report_paths}
    assert {"demo_report.html", "demo_transactions.csv"} <= names
    for path in result.report_paths:
        assert path.is_file() and path.stat().st_size > 0
    assert result.period == ("202410", "202503")
    assert result.regions == ["서울특별시 강남구", "서울특별시 마포구", "경기도 성남시 분당구"]
    assert result.initial_summary.inserted > 100
    assert result.summary.skipped > 0  # 2차 수집은 최근 3개월만
    with TransactionStore(result.db_path) as store:
        assert store.count() == result.transaction_count > 100
        new = store.query(first_seen_since=result.new_since)
    assert len(new) == result.summary.inserted

    again = run_demo(tmp_path / "demo", months=6, today=TODAY)  # 같은 폴더에 다시 실행
    assert again.transaction_count == result.transaction_count
