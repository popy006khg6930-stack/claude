from __future__ import annotations

from datetime import date, datetime

import pytest

from silgeorae.errors import ApiError, QuotaExceededError, ServiceKeyError
from silgeorae.models import DealType
from silgeorae.pipeline import CollectResult, CollectSummary, CollectTask, plan_tasks, run_collect
from silgeorae.processing import TransactionStore
from silgeorae.utils import KST, month_range

SALE, RENT = DealType.APT_SALE, DealType.APT_RENT
TODAY = date(2025, 3, 15)  # 이번 달 = 202503, 최근 3개월 = 202501~202503
MONTHS = month_range("202410", "202503")


@pytest.fixture
def store():
    with TransactionStore(":memory:") as s:
        yield s


@pytest.fixture
def data(make_sale, make_rent):
    """202410~202503 매매 2건씩, 전월세 1건씩 (강남구)."""
    result = {}
    for ym in MONTHS:
        result[(SALE, "11680", ym)] = [make_sale(ym, day=3), make_sale(ym, day=17, price="91,000", floor="12")]
        result[(RENT, "11680", ym)] = [make_rent(ym)]
    return result


def run(client, store, tasks, **kwargs):
    kwargs.setdefault("today", TODAY)
    return run_collect(client, store, tasks, **kwargs)


def test_plan_tasks_is_month_major_newest_first():
    class RegionLike:
        lawd_cd = "11440"

    tasks = plan_tasks([SALE, "apt_rent", SALE], ["11680", RegionLike(), "11680"], ["2025-01", "202502", "202501"])
    assert len(tasks) == 2 * 2 * 2
    assert tasks[0] == CollectTask(SALE, "11680", "202502")
    assert [t.deal_ym for t in tasks] == ["202502"] * 4 + ["202501"] * 4
    assert tasks[:4] == [
        CollectTask(SALE, "11680", "202502"),
        CollectTask(SALE, "11440", "202502"),
        CollectTask(RENT, "11680", "202502"),
        CollectTask(RENT, "11440", "202502"),
    ]


def test_first_run_fetches_everything(fake_client_cls, store, data):
    client = fake_client_cls(data)
    tasks = plan_tasks([SALE, RENT], ["11680"], MONTHS)
    summary = run(client, store, tasks)
    assert summary.planned == len(tasks) == 12
    assert (summary.ok, summary.skipped, summary.errors) == (12, 0, 0)
    assert summary.api_calls == 12
    assert summary.item_count == 18
    assert summary.inserted == 18 and len(summary.new_keys) == 18
    assert (summary.updated, summary.removed, summary.newly_cancelled) == (0, 0, 0)
    assert not summary.aborted
    assert client.calls[0] == (SALE, "11680", "202503")  # 최신 달부터
    assert store.count() == 18
    first = summary.results[0]
    assert first.status == "ok" and first.item_count == 2 and first.partition.inserted == 2


def test_second_run_skips_old_months_but_refreshes_recent(fake_client_cls, store, data):
    tasks = plan_tasks([SALE, RENT], ["11680"], MONTHS)
    run(fake_client_cls(data), store, tasks)
    client = fake_client_cls(data)
    summary = run(client, store, tasks, refresh_months=3)
    fetched_months = sorted({ym for _, _, ym in client.calls})
    assert fetched_months == ["202501", "202502", "202503"]
    assert (summary.ok, summary.skipped) == (6, 6)
    assert {r.note for r in summary.results if r.status == "skipped"} == {"이미 수집됨"}
    assert summary.inserted == 0 and summary.unchanged == 9
    assert summary.api_calls == 6


def test_refresh_months_one_only_refreshes_current_month(fake_client_cls, store, data):
    tasks = plan_tasks([SALE], ["11680"], MONTHS)
    run(fake_client_cls(data), store, tasks)
    client = fake_client_cls(data)
    run(client, store, tasks, refresh_months=1)
    assert client.calls == [(SALE, "11680", "202503")]


def test_force_refetches_everything(fake_client_cls, store, data):
    tasks = plan_tasks([SALE], ["11680"], MONTHS)
    run(fake_client_cls(data), store, tasks)
    client = fake_client_cls(data)
    summary = run(client, store, tasks, force=True)
    assert len(client.calls) == 6 and summary.skipped == 0


