"""SQLite 저장소 — 파티션 교체·변경 감지·조회 (ARCHITECTURE.md §5.3, §6).

테이블

* ``transactions`` — 거래 1건 = 1행, 기본키 ``key`` (= ``Transaction.key``).
  ``Transaction`` 필드마다 열 1개 + ``deal_ym``. ``first_seen_at``·``updated_at`` 은 저장소가 채운다.
* ``fetch_log`` — 파티션(유형·시군구·계약연월)마다 1행: 마지막 수집 시도와 마지막 성공 시각(``last_ok_at``).
* ``meta`` — ``schema_version`` 등.

시각은 한국 시간 ISO 문자열(마이크로초까지 고정 길이)로 저장해 문자열 비교가 곧 시간 비교가 되게 한다.
tz 없는 datetime 은 한국 시간으로 보고, 읽을 때는 tz 가 있는(KST) datetime 을 돌려준다.

``replace_partition``·``upsert`` 는 넘겨받은 ``Transaction`` 을 제자리에서 손본다:
키가 겹치면 ``assign_seq`` 적용, 키에 들어가는 숫자·날짜 형식 통일(면적 85 → 85.0 등),
저장이 끝나면 ``first_seen_at``·``updated_at`` 을 저장소 값으로 채운다.
"""

from __future__ import annotations

import json
import logging
import math
import operator
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..errors import StorageError
from ..models import DealType, Transaction, assign_seq
from ..utils import KST, now_kst, parse_ym, ym_of
from .fields import coerce_deal_type, parse_float, parse_int, parse_short_date

__all__ = ["TransactionStore", "PartitionResult", "FetchLogEntry", "SCHEMA_VERSION"]

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
"""저장소 형식 버전 (``meta`` 테이블 ``schema_version``)."""

_CHUNK = 500  # IN (...) 한 번에 넣는 키 수 (오래된 SQLite 의 변수 한도 999 이하)
_BUSY_TIMEOUT = 30.0  # 다른 프로세스가 쓰는 중일 때 기다리는 시간(초)
_TIMESTAMP_FIELDS = ("first_seen_at", "updated_at")
_DEAL_TYPES = {member.value: member for member in DealType}
_DEAL_TYPE_ORDER = {member: index for index, member in enumerate(DealType)}


# ====================================================================== 결과 타입


@dataclass
class PartitionResult:
    """``replace_partition``·``upsert`` 결과."""

    inserted: int = 0  # 처음 보는 키 → 추가 ("신규 거래")
    updated: int = 0  # 값이 바뀐 기존 키
    unchanged: int = 0  # 값이 그대로인 기존 키
    removed: int = 0  # 새 목록에 없어 지운 키 (replace_partition 만)
    newly_cancelled: int = 0  # 해제여부가 False → True 로 바뀐 건수
    new_keys: list[str] = field(default_factory=list)
    cancelled_keys: list[str] = field(default_factory=list)

    def merge(self, other: PartitionResult) -> PartitionResult:
        """``other`` 를 더한다 (제자리 합산, ``self`` 반환)."""
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.removed += other.removed
        self.newly_cancelled += other.newly_cancelled
        self.new_keys.extend(other.new_keys)
        self.cancelled_keys.extend(other.cancelled_keys)
        return self


@dataclass
class FetchLogEntry:
    """파티션별 마지막 수집 시도 기록 (``fetch_log`` 1행)."""

    deal_type: DealType
    lawd_cd: str
    deal_ym: str
    fetched_at: datetime
    item_count: int
    status: str  # "ok" | "error" 등 (호출자가 정함, "ok" 만 성공으로 본다)
    message: str


# ====================================================================== 값 변환 (파이썬 ↔ SQLite)


def _as_date(value: Any) -> date:
    parsed = parse_short_date(value)  # date·datetime·"2025-01-03"·"25.01.03" 등
    if parsed is None:
        raise ValueError("날짜가 아닙니다")
    return parsed


def _enc_text(value: Any) -> str:
    if type(value) is str:
        return value
    return "" if value is None else str(value)


def _enc_int(value: Any) -> Any:
    if value is None or type(value) is int:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("유한한 숫자가 아닙니다")
        return int(value) if value.is_integer() else value  # 소수는 REAL 로 저장되어 그대로 돌아온다
    if isinstance(value, int):  # bool 등 int 하위 형
        return int(value)
    parsed = parse_int(value)
    if parsed is None:
        raise ValueError("정수가 아닙니다")
    return parsed


def _enc_real(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) is float and math.isfinite(value):
        return value
    parsed = parse_float(value)
    if parsed is None:
        raise ValueError("숫자가 아닙니다")
    return parsed


def _enc_bool(value: Any) -> int:
    return 1 if value else 0


def _enc_optbool(value: Any) -> int | None:
    return None if value is None else (1 if value else 0)


def _enc_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return _as_date(value).isoformat()


def _enc_enum(value: Any) -> str:
    return coerce_deal_type(value).value


