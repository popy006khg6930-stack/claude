from __future__ import annotations

import logging
import sqlite3
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone

import pytest

from silgeorae.errors import StorageError
from silgeorae.models import DealType, Transaction
from silgeorae.processing import FetchLogEntry, PartitionResult, TransactionStore, normalize_items
from silgeorae.processing import storage as storage_module
from silgeorae.utils import KST

T0 = datetime(2025, 2, 1, 9, 0, tzinfo=KST)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
T3 = T0 + timedelta(hours=3)
T4 = T0 + timedelta(hours=4)
T5 = T0 + timedelta(hours=5)

APT = DealType.APT_SALE


def _stamps(tx: Transaction) -> tuple:
    return tx.first_seen_at, tx.updated_at


def _without_stamps(tx: Transaction) -> dict:
    data = tx.to_dict()
    del data["first_seen_at"], data["updated_at"]
    return data


@pytest.fixture
def store():
    with TransactionStore() as s:
        yield s


@pytest.fixture
def apt_raws(load_items):
    """apt_trade.xml 의 원본 3건. v1 = 해제·등기 전, v2 = 픽스처 그대로."""
    v2 = load_items("apt_trade")
    v1 = [dict(item) for item in v2]
    v1[0]["rgstDate"] = " "  # 아직 등기 전
    v1[1]["cdealType"] = " "  # 아직 해제 전
    v1[1]["cdealDay"] = " "
    return v1, v2


def _norm(raws):
    return normalize_items(APT, raws, lawd_cd="11680", sigungu="강남구")


# ---------------------------------------------------------------------- 파티션 교체


def test_replace_partition_lifecycle(store, apt_raws):
    v1, v2 = apt_raws

    # 1) 처음 저장 → 모두 신규
    first = _norm(v1)
    result = store.replace_partition(APT, "11680", "202501", first, fetched_at=T0)
    assert (result.inserted, result.updated, result.unchanged, result.removed) == (3, 0, 0, 0)
    assert result.new_keys == [t.key for t in first]
    assert result.newly_cancelled == 0 and result.cancelled_keys == []
    for tx in first:  # 넘겨준 객체에도 저장소 시각이 채워진다
        assert _stamps(tx) == (T0, T0)
        assert _stamps(store.get(tx.key)) == (T0, T0)

    # 2) 같은 내용 다시 → 변화 없음, updated_at 그대로
    result = store.replace_partition(APT, "11680", "202501", _norm(v1), fetched_at=T1)
    assert (result.inserted, result.updated, result.unchanged, result.removed) == (0, 0, 3, 0)
    assert result.new_keys == []
    assert all(_stamps(store.get(t.key)) == (T0, T0) for t in first)
    assert store.last_fetched(APT, "11680", "202501") == T1

    # 3) 가격은 그대로, 해제·등기 반영 → 같은 키로 UPDATE
    second = _norm(v2)
    assert [t.key for t in second] == [t.key for t in first]
    result = store.replace_partition(APT, "11680", "202501", second, fetched_at=T2)
    assert (result.inserted, result.updated, result.unchanged, result.removed) == (0, 2, 1, 0)
    assert result.newly_cancelled == 1
    assert result.cancelled_keys == [second[1].key]
    cancelled = store.get(second[1].key)
    assert cancelled.is_cancelled is True and cancelled.cancel_date == date(2025, 1, 28)
    assert _stamps(cancelled) == (T0, T2)  # first_seen_at 유지
    registered = store.get(second[0].key)
    assert registered.registration_date == date(2025, 2, 10) and _stamps(registered) == (T0, T2)
    assert _stamps(store.get(second[2].key)) == (T0, T0)

    # 4) 이미 해제된 건은 다시 세지 않는다
    result = store.replace_partition(APT, "11680", "202501", _norm(v2), fetched_at=T3)
    assert (result.updated, result.unchanged, result.newly_cancelled) == (0, 3, 0)

    # 5) 목록에서 사라진 거래 → 삭제
    result = store.replace_partition(APT, "11680", "202501", _norm(v2[:2]), fetched_at=T4)
    assert (result.inserted, result.updated, result.unchanged, result.removed) == (0, 0, 2, 1)
    assert store.get(second[2].key) is None
    assert store.count() == 2

    # 6) 새 거래 등장 → 신규
    newcomer = dict(v2[2], dealDay="29", dealAmount="99,000")
    third = _norm(v2[:2] + [newcomer])
    result = store.replace_partition(APT, "11680", "202501", third, fetched_at=T5)
    assert (result.inserted, result.updated, result.unchanged, result.removed) == (1, 0, 2, 0)
    assert result.new_keys == [third[2].key]
    assert _stamps(store.get(third[2].key)) == (T5, T5)
    assert store.count() == 3

    entry = store.fetch_log()[0]
    assert (entry.deal_type, entry.lawd_cd, entry.deal_ym) == (APT, "11680", "202501")
    assert (entry.fetched_at, entry.item_count, entry.status, entry.message) == (T5, 3, "ok", "")


