"""정제·저장팀 모듈 — 원본 레코드 정규화와 SQLite 저장·갱신 (ARCHITECTURE.md §5.3).

* ``normalize_item`` / ``normalize_items`` : API 원본 dict → ``Transaction``
* ``TransactionStore`` : 파티션 교체(신규·변경·해제·삭제 감지), 조회, 수집 기록
"""

from __future__ import annotations

from .normalize import normalize_item, normalize_items
from .storage import FetchLogEntry, PartitionResult, TransactionStore

__all__ = [
    "normalize_item",
    "normalize_items",
    "TransactionStore",
    "PartitionResult",
    "FetchLogEntry",
]
