from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from silgeorae.models import DealType, Transaction

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load_items(name: str) -> list[dict[str, str]]:
    """``tests/fixtures/<name>.xml`` 의 item 목록 (수집팀 모듈 없이 직접 파싱)."""
    root = ET.parse(FIXTURES / f"{name}.xml").getroot()
    return [{child.tag: child.text or "" for child in item} for item in root.iter("item")]


@pytest.fixture
def load_items():
    return _load_items


@pytest.fixture
def make_tx():
    def factory(**overrides) -> Transaction:
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
        base.update(overrides)
        return Transaction(**base)

    return factory