def test_replace_partition_only_touches_its_partition(store, make_tx):
    store.upsert([make_tx(deal_date=date(2025, 2, 3)), make_tx(lawd_cd="11440"), make_tx(deal_type=DealType.APT_RENT)])
    result = store.replace_partition(APT, "11680", "202501", [])
    assert result.removed == 0
    assert store.count() == 3
    assert store.fetch_log()[0].item_count == 0


def test_replace_partition_with_empty_list_clears_partition(store, make_tx):
    store.replace_partition(APT, "11680", "202501", [make_tx(), make_tx(floor=3)])
    result = store.replace_partition(APT, "11680", "202501", [])
    assert result.removed == 2 and store.count() == 0
    assert store.last_fetched(APT, "11680", "202501") is not None


def test_replace_partition_rejects_foreign_transactions(store, make_tx):
    store.replace_partition(APT, "11680", "202501", [make_tx()], fetched_at=T0)
    with pytest.raises(StorageError, match="거래 유형"):
        store.replace_partition(APT, "11680", "202501", [make_tx(floor=1), make_tx(deal_type=DealType.APT_RENT)])
    with pytest.raises(StorageError, match="시군구"):
        store.replace_partition(APT, "11680", "202501", [make_tx(floor=1), make_tx(lawd_cd="11440")])
    with pytest.raises(StorageError):
        store.replace_partition(APT, "11680", "202501", ["not a transaction"])
    # 아무것도 바뀌지 않았다
    assert store.count() == 1
    assert store.fetch_log()[0].fetched_at == T0


def test_replace_partition_accepts_text_arguments(store, make_tx):
    result = store.replace_partition("apt_sale", " 11680 ", "2025-01", [make_tx()])
    assert result.inserted == 1
    assert store.last_fetched(APT, "11680", 202501) is not None


def test_replace_partition_assigns_seq_to_duplicates(store, make_tx):
    txs = [make_tx(), make_tx(), make_tx(floor=3)]  # 호출자가 assign_seq 를 하지 않음
    result = store.replace_partition(APT, "11680", "202501", txs)
    assert result.inserted == 3
    assert [t.seq for t in txs] == [0, 1, 0]
    assert len(set(result.new_keys)) == 3
    assert sorted(t.seq for t in store.query()) == [0, 0, 1]


def test_same_object_twice_is_rejected(store, make_tx):
    tx = make_tx()
    with pytest.raises(StorageError, match="여러 번"):
        store.replace_partition(APT, "11680", "202501", [tx, tx])


def test_out_of_month_transaction_is_stored_with_warning(store, make_tx, caplog):
    inside = make_tx()
    outside = make_tx(deal_date=date(2024, 12, 31))
    with caplog.at_level(logging.WARNING, logger="silgeorae.processing"):
        result = store.replace_partition(APT, "11680", "202501", [inside, outside])
    assert result.inserted == 2
    assert "계약연월이 다른" in caplog.text
    assert [t.deal_ym for t in store.query(start_ym="2024-12", end_ym="2024-12")] == ["202412"]
    # 다시 받으면 키로 찾아 '그대로'로 센다 (중복 INSERT 아님)
    again = store.replace_partition(APT, "11680", "202501", [make_tx(), make_tx(deal_date=date(2024, 12, 31))])
    assert (again.inserted, again.unchanged, again.removed) == (0, 2, 0)


