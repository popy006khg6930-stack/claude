"""분석팀 테스트 공용 — 손으로 만드는 작은 거래 목록."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from silgeorae.models import DealType, Transaction


def _make_tx(deal_type: DealType = DealType.APT_SALE, when: str | date = "2025-01-15", **kw: Any) -> Transaction:
    """기본값이 채워진 거래 1건. ``when`` 은 ``"YYYY-MM-DD"`` 또는 ``date``."""
    base: dict[str, Any] = dict(
        deal_type=deal_type,
        lawd_cd="11680",
        deal_date=date.fromisoformat(when) if isinstance(when, str) else when,
        dong="대치동",
        jibun="316",
        name="한빛마을1단지",
        sigungu="강남구",
        floor=10,
        area_m2=84.97,
        build_year=2015,
    )
    if deal_type.is_sale:
        base["price"] = 100000
    else:
        base["deposit"] = 50000
        base["monthly_rent"] = 0
    base.update(kw)
    return Transaction(**base)


@pytest.fixture
def make_tx():
    return _make_tx
