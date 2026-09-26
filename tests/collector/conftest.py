"""수집팀 테스트 공용 픽스처 (네트워크·실제 sleep 없음)."""

from __future__ import annotations

from pathlib import Path

import pytest

from silgeorae.collector import HttpResponse
from silgeorae.regions import RegionTable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class SleepRecorder(list):
    """``sleep`` 대신 끼워 넣어 기다린 시간만 기록한다."""

    def __call__(self, seconds: float) -> None:
        self.append(seconds)


@pytest.fixture
def read_fixture():
    def _read(name: str) -> bytes:
        return (FIXTURES / name).read_bytes()

    return _read


@pytest.fixture
def fixture_response(read_fixture):
    def _response(name: str, status: int = 200) -> HttpResponse:
        return HttpResponse(status, read_fixture(name))

    return _response


@pytest.fixture(scope="session")
def table() -> RegionTable:
    return RegionTable.load()


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()