def test_replace_partition_is_atomic(store, make_tx, monkeypatch):
    store.replace_partition(APT, "11680", "202501", [make_tx()], fetched_at=T0)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("디스크 오류 흉내")

    monkeypatch.setattr(storage_module, "_record_fetch", broken)
    with pytest.raises(StorageError, match="디스크 오류 흉내"):
        store.replace_partition(APT, "11680", "202501", [make_tx(floor=1), make_tx(floor=2)], fetched_at=T1)
    monkeypatch.undo()
    # 앞서 실행된 INSERT/DELETE 도 모두 취소되었다
    assert [t.floor for t in store.query()] == [12]
    assert store.fetch_log()[0].fetched_at == T0

    with pytest.raises(StorageError, match="extra"):  # JSON 으로 못 바꾸는 값
        store.replace_partition(APT, "11680", "202501", [make_tx(extra={"x": object()})])
    assert [t.floor for t in store.query()] == [12]


# ---------------------------------------------------------------------- upsert


def test_upsert_merges_without_deleting(store, make_tx):
    a, b = make_tx(), make_tx(floor=3)
    result = store.upsert([a, b], now=T0)
    assert (result.inserted, result.updated, result.unchanged, result.removed) == (2, 0, 0, 0)
    assert result.new_keys == [a.key, b.key]

    a2 = make_tx(is_cancelled=True, cancel_date=date(2025, 1, 20))
    result = store.upsert([a2], now=T1)
    assert (result.inserted, result.updated, result.removed, result.newly_cancelled) == (0, 1, 0, 1)
    assert result.cancelled_keys == [a.key]
    assert store.count() == 2  # b 는 그대로 남는다
    assert _stamps(store.get(a.key)) == (T0, T1)
    assert _stamps(a2) == (T0, T1)
    assert store.fetch_log() == []  # upsert 는 수집 기록을 남기지 않는다

    assert store.upsert([], now=T2) == PartitionResult()


def test_numbers_are_canonicalized_so_keys_match(store, make_tx):
    tx = make_tx(area_m2=85, floor=3.0, price=90000.0, deal_date=datetime(2025, 1, 3, 15, 0))
    store.upsert([tx])
    assert isinstance(tx.area_m2, float) and tx.floor == 3 and isinstance(tx.floor, int)
    assert tx.deal_date == date(2025, 1, 3) and type(tx.deal_date) is date
    stored = store.get(tx.key)
    assert stored is not None and stored.key == tx.key
    assert store.upsert([make_tx(area_m2=85.0, floor=3, price=90000)]).unchanged == 1


def test_invalid_values_raise_storage_error(store, make_tx):
    with pytest.raises(StorageError, match="price"):
        store.upsert([make_tx(price="비공개")])
    with pytest.raises(StorageError, match="deal_date"):
        store.upsert([make_tx(deal_date=None)])
    assert store.count() == 0


# ---------------------------------------------------------------------- 조회


@pytest.fixture
def filled(store, make_tx):
    rows = [
        make_tx(deal_date=date(2024, 12, 20), area_m2=84.97, price=280000),
        make_tx(deal_date=date(2025, 1, 3), area_m2=114.8, price=312000, is_cancelled=True),
        make_tx(deal_date=date(2025, 1, 27), name="푸른숲", dong="개포동", jibun="12-3", area_m2=59.96, price=98500),
        make_tx(deal_type=DealType.APT_RENT, deal_date=date(2025, 1, 8), price=None, deposit=150000, monthly_rent=0),
        make_tx(lawd_cd="11440", deal_date=date(2025, 2, 5), name="Hanbit Tower", dong="망원동", area_m2=49.8, price=42000),
    ]
    store.upsert(rows, now=T0)
    late = make_tx(deal_type=DealType.SH_SALE, lawd_cd="11440", deal_date=date(2025, 1, 19), name="", dong="연남동",
                   jibun="3**", area_m2=298.72, land_area_m2=165.3, price=155000, house_type="다가구")
    store.upsert([late], now=T2)
    return store


def _dates(txs):
    return [t.deal_date.isoformat() for t in txs]


def test_query_all_ordered_by_deal_date(filled):
    txs = filled.query()
    assert _dates(txs) == ["2024-12-20", "2025-01-03", "2025-01-08", "2025-01-19", "2025-01-27", "2025-02-05"]
    assert all(t.first_seen_at is not None and t.updated_at is not None for t in txs)
    assert _dates(filled.query(limit=2)) == ["2024-12-20", "2025-01-03"]
    assert filled.query(limit=0) == []


