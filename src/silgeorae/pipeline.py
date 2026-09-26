"""수집 파이프라인 — (유형, 시군구, 연월) 작업을 만들고 API → 정규화 → 저장을 실행한다.

* 작업 순서는 **최신 달부터** (중간에 멈춰도 새 데이터가 먼저 들어가도록).
* 과거 달은 한 번 받으면 건너뛰고, 최근 ``refresh_months`` 개월은 매번 다시 받는다
  (신고 기한 30일·해제·등기 반영, ``docs/ARCHITECTURE.md`` §6). ``force=True`` 면 모두 다시 받는다.
* 인증키 오류·일일 한도 초과·저장소 오류는 즉시 중단하고, 그 밖의 API 오류는 기록 후 계속한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Optional

from .errors import QuotaExceededError, ServiceKeyError, SilgeoraeError, StorageError
from .models import DealType
from .utils import parse_ym, today_kst, ym_add, ym_of

if TYPE_CHECKING:  # 실행 시에는 불러오지 않는다 (다른 팀 모듈에 문제가 있어도 CLI 도움말은 동작)
    from .processing import PartitionResult
    from .regions import RegionTable

log = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

SKIP_FUTURE = "미래 연월"
SKIP_DONE = "이미 수집됨"

# 비정상 응답으로부터 저장된 거래를 지키는 기준 (§6 파티션 교체의 안전장치)
GUARD_MIN_EXISTING = 5  # 기존 거래가 이 이상인데 응답이 0건이면 교체하지 않는다
GUARD_MIN_SHRINK_BASE = 20  # 기존 거래가 이 이상이고
GUARD_SHRINK_RATIO = 0.5  # 응답이 기존의 이 비율 미만이면 삭제 없이 병합만 한다

ABORT_SERVICE_KEY = "service_key"
ABORT_QUOTA = "quota"
ABORT_STORAGE = "storage"


@dataclass(frozen=True)
class CollectTask:
    """수집 작업 1건 = API 조회 단위 (유형, 시군구 코드, 계약연월 ``YYYYMM``)."""

    deal_type: DealType
    lawd_cd: str
    deal_ym: str


@dataclass
class CollectResult:
    """작업 1건의 결과. ``status`` 는 ``"ok"`` / ``"skipped"`` / ``"error"``."""

    task: CollectTask
    status: str
    item_count: int = 0  # 저장한 거래 수 (정규화 성공분)
    partition: Optional["PartitionResult"] = None  # 저장소 반영 결과 (신규·변경·삭제·해제)
    error: str = ""  # 오류 메시지
    note: str = ""  # 건너뛴 이유 ("미래 연월", "이미 수집됨")
    invalid: int = 0  # 해석하지 못해 건너뛴 원본 레코드 수


@dataclass
class CollectSummary:
    """수집 실행 전체 결과와 합계."""

    results: list[CollectResult] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""
    api_calls: int = 0  # 이번 실행의 HTTP 호출 수
    planned: int = 0  # 계획된 작업 수 (중단되면 len(results) 보다 크다)
    abort_kind: str = ""  # "service_key" | "quota" | "storage"

    def _count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    def _sum(self, name: str) -> int:
        return sum(int(getattr(r.partition, name, 0) or 0) for r in self.results if r.partition is not None)

    @property
    def ok(self) -> int:
        return self._count(STATUS_OK)

    @property
    def skipped(self) -> int:
        return self._count(STATUS_SKIPPED)

    @property
    def errors(self) -> int:
        return self._count(STATUS_ERROR)

    @property
    def remaining(self) -> int:
        """중단으로 시도하지 못한 작업 수."""
        return max(0, self.planned - len(self.results))

    @property
    def item_count(self) -> int:
        return sum(r.item_count for r in self.results)

    @property
    def invalid(self) -> int:
        return sum(r.invalid for r in self.results)

    @property
    def inserted(self) -> int:
        return self._sum("inserted")

    @property
    def updated(self) -> int:
        return self._sum("updated")

    @property
    def unchanged(self) -> int:
        return self._sum("unchanged")

    @property
    def removed(self) -> int:
        return self._sum("removed")

    @property
    def newly_cancelled(self) -> int:
        return self._sum("newly_cancelled")

    @property
    def new_keys(self) -> list[str]:
        keys: list[str] = []
        for r in self.results:
            if r.partition is not None:
                keys.extend(r.partition.new_keys)
        return keys

    @property
    def cancelled_keys(self) -> list[str]:
        keys: list[str] = []
        for r in self.results:
            if r.partition is not None:
                keys.extend(r.partition.cancelled_keys)
        return keys

    @property
    def error_results(self) -> list[CollectResult]:
        return [r for r in self.results if r.status == STATUS_ERROR]


ProgressCallback = Callable[[int, int, CollectResult], None]


def plan_tasks(
    deal_types: Iterable[DealType | str],
    lawd_cds: Iterable[str],
    months: Iterable[str],
) -> list[CollectTask]:
    """작업 목록을 만든다. 순서: 최신 달부터 → 유형 → 지역 (중복은 한 번만)."""
    types: list[DealType] = []
    for value in deal_types:
        dt = value if isinstance(value, DealType) else DealType.parse(str(value))
        if dt not in types:
            types.append(dt)
    codes: list[str] = []
    for value in lawd_cds:
        code = str(getattr(value, "lawd_cd", value)).strip()  # Region 객체도 허용
        if code and code not in codes:
            codes.append(code)
    yms = sorted({parse_ym(m) for m in months}, reverse=True)
    return [CollectTask(dt, code, ym) for ym in yms for dt in types for code in codes]


def run_collect(
    client: Any,
    store: Any,
    tasks: Iterable[CollectTask],
    *,
    regions: Optional["RegionTable"] = None,
    refresh_months: int = 3,
    force: bool = False,
    today: Optional[date] = None,
    progress: Optional[ProgressCallback] = None,
    now: Optional[datetime] = None,
) -> CollectSummary:
    """작업을 차례로 실행한다.

    ``client`` 는 ``MolitClient`` (``fetch``, ``request_count``), ``store`` 는 ``TransactionStore``.
    ``regions`` 가 있으면 시군구 이름을 거래에 채운다. ``now`` 를 주면 저장소의 수집 시각으로 쓴다
    (데모에서 "일주일 전 수집"을 흉내 낼 때 사용).
    ``progress(i, total, result)`` 는 작업마다(건너뜀 포함) 호출된다.
    """
    from .processing import normalize_items  # 정제·저장팀 모듈은 필요할 때 불러온다

    task_list = list(tasks)
    total = len(task_list)
    current_ym = ym_of(today or today_kst())
    refresh_cutoff = ym_add(current_ym, -(max(refresh_months, 0) - 1))
    summary = CollectSummary(planned=total)
    calls_before = _request_count(client)
    try:
        for index, task in enumerate(task_list, 1):
            result = _run_task(
                client,
                store,
                task,
                normalize_items=normalize_items,
                regions=regions,
                current_ym=current_ym,
                refresh_cutoff=refresh_cutoff,
                force=force,
                now=now,
                summary=summary,
            )
            summary.results.append(result)
            if progress is not None:
                progress(index, total, result)
            if summary.aborted:
                log.info("수집 중단: %s (남은 작업 %d건)", summary.abort_reason, summary.remaining)
                break
    finally:
        summary.api_calls = max(0, _request_count(client) - calls_before)
    return summary


def _run_task(
    client: Any,
    store: Any,
    task: CollectTask,
    *,
    normalize_items: Callable[..., list],
    regions: Any,
    current_ym: str,
    refresh_cutoff: str,
    force: bool,
    now: Optional[datetime],
    summary: CollectSummary,
) -> CollectResult:
    if task.deal_ym > current_ym:
        return CollectResult(task, STATUS_SKIPPED, note=SKIP_FUTURE)
    invalid: list[str] = []

    def on_error(raw: Mapping[str, Any], exc: Exception) -> None:
        invalid.append(str(exc))
        log.debug("%s %s %s 레코드 해석 실패: %s (%r)", task.deal_type.label, task.lawd_cd, task.deal_ym, exc, raw)

    try:
        if not force and task.deal_ym < refresh_cutoff:
            if store.last_fetched(task.deal_type, task.lawd_cd, task.deal_ym) is not None:
                return CollectResult(task, STATUS_SKIPPED, note=SKIP_DONE)
        raws = client.fetch(task.deal_type, task.lawd_cd, task.deal_ym)
        transactions = normalize_items(
            task.deal_type,
            raws,
            lawd_cd=task.lawd_cd,
            sigungu=_sigungu_name(regions, task.lawd_cd),
            on_error=on_error,
        )
        existing = store.count(
            deal_types=[task.deal_type], lawd_cds=[task.lawd_cd], start_ym=task.deal_ym, end_ym=task.deal_ym
        )
        if existing >= GUARD_MIN_EXISTING and not transactions:
            # 포털이 가끔 일시적으로 빈 응답을 준다. 그대로 교체하면 저장된 거래가 모두 지워지고,
            # 다음 수집 때 '신규'로 다시 잡혀 알림이 쏟아지므로 기존 데이터를 유지한다.
            message = f"응답이 0건이라 기존 {existing:,}건을 그대로 두었습니다 (일시적 오류일 수 있어 다음 실행 때 다시 확인)"
            log.info("%s %s %s: %s", task.deal_type.label, task.lawd_cd, task.deal_ym, message)
            _record_error(store, task, SilgeoraeError(message), now)
            return CollectResult(task, STATUS_ERROR, error=message, invalid=len(invalid))
        fetched_at = {} if now is None else {"fetched_at": now}
        if existing >= GUARD_MIN_SHRINK_BASE and len(transactions) < existing * GUARD_SHRINK_RATIO:
            # 응답이 비정상적으로 줄었으면(부분 응답·형식 변경 등) 삭제 없이 병합만 한다.
            note = f"응답 {len(transactions):,}건이 기존 {existing:,}건보다 크게 적어 삭제 없이 병합했습니다"
            log.info("%s %s %s: %s", task.deal_type.label, task.lawd_cd, task.deal_ym, note)
            partition = store.upsert(transactions, now=now)
            store.record_fetch(
                task.deal_type, task.lawd_cd, task.deal_ym, item_count=len(transactions), message=note, **fetched_at
            )
            return CollectResult(
                task, STATUS_OK, item_count=len(transactions), partition=partition, note=note, invalid=len(invalid)
            )
        partition = store.replace_partition(task.deal_type, task.lawd_cd, task.deal_ym, transactions, **fetched_at)
    except (ServiceKeyError, QuotaExceededError, StorageError) as exc:
        _record_error(store, task, exc, now)
        summary.aborted = True
        summary.abort_reason = str(exc)
        if isinstance(exc, ServiceKeyError):
            summary.abort_kind = ABORT_SERVICE_KEY
        elif isinstance(exc, QuotaExceededError):
            summary.abort_kind = ABORT_QUOTA
        else:
            summary.abort_kind = ABORT_STORAGE
        return CollectResult(task, STATUS_ERROR, error=str(exc))
    except SilgeoraeError as exc:  # 일시적 API 오류 등 — 기록하고 다음 작업으로
        log.info("%s %s %s 수집 실패: %s", task.deal_type.label, task.lawd_cd, task.deal_ym, exc)
        _record_error(store, task, exc, now)
        return CollectResult(task, STATUS_ERROR, error=str(exc))

    if invalid:
        log.warning(
            "%s %s %s: 해석하지 못한 레코드 %d건을 건너뛰었습니다 (예: %s)",
            task.deal_type.label,
            task.lawd_cd,
            task.deal_ym,
            len(invalid),
            invalid[0],
        )
    return CollectResult(
        task, STATUS_OK, item_count=len(transactions), partition=partition, invalid=len(invalid)
    )


def _record_error(store: Any, task: CollectTask, exc: Exception, now: Optional[datetime]) -> None:
    try:
        store.record_fetch(
            task.deal_type,
            task.lawd_cd,
            task.deal_ym,
            item_count=0,
            status=STATUS_ERROR,
            message=str(exc)[:500],
            fetched_at=now,
        )
    except Exception:  # 기록 실패가 수집 흐름을 막지 않도록
        log.warning("수집 오류를 저장소에 기록하지 못했습니다", exc_info=True)


def _sigungu_name(regions: Any, lawd_cd: str) -> str:
    if regions is None:
        return ""
    try:
        region = regions.get(lawd_cd)
    except Exception:  # pragma: no cover - 지역표 문제는 이름만 비운다
        return ""
    return str(getattr(region, "short_name", "") or "") if region is not None else ""


def _request_count(client: Any) -> int:
    try:
        return int(getattr(client, "request_count", 0) or 0)
    except (TypeError, ValueError):
        return 0