def _enc_json(value: Any) -> str:
    if not value:
        return "{}"
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _enc_any(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


@lru_cache(maxsize=16384)
def _dec_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _dec_enum(value: str) -> DealType:
    try:
        return _DEAL_TYPES[value]
    except KeyError:
        raise StorageError(f"저장소에 알 수 없는 거래 유형 코드가 있습니다: {value!r}") from None


@lru_cache(maxsize=4096)
def _json_items(text: str) -> tuple[tuple[str, Any], ...]:
    data = json.loads(text)
    return tuple(data.items()) if isinstance(data, dict) else ()


def _dec_json(value: str | None) -> dict[str, Any]:
    if not value or value == "{}":
        return {}
    return dict(_json_items(value))  # 같은 문자열은 한 번만 해석하고, 거래마다 새 dict


def _dec_any(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _to_kst(value: datetime | date | str) -> datetime:
    """시각 → KST datetime (tz 없는 값은 KST 로 본다, date 는 그날 0시)."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip())
    elif not isinstance(value, datetime):
        if not isinstance(value, date):
            raise TypeError(f"시각은 datetime 이어야 합니다: {value!r}")
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=KST)
    return value.astimezone(KST)


def _ts_text(value: datetime | date | str) -> str:
    """저장용 시각 문자열 (KST, 마이크로초까지 고정 길이 → 문자열 비교 = 시간 비교)."""
    return _to_kst(value).isoformat(timespec="microseconds")


@lru_cache(maxsize=4096)
def _ts_value(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text).astimezone(KST)


# 필드 종류 → (저장 변환, 열 정의). 읽기 변환은 _decode_row (자주 쓰는 종류는 그 안에서 바로 처리).
_CODECS: dict[str, tuple[Callable[[Any], Any], str]] = {
    "enum": (_enc_enum, "TEXT NOT NULL"),
    "text": (_enc_text, "TEXT NOT NULL DEFAULT ''"),
    "int": (_enc_int, "INTEGER"),
    "real": (_enc_real, "REAL"),
    "bool": (_enc_bool, "INTEGER NOT NULL DEFAULT 0"),
    "optbool": (_enc_optbool, "INTEGER"),
    "date": (_enc_date, "TEXT"),
    "json": (_enc_json, "TEXT NOT NULL DEFAULT '{}'"),
    "timestamp": (_ts_text, "TEXT"),
    "any": (_enc_any, "TEXT"),  # 이 표에 없는 새 필드 (JSON)
}
_DECODERS: dict[str, Callable[[Any], Any]] = {
    "enum": _dec_enum,
    "json": _dec_json,
    "timestamp": _ts_value,
    "any": _dec_any,
}

# Transaction 필드 → 종류. 모델에 필드를 추가하면 여기에도 추가한다 (없으면 JSON 으로 저장).
_KINDS: dict[str, str] = {
    "deal_type": "enum",
    "lawd_cd": "text",
    "deal_date": "date",
    "dong": "text",
    "jibun": "text",
    "name": "text",
    "sigungu": "text",
    "floor": "int",
    "area_m2": "real",
    "land_area_m2": "real",
    "build_year": "int",
    "price": "int",
    "deposit": "int",
    "monthly_rent": "int",
    "is_cancelled": "bool",
    "cancel_date": "date",
    "deal_method": "text",
    "agent_location": "text",
    "registration_date": "date",
    "seller": "text",
    "buyer": "text",
    "building_dong": "text",
    "house_type": "text",
    "contract_type": "text",
    "contract_term": "text",
    "renewal_right_used": "optbool",
    "prev_deposit": "int",
    "prev_monthly_rent": "int",
    "complex_id": "text",
    "road_address": "text",
    "extra": "json",
    "seq": "int",
    "first_seen_at": "timestamp",
    "updated_at": "timestamp",
}

_DDL_OVERRIDES = {
    "lawd_cd": "TEXT NOT NULL",
    "deal_date": "TEXT NOT NULL",
    "seq": "INTEGER NOT NULL DEFAULT 0",
    "first_seen_at": "TEXT NOT NULL",
    "updated_at": "TEXT NOT NULL",
}


def _field_kinds() -> dict[str, str]:
    kinds: dict[str, str] = {}
    for f in fields(Transaction):
        kind = _KINDS.get(f.name)
        if kind is None:
            logger.warning("저장소가 모르는 Transaction 필드 %r 는 JSON 으로 저장합니다 (storage._KINDS 에 추가 필요).", f.name)
            kind = "any"
        kinds[f.name] = kind
    return kinds


_FIELD_KINDS = _field_kinds()


def _column_ddl(name: str) -> str:
    return _DDL_OVERRIDES.get(name) or _CODECS[_FIELD_KINDS[name]][1]


_MODEL_FIELDS: tuple[str, ...] = tuple(_FIELD_KINDS)
_VALUE_FIELDS: tuple[str, ...] = tuple(name for name in _MODEL_FIELDS if name not in _TIMESTAMP_FIELDS)
_VALUE_COLUMNS: tuple[str, ...] = _VALUE_FIELDS + ("deal_ym",)  # 변경 비교 대상 (시각 제외)
_CANCEL_INDEX = _VALUE_COLUMNS.index("is_cancelled")
_DEAL_DATE_INDEX = _VALUE_FIELDS.index("deal_date")

# 쓰기: 필드 값을 한 번에 꺼낸 뒤, 형식이 이미 맞는 흔한 경우는 변환 함수를 부르지 않는다 (10만 건 대비).
_GET_VALUES = operator.attrgetter(*_VALUE_FIELDS)
_ENCODERS = tuple((name, _CODECS[_FIELD_KINDS[name]][0]) for name in _VALUE_FIELDS)  # 느린 경로·오류 위치 찾기


def _indexes(names: Sequence[str], *kinds: str) -> tuple[int, ...]:
    return tuple(i for i, name in enumerate(names) if _FIELD_KINDS[name] in kinds)


_ENC_TEXT = _indexes(_VALUE_FIELDS, "text")
_ENC_INT = _indexes(_VALUE_FIELDS, "int")
_ENC_REAL = _indexes(_VALUE_FIELDS, "real")
_ENC_DATE = _indexes(_VALUE_FIELDS, "date")
_ENC_BOOL = _indexes(_VALUE_FIELDS, "bool")
_ENC_OPTBOOL = _indexes(_VALUE_FIELDS, "optbool")
_ENC_OTHER = tuple((i, _ENCODERS[i][1]) for i in _indexes(_VALUE_FIELDS, "enum", "json", "timestamp", "any"))


# 읽기: key + 모델 필드 순서 그대로 (문자열 열의 NULL 은 SQL 에서 "" 로)
def _read_expr(name: str) -> str:
    kind = _FIELD_KINDS[name]
    if kind == "text":
        return f"COALESCE({name}, '')"
    if kind == "json":
        return f"COALESCE({name}, '{{}}')"
    if kind == "bool":
        return f"COALESCE({name}, 0)"
    return name


_READ_SELECT = ", ".join(["key"] + [_read_expr(name) for name in _MODEL_FIELDS])
_DEC_BOOL = _indexes(_MODEL_FIELDS, "bool")
_DEC_OPTBOOL = _indexes(_MODEL_FIELDS, "optbool")
_DEC_DATE = _indexes(_MODEL_FIELDS, "date")
_DEC_REAL = _indexes(_MODEL_FIELDS, "real")
_DEC_OTHER = tuple((i, _DECODERS[_FIELD_KINDS[_MODEL_FIELDS[i]]]) for i in _indexes(_MODEL_FIELDS, *_DECODERS))

# 병합용 SELECT: key, first_seen_at, updated_at, 값 열들 (저장된 그대로 비교)
_EXISTING_SELECT = ", ".join(("key",) + _TIMESTAMP_FIELDS + _VALUE_COLUMNS)
_EXISTING_OFFSET = 1 + len(_TIMESTAMP_FIELDS)


def _marks(count: int) -> str:
    return ", ".join("?" * count)


_INSERT_SQL = (
    f"INSERT INTO transactions (key, {', '.join(_VALUE_COLUMNS)}, first_seen_at, updated_at) "
    f"VALUES ({_marks(len(_VALUE_COLUMNS) + 3)})"
)
_UPDATE_SQL = f"UPDATE transactions SET {', '.join(f'{c} = ?' for c in _VALUE_COLUMNS)}, updated_at = ? WHERE key = ?"
_DELETE_SQL = "DELETE FROM transactions WHERE key = ?"


def _create_transactions_sql() -> str:
    columns = ["key TEXT PRIMARY KEY"]
    for name in _VALUE_FIELDS:
        columns.append(f"{name} {_column_ddl(name)}")
        if name == "deal_date":
            columns.append("deal_ym TEXT NOT NULL")
    columns += [f"{name} {_column_ddl(name)}" for name in _TIMESTAMP_FIELDS]
    body = ",\n    ".join(columns)
    return f"CREATE TABLE IF NOT EXISTS transactions (\n    {body}\n)"


_CREATE_META = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
_CREATE_TRANSACTIONS = _create_transactions_sql()
_CREATE_FETCH_LOG = """CREATE TABLE IF NOT EXISTS fetch_log (
    deal_type TEXT NOT NULL,
    lawd_cd TEXT NOT NULL,
    deal_ym TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    last_ok_at TEXT,
    PRIMARY KEY (deal_type, lawd_cd, deal_ym)
)"""
_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_transactions_partition ON transactions (deal_type, lawd_cd, deal_ym)",
    "CREATE INDEX IF NOT EXISTS ix_transactions_deal_date ON transactions (deal_date)",
    "CREATE INDEX IF NOT EXISTS ix_transactions_name ON transactions (name)",
    "CREATE INDEX IF NOT EXISTS ix_transactions_first_seen ON transactions (first_seen_at)",
    "CREATE INDEX IF NOT EXISTS ix_fetch_log_fetched_at ON fetch_log (fetched_at)",
)


def _addable_ddl(ddl: str) -> str:
    """ALTER TABLE ADD COLUMN 용 정의 (기본값 없는 NOT NULL 은 추가할 수 없어 뺀다)."""
    if "NOT NULL" in ddl and "DEFAULT" not in ddl:
        return ddl.replace(" NOT NULL", "")
    return ddl


# ====================================================================== 입력 정리


# Transaction.natural_key() 에 들어가는 필드 (형식이 어긋나면 저장 전후 키가 달라진다)
_KEY_TEXT_FIELDS = ("lawd_cd", "dong", "jibun", "name", "building_dong", "house_type")
_KEY_INT_FIELDS = ("floor", "price", "deposit", "monthly_rent")
_KEY_REAL_FIELDS = ("area_m2", "land_area_m2")


def _describe(tx: Transaction) -> str:
    deal_type = getattr(tx.deal_type, "value", tx.deal_type)
    label = " ".join(str(v) for v in (tx.name or tx.dong, tx.jibun) if v)
    return f"{deal_type}/{tx.lawd_cd}/{tx.deal_date} {label}".strip()


def _prepare(tx: Any) -> None:
    """키에 들어가는 값의 형식을 '저장했다 읽은 값'과 같게 맞춘다 (제자리).

    예: 면적 ``85``(int) 는 REAL 로 저장되어 ``85.0`` 으로 돌아오므로, 미리 float 로 바꿔
    ``tx.key`` 와 읽어 온 거래의 ``key`` 가 항상 같게 한다.
    """
    if not isinstance(tx, Transaction):
        raise StorageError(f"Transaction 이 아닌 값은 저장할 수 없습니다: {tx!r}")
    name = "deal_type"  # 오류 메시지용: 지금 다루는 필드
    try:
        if type(tx.deal_type) is not DealType:
            tx.deal_type = coerce_deal_type(tx.deal_type)
        name = "deal_date"
        if type(tx.deal_date) is not date:
            if tx.deal_date is None or tx.deal_date == "":
                raise ValueError("계약일이 없습니다")
            tx.deal_date = _as_date(tx.deal_date)
        for name in _KEY_TEXT_FIELDS:
            value = getattr(tx, name)
            if type(value) is not str:
                setattr(tx, name, _enc_text(value))
        for name in _KEY_INT_FIELDS:
            value = getattr(tx, name)
            if value is not None and type(value) is not int:
                setattr(tx, name, _enc_int(value))
        for name in _KEY_REAL_FIELDS:
            value = getattr(tx, name)
            if value is not None and type(value) is not float:
                setattr(tx, name, _enc_real(value))
        name = "seq"
        if type(tx.seq) is not int:
            tx.seq = _enc_int(tx.seq) or 0
    except (TypeError, ValueError) as exc:
        value = getattr(tx, name, None)
        raise StorageError(f"{_describe(tx)}: '{name}' 값을 저장할 수 없습니다 ({value!r}: {exc})") from exc


def _unique_keys(txs: list[Transaction]) -> list[str]:
    """키 목록. 겹치면(호출자가 assign_seq 를 안 했으면) assign_seq 후 다시 계산한다."""
    keys = [tx.key for tx in txs]
    if len(set(keys)) != len(keys):
        assign_seq(txs)
        keys = [tx.key for tx in txs]
        if len(set(keys)) != len(keys):
            raise StorageError("같은 Transaction 객체가 목록에 여러 번 들어 있습니다 (키 중복).")
    return keys


def _encode_values(tx: Transaction) -> tuple[Any, ...]:
    """``Transaction`` → ``_VALUE_COLUMNS`` 순서의 SQLite 값 튜플 (``_prepare`` 를 거친 거래)."""
    values = list(_GET_VALUES(tx))
    try:
        for i in _ENC_TEXT:
            if type(values[i]) is not str:
                values[i] = _enc_text(values[i])
        for i in _ENC_INT:
            value = values[i]
            if value is not None and type(value) is not int:
                values[i] = _enc_int(value)
        for i in _ENC_REAL:
            value = values[i]
            if value is not None and (type(value) is not float or not math.isfinite(value)):
                values[i] = _enc_real(value)
        for i in _ENC_DATE:
            value = values[i]
            if value is not None:
                values[i] = value.isoformat() if type(value) is date else _enc_date(value)
        for i in _ENC_BOOL:
            values[i] = 1 if values[i] else 0
        for i in _ENC_OPTBOOL:
            value = values[i]
            if value is not None:
                values[i] = 1 if value else 0
        for i, encode in _ENC_OTHER:
            values[i] = encode(values[i])
    except (TypeError, ValueError) as exc:
        raise StorageError(f"{_describe(tx)}: 저장할 수 없는 값이 있습니다 ({_bad_field(tx)}: {exc})") from exc
    iso = values[_DEAL_DATE_INDEX]
    values.append(iso[:4] + iso[5:7])  # deal_ym
    return tuple(values)


def _bad_field(tx: Transaction) -> str:
    """오류 메시지용: 변환에 실패하는 첫 필드."""
    for name, encode in _ENCODERS:
        value = getattr(tx, name)
        try:
            encode(value)
        except (TypeError, ValueError):
            return f"{name}={value!r}"
    return "?"


def _decode_row(row: Sequence[Any]) -> Transaction:
    """``_READ_SELECT`` 한 행 → ``Transaction``."""
    values = list(row[1:])
    try:
        for i in _DEC_BOOL:
            values[i] = bool(values[i])
        for i in _DEC_OPTBOOL:
            value = values[i]
            if value is not None:
                values[i] = bool(value)
        for i in _DEC_DATE:
            value = values[i]
            values[i] = _dec_date(value) if value else None
        for i in _DEC_REAL:
            value = values[i]
            if value is not None and type(value) is not float:
                values[i] = float(value)
        for i, decode in _DEC_OTHER:
            values[i] = decode(values[i])
    except (TypeError, ValueError) as exc:
        raise StorageError(f"저장된 거래를 읽을 수 없습니다 (key={row[0]}): {exc}") from exc
    return Transaction(*values)


def _chunks(items: Sequence[str], size: int = _CHUNK) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _clean_code(lawd_cd: Any) -> str:
    code = str(lawd_cd if lawd_cd is not None else "").strip()
    if not code:
        raise StorageError("시군구 코드(lawd_cd)가 비어 있습니다.")
    return code


def _limit(limit: Any) -> int:
    return max(0, int(limit))


# ====================================================================== 조회 조건


@lru_cache(maxsize=8192)
def _search_text(text: str) -> str:
    """검색 비교용: 공백 제거 + 대소문자 무시."""
    return "".join(text.split()).casefold()


def _sql_contains(haystack: Any, needle: Any) -> int:
    if not haystack or needle is None:
        return 0
    return 1 if needle in _search_text(str(haystack)) else 0


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        return [str(value)]
    return [str(item) for item in value]


def _code_list(value: Any) -> list[str]:
    items = [value] if isinstance(value, str) or not isinstance(value, Iterable) else list(value)
    codes: list[str] = []
    for item in items:
        item = getattr(item, "lawd_cd", item)  # Region 객체도 받는다
        for token in str(item).split(","):
            token = token.strip()
            if token and token not in codes:
                codes.append(token)
    return codes


def _build_where(
    *,
    deal_types: Any = None,
    lawd_cds: Any = None,
    start_ym: Any = None,
    end_ym: Any = None,
    name: Any = None,
    dong: Any = None,
    include_cancelled: bool = True,
    min_area: float | None = None,
    max_area: float | None = None,
    first_seen_since: datetime | date | str | None = None,
) -> tuple[str, list[Any]]:
    """query/count 공통 WHERE 절. 빈 목록 필터(``deal_types=[]`` 등)는 결과 없음."""
    clauses: list[str] = []
    params: list[Any] = []
    if deal_types is not None:
        codes = [member.value for member in DealType.parse_many(deal_types)]
        if not codes:
            return " WHERE 0", []
        clauses.append(f"deal_type IN ({_marks(len(codes))})")
        params.extend(codes)
    if lawd_cds is not None:
        codes = _code_list(lawd_cds)
        if not codes:
            return " WHERE 0", []
        clauses.append(f"lawd_cd IN ({_marks(len(codes))})")
        params.extend(codes)
    if start_ym is not None:
        clauses.append("deal_ym >= ?")
        params.append(parse_ym(start_ym))
    if end_ym is not None:
        clauses.append("deal_ym <= ?")
        params.append(parse_ym(end_ym))
    for column, value in (("name", name), ("dong", dong)):
        if value is None:
            continue
        needles = [_search_text(text) for text in _str_list(value)]
        if not needles:
            return " WHERE 0", []
        if "" in needles:  # 빈 검색어는 모든 행과 일치
            continue
        clauses.append("(" + " OR ".join(f"silgeorae_contains({column}, ?)" for _ in needles) + ")")
        params.extend(needles)
    if not include_cancelled:
        clauses.append("is_cancelled = 0")
    if min_area is not None:
        clauses.append("area_m2 >= ?")
        params.append(float(min_area))
    if max_area is not None:
        clauses.append("area_m2 <= ?")
        params.append(float(max_area))
    if first_seen_since is not None:
        clauses.append("first_seen_at >= ?")
        params.append(_ts_text(first_seen_since))
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


# ====================================================================== 병합 (파티션 교체·upsert 공통)


def _merge(
    conn: sqlite3.Connection,
    txs: list[Transaction],
    keys: list[str],
    stamp: str,
    partition: tuple[str, str, str] | None,
) -> tuple[PartitionResult, list[tuple[str, str]]]:
    """새 목록과 기존 행을 비교해 INSERT/UPDATE/(파티션이면) DELETE 한다. 트랜잭션 안에서 부른다.

    반환: (결과, 거래별 (first_seen_at, updated_at) 저장 문자열).
    """
    result = PartitionResult()
    encoded = [_encode_values(tx) for tx in txs]

    # 기존 행 = 이 파티션의 행 ∪ 새 목록의 키로 찾은 행
    existing: dict[str, tuple[Any, ...]] = {}
    if partition is not None:
        sql = f"SELECT {_EXISTING_SELECT} FROM transactions WHERE deal_type = ? AND lawd_cd = ? AND deal_ym = ?"
        for row in conn.execute(sql, partition):
            existing[row[0]] = row
    lookup = [key for key in keys if key not in existing]
    for chunk in _chunks(lookup):
        sql = f"SELECT {_EXISTING_SELECT} FROM transactions WHERE key IN ({_marks(len(chunk))})"
        for row in conn.execute(sql, list(chunk)):
            existing[row[0]] = row

    inserts: list[tuple[Any, ...]] = []
    updates: list[tuple[Any, ...]] = []
    stamps: list[tuple[str, str]] = []
    for key, values in zip(keys, encoded, strict=True):
        old = existing.get(key)
        if old is None:
            inserts.append((key, *values, stamp, stamp))
            result.inserted += 1
            result.new_keys.append(key)
            stamps.append((stamp, stamp))
        elif old[_EXISTING_OFFSET:] == values:
            result.unchanged += 1
            stamps.append((old[1], old[2]))
        else:
            updates.append((*values, stamp, key))
            result.updated += 1
            if values[_CANCEL_INDEX] and not old[_EXISTING_OFFSET + _CANCEL_INDEX]:
                result.newly_cancelled += 1
                result.cancelled_keys.append(key)
            stamps.append((old[1], stamp))

    removed: list[str] = []
    if partition is not None:
        incoming = set(keys)
        removed = [key for key in existing if key not in incoming]

    if inserts:
        conn.executemany(_INSERT_SQL, inserts)
    if updates:
        conn.executemany(_UPDATE_SQL, updates)
    if removed:
        conn.executemany(_DELETE_SQL, [(key,) for key in removed])
    result.removed = len(removed)
    return result, stamps


def _apply_stamps(txs: list[Transaction], stamps: list[tuple[str, str]]) -> None:
    for tx, (first_seen, updated) in zip(txs, stamps, strict=True):
        tx.first_seen_at = _ts_value(first_seen)
        tx.updated_at = _ts_value(updated)


def _record_fetch(
    conn: sqlite3.Connection,
    deal_type: str,
    lawd_cd: str,
    deal_ym: str,
    item_count: int,
    status: str,
    message: str,
    stamp: str,
) -> None:
    ok_at = stamp if status == "ok" else None
    cursor = conn.execute(
        "UPDATE fetch_log SET fetched_at = ?, item_count = ?, status = ?, message = ?, "
        "last_ok_at = COALESCE(?, last_ok_at) WHERE deal_type = ? AND lawd_cd = ? AND deal_ym = ?",
        (stamp, item_count, status, message, ok_at, deal_type, lawd_cd, deal_ym),
    )
    if cursor.rowcount == 0:
        conn.execute(
            "INSERT INTO fetch_log (deal_type, lawd_cd, deal_ym, fetched_at, item_count, status, message, last_ok_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (deal_type, lawd_cd, deal_ym, stamp, item_count, status, message, ok_at),
        )


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_META)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    else:
        try:
            version = int(row[0])
        except (TypeError, ValueError):
            raise StorageError(f"저장소 형식 버전을 알 수 없습니다: {row[0]!r}") from None
        if version > SCHEMA_VERSION:
            raise StorageError(
                f"이 저장소는 더 새로운 형식(v{version})입니다. silgeorae 를 최신 버전으로 업데이트하세요 "
                f"(지원하는 형식: v{SCHEMA_VERSION})."
            )
    conn.execute(_CREATE_TRANSACTIONS)
    conn.execute(_CREATE_FETCH_LOG)
    present = {info[1] for info in conn.execute("PRAGMA table_info(transactions)")}
    for name in _VALUE_COLUMNS + _TIMESTAMP_FIELDS:
        if name not in present:  # 모델에 새로 생긴 필드
            ddl = "TEXT" if name == "deal_ym" else _addable_ddl(_column_ddl(name))
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {ddl}")
            logger.info("저장소에 '%s' 열을 추가했습니다.", name)
    for statement in _CREATE_INDEXES:
        conn.execute(statement)


# ====================================================================== 저장소


class TransactionStore:
    """거래 저장소 (SQLite 파일 1개, 기본값은 메모리).

    >>> with TransactionStore("data/silgeorae.db") as store:   # doctest: +SKIP
    ...     result = store.replace_partition(DealType.APT_SALE, "11680", "202501", transactions)
    ...     recent = store.query(lawd_cds=["11680"], start_ym="2025-01")
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.path: str | Path
        if isinstance(path, str) and path.strip() in (":memory:", ""):
            self.path = ":memory:"
            target = ":memory:"
        else:
            file_path = Path(path).expanduser()
            if file_path.is_dir():
                raise StorageError(f"저장소 경로가 폴더입니다: {file_path}")
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StorageError(f"저장소 폴더를 만들 수 없습니다: {file_path.parent} ({exc})") from exc
            self.path = file_path
            target = str(file_path)
        try:
            conn = sqlite3.connect(target, timeout=_BUSY_TIMEOUT, isolation_level=None, check_same_thread=False)
        except sqlite3.Error as exc:
            raise StorageError(f"저장소를 열 수 없습니다: {target} ({exc})") from exc
        self._conn = conn
        try:
            try:
                conn.create_function("silgeorae_contains", 2, _sql_contains, deterministic=True)
            except sqlite3.NotSupportedError:  # 아주 오래된 SQLite
                conn.create_function("silgeorae_contains", 2, _sql_contains)
            with self._writing(f"저장소 열기({target})") as c:
                _init_schema(c)
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------------ 수명
    def close(self) -> None:
        """연결을 닫는다 (여러 번 불러도 된다)."""
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as exc:  # pragma: no cover - 닫기 실패는 무시
                logger.debug("저장소 닫기 실패: %s", exc)

    @property
    def closed(self) -> bool:
        return self._conn is None

    def __enter__(self) -> TransactionStore:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"TransactionStore({str(self.path)!r})"

    @property
    def schema_version(self) -> int:
        """저장된 형식 버전 (``meta.schema_version``)."""
        with self._reading("형식 버전 조회") as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ 내부: 트랜잭션
    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("저장소가 이미 닫혔습니다.")
        return self._conn

    @contextmanager
    def _reading(self, action: str) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._require_conn()
            try:
                yield conn
            except sqlite3.Error as exc:
                raise StorageError(f"{action} 중 저장소 오류: {exc}") from exc

    @contextmanager
    def _writing(self, action: str) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` … ``COMMIT`` (예외가 나면 ``ROLLBACK``)."""
        with self._lock:
            conn = self._require_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StorageError(f"{action} 중 저장소 오류: {exc}") from exc
            try:
                yield conn
            except BaseException as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error as rollback_exc:  # pragma: no cover - 연결이 이미 끊긴 경우
                    logger.debug("ROLLBACK 실패: %s", rollback_exc)
                if isinstance(exc, sqlite3.Error):
                    raise StorageError(f"{action} 중 저장소 오류: {exc}") from exc
                raise
            try:
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover
                    pass
                raise StorageError(f"{action} 중 저장소 오류(커밋 실패): {exc}") from exc

    # ------------------------------------------------------------------ 쓰기
    def replace_partition(
        self,
        deal_type: DealType | str,
        lawd_cd: str,
        deal_ym: str | int,
        transactions: Iterable[Transaction],
        *,
        fetched_at: datetime | None = None,
    ) -> PartitionResult:
        """새로 받은 목록을 파티션(유형·시군구·계약연월)의 정답으로 보고 한 트랜잭션으로 반영한다.

        * 처음 보는 키 → INSERT (``first_seen_at`` = ``updated_at`` = ``fetched_at``, 기본 지금)
        * 있던 키 → 값이 바뀌었으면 UPDATE (``first_seen_at`` 유지). 해제여부 False → True 면 ``newly_cancelled``
        * 새 목록에 없는 이 파티션의 키 → DELETE
        * 마지막에 ``record_fetch(status="ok", item_count=len(transactions))``

        모든 거래는 ``deal_type``·``lawd_cd`` 가 파티션과 같아야 한다 (아니면 ``StorageError``).
        계약연월만 다른 거래는 경고 로그를 남기고 자기 계약연월로 저장한다.
        """
        deal_type = coerce_deal_type(deal_type)
        lawd_cd = _clean_code(lawd_cd)
        deal_ym = parse_ym(deal_ym)
        where = f"{deal_type.value}/{lawd_cd}/{deal_ym}"
        txs = list(transactions)
        outside = 0
        for tx in txs:
            _prepare(tx)
            if tx.deal_type is not deal_type:
                raise StorageError(f"파티션({where})과 거래 유형이 다른 거래가 있습니다: {_describe(tx)}")
            if tx.lawd_cd != lawd_cd:
                raise StorageError(f"파티션({where})과 시군구 코드가 다른 거래가 있습니다: {_describe(tx)}")
            if ym_of(tx.deal_date) != deal_ym:
                outside += 1
        if outside:
            logger.warning("파티션(%s)에 계약연월이 다른 거래 %d건이 있어 각자의 계약연월로 저장합니다.", where, outside)
        keys = _unique_keys(txs)
        stamp = _ts_text(fetched_at if fetched_at is not None else now_kst())
        with self._writing(f"파티션 교체({where})") as conn:
            result, stamps = _merge(conn, txs, keys, stamp, (deal_type.value, lawd_cd, deal_ym))
            _record_fetch(conn, deal_type.value, lawd_cd, deal_ym, len(txs), "ok", "", stamp)
        _apply_stamps(txs, stamps)
        logger.debug(
            "파티션 교체(%s): 신규 %d · 변경 %d · 동일 %d · 삭제 %d · 새 해제 %d",
            where, result.inserted, result.updated, result.unchanged, result.removed, result.newly_cancelled,
        )
        return result

    def upsert(self, transactions: Iterable[Transaction], *, now: datetime | None = None) -> PartitionResult:
        """``replace_partition`` 과 같은 병합이지만 지우지 않고 ``fetch_log`` 도 건드리지 않는다."""
        txs = list(transactions)
        if not txs:
            return PartitionResult()
        for tx in txs:
            _prepare(tx)
        keys = _unique_keys(txs)
        stamp = _ts_text(now if now is not None else now_kst())
        with self._writing("거래 저장") as conn:
            result, stamps = _merge(conn, txs, keys, stamp, None)
        _apply_stamps(txs, stamps)
        return result

    def record_fetch(
        self,
        deal_type: DealType | str,
        lawd_cd: str,
        deal_ym: str | int,
        *,
        item_count: int,
        status: str = "ok",
        message: str = "",
        fetched_at: datetime | None = None,
    ) -> None:
        """파티션의 수집 시도를 기록한다 (파티션마다 최신 1행). ``status="ok"`` 일 때만 ``last_ok_at`` 갱신."""
        deal_type = coerce_deal_type(deal_type)
        lawd_cd = _clean_code(lawd_cd)
        deal_ym = parse_ym(deal_ym)
        stamp = _ts_text(fetched_at if fetched_at is not None else now_kst())
        with self._writing("수집 기록 저장") as conn:
            _record_fetch(conn, deal_type.value, lawd_cd, deal_ym, int(item_count), str(status), str(message or ""), stamp)

    # ------------------------------------------------------------------ 읽기
    def query(
        self,
        *,
        deal_types: Iterable[DealType | str] | DealType | str | None = None,
        lawd_cds: Iterable[str] | str | None = None,
        start_ym: str | int | None = None,
        end_ym: str | int | None = None,
        name: str | Iterable[str] | None = None,
        dong: str | Iterable[str] | None = None,
        include_cancelled: bool = True,
        min_area: float | None = None,
        max_area: float | None = None,
        first_seen_since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Transaction]:
        """조건에 맞는 거래 (계약일 → 키 순).

        * ``deal_types`` — DealType 또는 코드·한글 이름(``"apt_sale"``, ``"아파트"``), 목록 가능
        * ``lawd_cds`` — 시군구 코드 목록 (문자열 하나·쉼표 구분도 가능)
        * ``start_ym``·``end_ym`` — ``parse_ym`` 이 받는 표기, 양끝 포함
        * ``name``·``dong`` — 부분 일치 (대소문자·공백 무시), 목록이면 그중 하나
        * ``min_area``·``max_area`` — ``area_m2`` 범위, ``first_seen_since`` — 그 시각 이후 처음 저장된 거래
          (tz 없는 datetime 은 한국 시간)
        * 빈 목록 필터(``deal_types=[]``)는 결과 없음, ``None`` 은 조건 없음
        * 알 수 없는 유형·연월 표기는 ``ValueError`` (한국어 메시지)
        """
        where, params = _build_where(
            deal_types=deal_types, lawd_cds=lawd_cds, start_ym=start_ym, end_ym=end_ym, name=name, dong=dong,
            include_cancelled=include_cancelled, min_area=min_area, max_area=max_area,
            first_seen_since=first_seen_since,
        )
        sql = f"SELECT {_READ_SELECT} FROM transactions{where} ORDER BY deal_date, key"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(_limit(limit))
        with self._reading("거래 조회") as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_decode_row(row) for row in rows]

    def count(self, **filters: Any) -> int:
        """``query`` 와 같은 조건의 거래 수."""
        limit = filters.pop("limit", None)
        where, params = _build_where(**filters)
        sql = f"SELECT COUNT(*) FROM transactions{where}"
        if limit is not None:
            sql = f"SELECT COUNT(*) FROM (SELECT 1 FROM transactions{where} LIMIT ?)"
            params.append(_limit(limit))
        with self._reading("거래 수 조회") as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def get(self, key: str) -> Transaction | None:
        """키로 거래 1건 (없으면 ``None``)."""
        with self._reading("거래 조회") as conn:
            row = conn.execute(f"SELECT {_READ_SELECT} FROM transactions WHERE key = ?", (str(key),)).fetchone()
        return _decode_row(row) if row else None

    def get_many(self, keys: Iterable[str]) -> list[Transaction]:
        """여러 키의 거래를 주어진 순서대로 (없는 키는 건너뜀). 예: ``result.new_keys``."""
        wanted = list(dict.fromkeys(str(key) for key in keys))
        found: dict[str, Sequence[Any]] = {}
        with self._reading("거래 조회") as conn:
            for chunk in _chunks(wanted):
                sql = f"SELECT {_READ_SELECT} FROM transactions WHERE key IN ({_marks(len(chunk))})"
                for row in conn.execute(sql, list(chunk)):
                    found[row[0]] = row
        return [_decode_row(found[key]) for key in wanted if key in found]

    def last_fetched(self, deal_type: DealType | str, lawd_cd: str, deal_ym: str | int) -> datetime | None:
        """파티션을 마지막으로 성공(``status="ok"``)해 받은 시각. 성공한 적이 없으면 ``None``."""
        params = (coerce_deal_type(deal_type).value, _clean_code(lawd_cd), parse_ym(deal_ym))
        with self._reading("수집 기록 조회") as conn:
            row = conn.execute(
                "SELECT last_ok_at FROM fetch_log WHERE deal_type = ? AND lawd_cd = ? AND deal_ym = ?", params
            ).fetchone()
        return _ts_value(row[0]) if row else None

    def fetch_log(self, *, limit: int | None = None, status: str | None = None) -> list[FetchLogEntry]:
        """파티션별 마지막 수집 시도 (최근 순). ``status`` 로 거를 수 있다 (예: ``"error"``)."""
        sql = "SELECT deal_type, lawd_cd, deal_ym, fetched_at, item_count, status, message FROM fetch_log"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(str(status))
        sql += " ORDER BY fetched_at DESC, deal_type, lawd_cd, deal_ym"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(_limit(limit))
        with self._reading("수집 기록 조회") as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            FetchLogEntry(
                deal_type=_dec_enum(row[0]),
                lawd_cd=row[1],
                deal_ym=row[2],
                fetched_at=_ts_value(row[3]),
                item_count=int(row[4]),
                status=row[5],
                message=row[6],
            )
            for row in rows
        ]

    def summary(self) -> list[dict[str, Any]]:
        """유형·시군구별 건수: ``deal_type``(DealType), ``lawd_cd``, ``count``, ``cancelled``, ``min_ym``, ``max_ym``."""
        with self._reading("요약 조회") as conn:
            rows = conn.execute(
                "SELECT deal_type, lawd_cd, COUNT(*), COALESCE(SUM(is_cancelled), 0), MIN(deal_ym), MAX(deal_ym) "
                "FROM transactions GROUP BY deal_type, lawd_cd"
            ).fetchall()
        result = [
            {
                "deal_type": _dec_enum(row[0]),
                "lawd_cd": row[1],
                "count": int(row[2]),
                "cancelled": int(row[3]),
                "min_ym": row[4],
                "max_ym": row[5],
            }
            for row in rows
        ]
        result.sort(key=lambda item: (_DEAL_TYPE_ORDER[item["deal_type"]], item["lawd_cd"]))
        return result