@pytest.mark.parametrize(
    "filters, expected",
    [
        ({"deal_types": DealType.APT_SALE}, 4),
        ({"deal_types": "apt_rent"}, 1),
        ({"deal_types": ["apt_sale", DealType.SH_SALE]}, 5),
        ({"deal_types": "아파트"}, 4),
        ({"deal_types": []}, 0),
        ({"lawd_cds": "11440"}, 2),
        ({"lawd_cds": ["11680"]}, 4),
        ({"lawd_cds": "11680,11440"}, 6),
        ({"lawd_cds": []}, 0),
        ({"start_ym": "2025-01"}, 5),
        ({"end_ym": 202501}, 5),
        ({"start_ym": "2025.01", "end_ym": "2025-01"}, 4),
        ({"name": "한빛"}, 3),
        ({"name": "한빛 마을"}, 3),  # 공백 무시
        ({"name": "hanbit"}, 1),  # 대소문자 무시
        ({"name": ["푸른", "TOWER"]}, 2),
        ({"name": ""}, 6),
        ({"dong": "대치"}, 3),
        ({"dong": "망원동"}, 1),
        ({"include_cancelled": False}, 5),
        ({"min_area": 60}, 4),
        ({"max_area": 60}, 2),
        ({"min_area": 50, "max_area": 100}, 3),
        ({"first_seen_since": T1}, 1),
        ({"first_seen_since": T0}, 6),
        ({"deal_types": APT, "lawd_cds": "11680", "start_ym": "202501", "include_cancelled": False}, 1),
    ],
)
def test_query_filters(filled, filters, expected):
    txs = filled.query(**filters)
    assert len(txs) == expected
    assert filled.count(**filters) == expected


def test_query_filter_contents(filled):
    (sh,) = filled.query(deal_types="sh_sale")
    assert (sh.lawd_cd, sh.jibun, sh.land_area_m2, sh.house_type) == ("11440", "3**", 165.3, "다가구")
    assert [t.name for t in filled.query(name="hanbit")] == ["Hanbit Tower"]
    assert all(not t.is_cancelled for t in filled.query(include_cancelled=False))
    assert all(t.area_m2 >= 60 for t in filled.query(min_area=60))
    assert [t.deal_type for t in filled.query(first_seen_since=T1)] == [DealType.SH_SALE]


def test_count_get_and_get_many(filled, make_tx):
    assert filled.count() == 6
    assert filled.count(limit=2) == 2
    with pytest.raises(TypeError):
        filled.count(bogus=1)
    key = make_tx(deal_date=date(2025, 1, 27), name="푸른숲", dong="개포동", jibun="12-3", area_m2=59.96, price=98500).key
    assert filled.get(key).name == "푸른숲"
    assert filled.get("없는키") is None
    keys = [t.key for t in filled.query()]
    got = filled.get_many([keys[3], "없는키", keys[0], keys[3]])
    assert [t.key for t in got] == [keys[3], keys[0]]


def test_first_seen_since_accepts_naive_and_utc(filled):
    naive_kst = datetime(2025, 2, 1, 10, 0)  # KST 로 간주 = T1
    utc = datetime(2025, 2, 1, 1, 0, tzinfo=timezone.utc)  # = T1
    assert filled.count(first_seen_since=naive_kst) == filled.count(first_seen_since=utc) == 1


# ---------------------------------------------------------------------- 수집 기록


def test_fetch_log_and_last_fetched(store):
    assert store.last_fetched(APT, "11680", "202501") is None
    store.record_fetch(APT, "11680", "202501", item_count=3, fetched_at=T0)
    assert store.last_fetched(APT, "11680", "202501") == T0

    # 실패한 시도는 기록하지만 마지막 성공 시각은 그대로
    store.record_fetch("apt_sale", "11680", "2025-01", item_count=0, status="error", message="일시적 오류", fetched_at=T1)
    assert store.last_fetched(APT, "11680", "202501") == T0
    (entry,) = store.fetch_log()
    assert entry == FetchLogEntry(APT, "11680", "202501", T1, 0, "error", "일시적 오류")

    # 한 번도 성공하지 못한 파티션
    store.record_fetch(DealType.APT_RENT, "11680", "202501", item_count=0, status="error", fetched_at=T2)
    assert store.last_fetched(DealType.APT_RENT, "11680", "202501") is None

    store.record_fetch(APT, "11440", "202501", item_count=5, fetched_at=T3)
    log = store.fetch_log()
    assert [(e.deal_type, e.lawd_cd, e.fetched_at) for e in log] == [
        (APT, "11440", T3),
        (DealType.APT_RENT, "11680", T2),
        (APT, "11680", T1),
    ]
    assert [e.lawd_cd for e in store.fetch_log(status="ok")] == ["11440"]
    assert len(store.fetch_log(status="error")) == 2
    assert len(store.fetch_log(limit=1)) == 1

    # 다음 성공이 마지막 성공 시각을 갱신
    store.record_fetch(APT, "11680", "202501", item_count=4, fetched_at=T4)
    assert store.last_fetched(APT, "11680", "202501") == T4
    assert store.fetch_log()[0].status == "ok"


