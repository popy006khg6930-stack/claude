"""``urllib_transport`` — ``urlopen`` 을 가짜로 바꿔 네트워크 없이 확인한다."""

from __future__ import annotations

import http.client
import io
import urllib.error
import urllib.request
from email.message import Message

import pytest

from silgeorae.collector import HttpResponse, urllib_transport

URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str], *, fail_read: bool = False):
        self.status = status
        self._body = body
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value
        self._fail_read = fail_read

    def read(self) -> bytes:
        if self._fail_read:
            raise http.client.IncompleteRead(b"partial")
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def urlopen(monkeypatch):
    """``urllib.request.urlopen`` 을 바꿔 끼우고 받은 요청을 기록한다."""
    state = {"requests": [], "result": FakeResponse(200, b"<ok/>", {"Content-Type": "text/xml;charset=UTF-8"})}

    def fake_urlopen(request, timeout=None):
        state["requests"].append((request, timeout))
        result = state["result"]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return state


def test_builds_encoded_query(urlopen):
    response = urllib_transport(URL, {"serviceKey": "a+b/c==", "LAWD_CD": "11680", "DEAL_YMD": "202501"}, 7.5)
    assert response == HttpResponse(200, b"<ok/>", {"Content-Type": "text/xml;charset=UTF-8"})
    [(request, timeout)] = urlopen["requests"]
    # Decoding 키의 + / = 는 한 번만 인코딩된다
    assert request.full_url == URL + "?serviceKey=a%2Bb%2Fc%3D%3D&LAWD_CD=11680&DEAL_YMD=202501"
    assert request.get_method() == "GET"
    assert timeout == 7.5
    assert request.get_header("User-agent").startswith("silgeorae/")


def test_http_errors_become_responses(urlopen):
    headers = Message()
    headers["Content-Type"] = "text/html"
    urlopen["result"] = urllib.error.HTTPError(URL, 503, "Service Unavailable", headers, io.BytesIO(b"busy"))
    response = urllib_transport(URL, {"a": "1"}, 5.0)
    assert (response.status, response.body, response.headers) == (503, b"busy", {"Content-Type": "text/html"})
    assert not response.ok


def test_http_error_without_body(urlopen):
    urlopen["result"] = urllib.error.HTTPError(URL, 401, "Unauthorized", None, None)
    assert urllib_transport(URL, {}, 5.0) == HttpResponse(401, b"", {})


@pytest.mark.parametrize(
    "error", [urllib.error.URLError("connection refused"), TimeoutError("timed out"), ConnectionResetError()]
)
def test_network_errors_propagate_as_oserror(urlopen, error):
    urlopen["result"] = error
    with pytest.raises(OSError):
        urllib_transport(URL, {}, 5.0)


def test_incomplete_read_becomes_connection_error(urlopen):
    urlopen["result"] = FakeResponse(200, b"", {}, fail_read=True)
    with pytest.raises(ConnectionError):
        urllib_transport(URL, {}, 5.0)


def test_http_response_helpers():
    assert HttpResponse(200, "한글".encode("utf-8")).text == "한글"
    assert HttpResponse(200, b"\xff\xfe").text == "\ufffd\ufffd"
    assert HttpResponse(200, "문자열 본문").body == "문자열 본문".encode("utf-8")
    assert HttpResponse(200, b"").headers == {}
    assert HttpResponse(204, b"").ok and not HttpResponse(404, b"").ok
