"""tests/app 공용 도우미 — 환경변수 격리, 가짜 API 클라이언트, 원본 레코드 생성."""

from __future__ import annotations

from typing import Callable

import pytest

from silgeorae.models import DealType

_ENV_VARS = (
    "SILGEORAE_SERVICE_KEY",
    "DATA_GO_KR_SERVICE_KEY",
    "SILGEORAE_WEBHOOK_URL",
    "SILGEORAE_TELEGRAM_BOT_TOKEN",
    "SILGEORAE_TELEGRAM_CHAT_ID",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """개발자 PC 의 인증키·알림 환경변수가 테스트에 끼어들지 않게 한다."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")


def sale_item(ym: str, *, day: int = 3, price: str = "85,000", floor: str = "7", name: str = "한빛마을1단지",
              area: str = "84.97", cancel_day: str = "", **extra: str) -> dict[str, str]:
    """``tests/fixtures/apt_trade.xml`` 과 같은 모양의 아파트 매매 원본 레코드."""
    item = {
        "aptDong": "101", "aptNm": name, "aptSeq": "11680-9001", "bonbun": "0316", "bubun": "0000",
        "buildYear": "2015", "buyerGbn": "개인", "cdealDay": " ", "cdealType": " ", "dealAmount": price,
        "dealDay": str(day), "dealMonth": str(int(ym[4:])), "dealYear": ym[:4], "dealingGbn": "중개거래",
        "estateAgentSggNm": "서울 강남구", "excluUseAr": area, "floor": floor, "jibun": "316", "landCd": "1",
        "landLeaseholdGbn": "N", "rgstDate": " ", "roadNm": "샘플로", "roadNmBonbun": "00051",
        "roadNmBubun": "00000", "sggCd": "11680", "slerGbn": "개인", "umdCd": "10600", "umdNm": "대치동",
    }
    if cancel_day:
        item["cdealType"] = "O"
        item["cdealDay"] = cancel_day
    item.update(extra)
    return item


def rent_item(ym: str, *, day: int = 8, deposit: str = "150,000", monthly: str = "0") -> dict[str, str]:
    """``tests/fixtures/apt_rent.xml`` 과 같은 모양의 아파트 전월세 원본 레코드."""
    return {
        "aptNm": "한빛마을1단지", "buildYear": "2015", "contractTerm": "25.02~27.02", "contractType": "신규",
        "dealDay": str(day), "dealMonth": str(int(ym[4:])), "dealYear": ym[:4], "deposit": deposit,
        "excluUseAr": "84.97", "floor": "7", "jibun": "316", "monthlyRent": monthly, "preDeposit": " ",
        "preMonthlyRent": " ", "sggCd": "11680", "umdNm": "대치동", "useRRRight": " ",
    }


class FakeClient:
    """``MolitClient`` 대역: ``fetch`` 는 미리 넣은 원본 레코드를 돌려주고 호출을 기록한다."""

    def __init__(self, data: dict | None = None, errors: dict | None = None) -> None:
        self.data: dict[tuple[DealType, str, str], list[dict[str, str]]] = dict(data or {})
        self.errors: dict[tuple[DealType, str, str], Exception] = dict(errors or {})
        self.calls: list[tuple[DealType, str, str]] = []
        self.request_count = 0

    def fetch(self, deal_type: DealType, lawd_cd: str, deal_ym: str) -> list[dict[str, str]]:
        key = (deal_type, lawd_cd, deal_ym)
        self.calls.append(key)
        self.request_count += 1
        if key in self.errors:
            raise self.errors[key]
        return [dict(item) for item in self.data.get(key, [])]


@pytest.fixture
def make_sale() -> Callable[..., dict[str, str]]:
    return sale_item


@pytest.fixture
def make_rent() -> Callable[..., dict[str, str]]:
    return rent_item


@pytest.fixture
def fake_client_cls() -> type:
    return FakeClient


@pytest.fixture
def fixture_tags() -> Callable[[str], set[str]]:
    """공용 XML 픽스처의 첫 item 태그 집합."""
    import xml.etree.ElementTree as ET
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "fixtures"

    def tags(name: str) -> set[str]:
        item = ET.parse(root / name).getroot().find("./body/items/item")
        assert item is not None
        return {child.tag for child in item}

    return tags