def test_summary(filled):
    rows = filled.summary()
    assert [(r["deal_type"], r["lawd_cd"]) for r in rows] == [
        (DealType.APT_SALE, "11440"),
        (DealType.APT_SALE, "11680"),
        (DealType.APT_RENT, "11680"),
        (DealType.SH_SALE, "11440"),
    ]
    apt = rows[1]
    assert apt == {"deal_type": APT, "lawd_cd": "11680", "count": 3, "cancelled": 1, "min_ym": "202412", "max_ym": "202501"}
    assert all(isinstance(r["deal_type"], DealType) for r in rows)


# ---------------------------------------------------------------------- 형식·지속성


def test_roundtrip_of_all_fixture_types(store, load_items):
    groups = [
        (DealType.APT_SALE, load_items("apt_trade"), "11680"),
        (DealType.APT_RENT, load_items("apt_rent"), "11680"),
        (DealType.OFFI_SALE, load_items("offi_trade"), "11680"),
        (DealType.RH_SALE, load_items("rh_trade"), "11440"),
        (DealType.SH_SALE, load_items("sh_trade"), "11440"),
        (DealType.APT_SALE, load_items("legacy_apt_trade"), "11110"),
    ]
    originals = []
    for deal_type, raws, lawd_cd in groups:
        txs = normalize_items(deal_type, raws, lawd_cd=lawd_cd)
        by_month: dict[str, list[Transaction]] = {}
        for tx in txs:
            by_month.setdefault(tx.deal_ym, []).append(tx)
        for ym, part in by_month.items():
            store.replace_partition(deal_type, lawd_cd, ym, part, fetched_at=T0)
        originals.extend(txs)

    assert store.count() == len(originals) == 10
    for tx in originals:
        stored = store.get(tx.key)
        assert stored is not None
        assert stored.key == tx.key
        assert _without_stamps(stored) == _without_stamps(tx)
        assert Transaction.from_dict(stored.to_dict()) == stored
        assert stored == tx  # 넘겨준 객체에도 같은 시각이 채워져 있다
    renewal = next(t for t in store.query(deal_types="apt_rent") if t.contract_type == "갱신")
    assert renewal.renewal_right_used is True and renewal.prev_deposit == 100000
    assert store.query(deal_types="apt_rent", name="푸른숲")[0].renewal_right_used is None


def test_timestamps_stored_as_kst_text(tmp_path, make_tx):
    path = tmp_path / "ts.db"
    with TransactionStore(path) as s:
        s.upsert([make_tx()], now=datetime(2025, 3, 1, 12, 0))  # tz 없음 → KST 로 간주
        s.upsert([make_tx(floor=1)], now=datetime(2025, 3, 1, 3, 0, tzinfo=timezone.utc))  # = 12:00 KST
        stored = s.query()
        assert {t.first_seen_at for t in stored} == {datetime(2025, 3, 1, 12, 0, tzinfo=KST)}
        assert all(t.first_seen_at.utcoffset() == timedelta(hours=9) for t in stored)
    with sqlite3.connect(path) as conn:
        texts = {row[0] for row in conn.execute("SELECT first_seen_at FROM transactions")}
    assert texts == {"2025-03-01T12:00:00.000000+09:00"}