def test_future_months_are_skipped_without_calling_api(fake_client_cls, store, data):
    client = fake_client_cls(data)
    tasks = plan_tasks([SALE], ["11680"], month_range("202502", "202505"))
    summary = run(client, store, tasks)
    future = [r for r in summary.results if r.task.deal_ym > "202503"]
    assert [r.status for r in future] == ["skipped", "skipped"]
    assert {r.note for r in future} == {"미래 연월"}
    assert sorted(ym for _, _, ym in client.calls) == ["202502", "202503"]


def test_changes_between_runs_are_reported(fake_client_cls, store, make_sale):
    ym = "202503"
    first = [make_sale(ym, day=3), make_sale(ym, day=5, floor="9"), make_sale(ym, day=9, floor="2")]
    tasks = plan_tasks([SALE], ["11680"], [ym])
    run(fake_client_cls({(SALE, "11680", ym): first}), store, tasks)

    second = [
        make_sale(ym, day=3, cancel_day="25.03.20"),  # 해제
        make_sale(ym, day=5, floor="9", rgstDate="25.03.25"),  # 등기일자 반영 → 변경
        make_sale(ym, day=11, floor="15"),  # 신규 (day 9 거래는 사라짐 → 삭제)
    ]
    summary = run(fake_client_cls({(SALE, "11680", ym): second}), store, tasks)
    assert summary.inserted == 1
    assert summary.removed == 1
    assert summary.newly_cancelled == 1
    assert summary.updated == 2
    assert len(summary.cancelled_keys) == 1
    cancelled = store.get(summary.cancelled_keys[0])
    assert cancelled.is_cancelled and cancelled.deal_date == date(2025, 3, 3)
    new = store.get(summary.new_keys[0])
    assert new.floor == 15


def test_service_key_error_aborts_and_is_recorded(fake_client_cls, store, data):
    tasks = plan_tasks([SALE, RENT], ["11680"], MONTHS)
    assert tasks[1] == CollectTask(RENT, "11680", "202503")
    client = fake_client_cls(data, errors={(RENT, "11680", "202503"): ServiceKeyError("인증키 오류")})
    summary = run(client, store, tasks)
    assert summary.aborted and summary.abort_kind == "service_key"
    assert "인증키 오류" in summary.abort_reason
    assert len(client.calls) == 2  # 남은 작업은 시도하지 않음
    assert len(summary.results) == 2 and summary.remaining == 10
    assert [r.status for r in summary.results] == ["ok", "error"]
    errors = store.fetch_log(status="error")
    assert len(errors) == 1 and errors[0].deal_type is RENT and "인증키" in errors[0].message
    assert store.last_fetched(RENT, "11680", "202503") is None


def test_quota_error_aborts(fake_client_cls, store, data):
    tasks = plan_tasks([SALE], ["11680"], MONTHS)
    client = fake_client_cls(data, errors={(SALE, "11680", "202503"): QuotaExceededError("한도 초과")})
    summary = run(client, store, tasks)
    assert summary.aborted and summary.abort_kind == "quota"
    assert len(client.calls) == 1 and summary.remaining == 5
    assert summary.errors == 1 and summary.ok == 0


def test_other_api_errors_are_recorded_and_collection_continues(fake_client_cls, store, data):
    tasks = plan_tasks([SALE], ["11680"], MONTHS)
    client = fake_client_cls(data, errors={(SALE, "11680", "202502"): ApiError("일시적 오류", retryable=True)})
    summary = run(client, store, tasks)
    assert not summary.aborted
    assert (summary.ok, summary.errors) == (5, 1)
    assert len(client.calls) == 6
    failed = summary.error_results[0]
    assert failed.task.deal_ym == "202502" and "일시적 오류" in failed.error
    assert store.last_fetched(SALE, "11680", "202502") is None  # 다음 실행 때 다시 받는다
    assert store.fetch_log(status="error")[0].deal_ym == "202502"

    retry_client = fake_client_cls(data)
    run(retry_client, store, tasks, refresh_months=1)
    assert (SALE, "11680", "202502") in retry_client.calls


def test_progress_callback_sees_every_task(fake_client_cls, store, data):
    tasks = plan_tasks([SALE], ["11680"], month_range("202502", "202504"))
    seen = []
    run(fake_client_cls(data), store, tasks, progress=lambda i, total, r: seen.append((i, total, r.status)))
    assert seen == [(1, 3, "skipped"), (2, 3, "ok"), (3, 3, "ok")]


