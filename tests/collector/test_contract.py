"""docs/ARCHITECTURE.md §5.2 계약(공개 인터페이스) 고정 — 다른 팀이 이 모양에 맞춰 코딩한다."""

from __future__ import annotations

import dataclasses
import inspect
import time

import silgeorae.collector as collector
from silgeorae.collector import FakeTransport, HttpResponse, MolitClient, ParsedPage
from silgeorae.regions import Region, RegionTable

KW = inspect.Parameter.KEYWORD_ONLY


def params_of(func) -> dict[str, inspect.Parameter]:
    return dict(inspect.signature(func).parameters)


def test_collector_exports():
    expected = {
        "HttpResponse", "Transport", "urllib_transport", "ParsedPage", "parse_response", "MolitClient",
        "build_response_xml", "build_error_xml", "FakeTransport", "sync_regions",
    }
    assert expected <= set(collector.__all__)
    assert all(hasattr(collector, name) for name in collector.__all__)


def test_region_dataclass():
    assert [f.name for f in dataclasses.fields(Region)] == [
        "lawd_cd", "sido", "sigungu", "parent_cd", "is_leaf", "former_cd", "note",
    ]
    region = Region("11680", "서울특별시", "강남구")
    assert (region.parent_cd, region.is_leaf, region.former_cd, region.note) == ("", True, "", "")
    assert dataclasses.is_dataclass(region) and Region.__dataclass_params__.frozen


def test_region_table_methods():
    assert list(params_of(RegionTable.load)) == ["path"]
    assert params_of(RegionTable.load)["path"].default is None
    assert params_of(RegionTable.all)["leaves_only"].kind is KW
    for name in ("get", "search", "resolve", "resolve_many", "name_of", "save"):
        assert callable(getattr(RegionTable, name))


def test_molit_client_signature():
    params = params_of(MolitClient.__init__)
    assert list(params) == [
        "self", "service_key", "transport", "timeout", "max_retries", "backoff", "page_size",
        "min_interval", "base_url", "service_overrides", "sleep",
    ]
    defaults = {name: p.default for name, p in params.items() if name not in ("self", "service_key")}
    assert defaults == {
        "transport": None, "timeout": 20.0, "max_retries": 3, "backoff": 1.0, "page_size": 1000,
        "min_interval": 0.0, "base_url": "https://apis.data.go.kr/1613000", "service_overrides": None,
        "sleep": time.sleep,
    }
    assert all(p.kind is KW for name, p in params.items() if name not in ("self", "service_key"))
    assert MolitClient.DEFAULT_BASE_URL == "https://apis.data.go.kr/1613000"
    assert list(params_of(MolitClient.fetch)) == ["self", "deal_type", "lawd_cd", "deal_ym"]
    assert list(params_of(MolitClient.url_for)) == ["self", "deal_type"]
    assert isinstance(MolitClient.request_count, property)


def test_data_classes():
    assert [f.name for f in dataclasses.fields(HttpResponse)] == ["status", "body", "headers"]
    assert [f.name for f in dataclasses.fields(ParsedPage)] == [
        "items", "total_count", "page_no", "num_of_rows", "result_code", "result_msg",
    ]


def test_builders_and_fake_signatures():
    build = params_of(collector.build_response_xml)
    assert list(build) == ["items", "total_count", "page_no", "num_of_rows", "result_code", "result_msg"]
    assert [build[n].default for n in list(build)[1:]] == [None, 1, None, "000", "OK"]
    assert all(build[n].kind is KW for n in list(build)[1:])
    error = params_of(collector.build_error_xml)
    assert list(error) == ["reason_code", "auth_msg", "err_msg"] and error["err_msg"].default == "SERVICE ERROR"
    assert list(params_of(FakeTransport.__init__)) == ["self", "data"]
    assert list(params_of(FakeTransport.add)) == ["self", "deal_type", "lawd_cd", "deal_ym", "items"]
    fail = params_of(FakeTransport.fail)
    assert list(fail) == ["self", "deal_type", "lawd_cd", "deal_ym", "response", "times"]
    assert fail["times"].default is None
    assert list(params_of(FakeTransport.__call__)) == ["self", "url", "params", "timeout"]
    assert FakeTransport().calls == []


def test_sync_regions_signature():
    params = params_of(collector.sync_regions)
    assert list(params)[:2] == ["service_key", "transport"]
    assert params["transport"].kind is KW and params["transport"].default is None