def test_file_store_persists_and_creates_parent_dirs(tmp_path, make_tx):
    path = tmp_path / "nested" / "dir" / "silgeorae.db"
    with TransactionStore(path) as s:
        assert s.path == path
        s.replace_partition(APT, "11680", "202501", [make_tx(), make_tx(floor=3)], fetched_at=T0)
        s.record_fetch(DealType.APT_RENT, "11680", "202501", item_count=0, status="error", message="x", fetched_at=T1)
        before = s.query()
    assert path.exists()

    with TransactionStore(str(path)) as s:  # 다시 열기
        assert s.schema_version == 1
        after = s.query()
        assert after == before
        assert s.last_fetched(APT, "11680", "202501") == T0
        assert len(s.fetch_log()) == 2
        assert s.replace_partition(APT, "11680", "202501", [make_tx(), make_tx(floor=3)]).unchanged == 2


def test_schema(tmp_path):
    path = tmp_path / "schema.db"
    TransactionStore(path).close()
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        columns = [row[1] for row in conn.execute("PRAGMA table_info(transactions)")]
        pk = [row[1] for row in conn.execute("PRAGMA table_info(transactions)") if row[5]]
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        log_columns = [row[1] for row in conn.execute("PRAGMA table_info(fetch_log)")]
    assert {"meta", "transactions", "fetch_log"} <= tables
    assert {
        "ix_transactions_partition",
        "ix_transactions_deal_date",
        "ix_transactions_name",
        "ix_transactions_first_seen",
    } <= indexes
    assert pk == ["key"]
    assert set(columns) == {"key", "deal_ym"} | {f.name for f in fields(Transaction)}
    assert version == "1"
    assert log_columns == ["deal_type", "lawd_cd", "deal_ym", "fetched_at", "item_count", "status", "message", "last_ok_at"]


def test_storage_kinds_cover_every_transaction_field():
    # 모델에 필드가 추가되면 storage._KINDS 에도 추가해야 한다
    assert {f.name for f in fields(Transaction)} == set(storage_module._KINDS)


def test_missing_column_is_added_on_open(tmp_path, make_tx):
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("DROP COLUMN 은 SQLite 3.35 이상")
    path = tmp_path / "old.db"
    with TransactionStore(path) as s:
        s.upsert([make_tx(road_address="샘플로 51")])
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE transactions DROP COLUMN road_address")
    with TransactionStore(path) as s:
        (tx,) = s.query()
        assert tx.road_address == ""
        assert s.upsert([make_tx(road_address="샘플로 51")]).updated == 1


def test_newer_schema_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    TransactionStore(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(StorageError, match="새로운 형식"):
        TransactionStore(path)


def test_not_a_database_file(tmp_path):
    path = tmp_path / "garbage.db"
    path.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(StorageError):
        TransactionStore(path)
    with pytest.raises(StorageError, match="폴더"):
        TransactionStore(tmp_path)


def test_closed_store_raises(make_tx):
    with TransactionStore() as s:
        s.upsert([make_tx()])
    assert s.closed
    with pytest.raises(StorageError, match="닫혔"):
        s.query()
    s.close()  # 여러 번 닫아도 된다


def test_large_batch_crosses_chunk_boundaries(store, make_tx):
    txs = [make_tx(deal_date=date(2025, 1, 1 + i % 28), floor=i % 40, price=10000 + i) for i in range(2500)]
    result = store.replace_partition(APT, "11680", "202501", txs, fetched_at=T0)
    assert result.inserted == 2500 and store.count() == 2500
    again = [make_tx(deal_date=date(2025, 1, 1 + i % 28), floor=i % 40, price=10000 + i) for i in range(2500)]
    assert store.upsert(again, now=T1).unchanged == 2500
    keys = [t.key for t in txs]
    assert [t.key for t in store.get_many(keys)] == keys
    assert store.replace_partition(APT, "11680", "202501", again[:1000], fetched_at=T2).removed == 1500


def test_partition_result_merge():
    total = PartitionResult(inserted=1, new_keys=["a"])
    other = PartitionResult(inserted=2, updated=1, unchanged=3, removed=4, newly_cancelled=1, new_keys=["b", "c"], cancelled_keys=["d"])
    assert total.merge(other) is total
    assert total == PartitionResult(3, 1, 3, 4, 1, ["a", "b", "c"], ["d"])
    assert other.new_keys == ["b", "c"]  # 합친 쪽은 그대로


def test_store_usable_from_another_thread(store, make_tx):
    import threading

    store.upsert([make_tx()])
    counts: list[int] = []
    worker = threading.Thread(target=lambda: counts.append(store.count()))
    worker.start()
    worker.join()
    assert counts == [1]