def test_sigungu_is_filled_from_region_table(fake_client_cls, store, data):
    from silgeorae.regions import RegionTable

    run(fake_client_cls(data), store, plan_tasks([SALE], ["11680"], ["202503"]), regions=RegionTable.load())
    assert {tx.sigungu for tx in store.query()} == {"강남구"}


def test_invalid_records_are_skipped_and_counted(fake_client_cls, store, make_sale, caplog):
    ym = "202503"
    broken = make_sale(ym, day=5)
    del broken["dealYear"]
    client = fake_client_cls({(SALE, "11680", ym): [make_sale(ym), broken]})
    with caplog.at_level("WARNING", logger="silgeorae.pipeline"):
        summary = run(client, store, plan_tasks([SALE], ["11680"], [ym]))
    assert summary.ok == 1 and summary.item_count == 1 and summary.invalid == 1
    assert summary.results[0].invalid == 1
    assert "1건" in caplog.text


def test_now_is_used_as_fetch_and_first_seen_time(fake_client_cls, store, data):
    when = datetime(2025, 3, 1, 9, 30, tzinfo=KST)
    run(fake_client_cls(data), store, plan_tasks([SALE], ["11680"], ["202503"]), now=when)
    assert store.last_fetched(SALE, "11680", "202503") == when
    assert {tx.first_seen_at for tx in store.query()} == {when}


def test_empty_summary_totals():
    summary = CollectSummary()
    assert (summary.ok, summary.skipped, summary.errors, summary.inserted, summary.updated) == (0, 0, 0, 0, 0)
    assert (summary.removed, summary.newly_cancelled, summary.remaining) == (0, 0, 0)
    assert summary.new_keys == [] and summary.cancelled_keys == []
    result = CollectResult(CollectTask(SALE, "11680", "202501"), "skipped")
    assert result.partition is None and result.item_count == 0


def test_api_calls_without_request_count(store):
    class Bare:
        def fetch(self, deal_type, lawd_cd, deal_ym):
            return []

    summary = run(Bare(), store, plan_tasks([SALE], ["11680"], ["202503"]))
    assert summary.ok == 1 and summary.api_calls == 0


# ---------------------------------------------------------------- 비정상 응답 안전장치
def _many_sales(make_sale, ym: str, n: int) -> list[dict[str, str]]:
    return [make_sale(ym, day=1 + i % 28, floor=str(1 + i // 28), price=f"{80_000 + i * 100:,}") for i in range(n)]


def test_empty_response_keeps_existing_rows(fake_client_cls, store, make_sale):
    tasks = [CollectTask(SALE, "11680", "202503")]
    run(fake_client_cls({(SALE, "11680", "202503"): _many_sales(make_sale, "202503", 6)}), store, tasks)
    assert store.count() == 6
    first_ok = store.last_fetched(SALE, "11680", "202503")

    summary = run(fake_client_cls({(SALE, "11680", "202503"): []}), store, tasks)
    result = summary.results[0]
    assert result.status == "error" and "기존 6건" in result.error
    assert summary.removed == 0 and store.count() == 6
    assert store.last_fetched(SALE, "11680", "202503") == first_ok  # 성공 기록은 그대로
    assert store.fetch_log(limit=1)[0].status == "error"


def test_tiny_partition_may_legitimately_become_empty(fake_client_cls, store, make_sale):
    tasks = [CollectTask(SALE, "11680", "202503")]
    run(fake_client_cls({(SALE, "11680", "202503"): _many_sales(make_sale, "202503", 2)}), store, tasks)
    summary = run(fake_client_cls({(SALE, "11680", "202503"): []}), store, tasks)
    assert summary.results[0].status == "ok" and summary.removed == 2 and store.count() == 0


def test_sharp_shrink_merges_without_deleting(fake_client_cls, store, make_sale):
    tasks = [CollectTask(SALE, "11680", "202503")]
    items = _many_sales(make_sale, "202503", 30)
    run(fake_client_cls({(SALE, "11680", "202503"): items}), store, tasks)

    partial = [dict(item) for item in items[:10]]
    partial[0].update(cdealType="O", cdealDay="25.03.20")  # 받은 일부에는 변경도 반영된다
    summary = run(fake_client_cls({(SALE, "11680", "202503"): partial}), store, tasks)
    result = summary.results[0]
    assert result.status == "ok" and "삭제 없이 병합" in result.note
    assert summary.removed == 0 and store.count() == 30
    assert summary.newly_cancelled == 1 and summary.unchanged == 9
    assert store.fetch_log(limit=1)[0].status == "ok"
