"""로컬 HTTP 서버 + 실제 CLI 로 ``update`` 전 과정을 확인한다.

``FakeTransport`` 를 직접 끼우는 다른 테스트와 달리, 여기서는 ``urllib_transport`` 가 진짜 HTTP 요청을
보내고(쿼리 인코딩·인증키 처리 포함), 설정 파일의 ``[api] base_url`` 로 가짜 API 서버를 가리킨다.
"""

from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from silgeorae.cli import main
from silgeorae.collector import FakeTransport
from silgeorae.demo import generate_demo_data
from silgeorae.models import DealType
from silgeorae.processing import TransactionStore
from silgeorae.utils import recent_months

GANGNAM = "11680"


@pytest.fixture
def api_server(monkeypatch):
    """``FakeTransport`` 를 HTTP 로 내보내는 로컬 서버 → (fake, 받은 요청 목록, base_url)."""
    for name in ("NO_PROXY", "no_proxy"):  # 사내 프록시 환경변수가 있어도 로컬 서버로 바로 가게
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    fake = FakeTransport()
    requests: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server 규약
            url = urllib.parse.urlsplit(self.path)
            params = dict(urllib.parse.parse_qsl(url.query))
            requests.append(params)
            response = fake(f"http://local{url.path}", params, 5.0)
            self.send_response(response.status)
            self.send_header("Content-Type", "text/xml;charset=UTF-8")
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield fake, requests, f"http://127.0.0.1:{server.server_address[1]}/1613000"
    finally:
        server.shutdown()
        server.server_close()


def test_update_over_real_http(api_server, tmp_path, monkeypatch, capsys):
    fake, requests, base_url = api_server
    months = recent_months(3)
    for (deal_type, lawd_cd, ym), items in generate_demo_data(months).items():
        fake.add(deal_type, lawd_cd, ym, items)
    (tmp_path / "silgeorae.toml").write_text(
        f"""
[api]
service_key = "abc%2Bdef%3D%3D"   # Encoding 키를 그대로 넣어도 된다
base_url = "{base_url}"

[storage]
db_path = "deals.db"

[watch]
regions = ["강남구"]
deal_types = ["apt_sale", "apt_rent"]
months = 3

[report]
formats = ["html", "csv"]
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    # 1) 첫 실행: 전부 받아 저장하지만, 처음 받는 지역·유형이라 '신규' 알림에서는 뺀다
    assert main(["update", "--report", "--no-notify"]) == 0
    out = capsys.readouterr().out
    types = (DealType.APT_SALE, DealType.APT_RENT)
    expected = sum(len(fake.items_for(dt, GANGNAM, ym)) for dt in types for ym in months)
    with TransactionStore(tmp_path / "deals.db") as store:
        assert store.count() == expected > 0
    assert "신규 알림에서 제외" in out
    assert {r["serviceKey"] for r in requests} == {"abc+def=="}  # 한 번만 인코딩되어 도착
    assert {r["LAWD_CD"] for r in requests} == {GANGNAM}
    assert {r["DEAL_YMD"] for r in requests} == set(months)
    reports = sorted(p.name for p in (tmp_path / "reports").iterdir())
    assert any(name.endswith(".html") for name in reports) and any(name.endswith(".csv") for name in reports)

    # 2) 새 거래가 신고되면 다음 update 에서 '신규'로 잡힌다
    newest = months[-1]
    base = fake.items_for(DealType.APT_SALE, GANGNAM, newest)[0]
    fake.add(DealType.APT_SALE, GANGNAM, newest, [dict(base, dealAmount="999,999", floor="33", dealDay="28")])
    assert main(["update", "--no-notify"]) == 0
    out = capsys.readouterr().out
    assert "신규 거래 1건" in out and "99억 9,999만원" in out

    # 3) 기존 거래가 해제되면 '해제'로 알린다
    items = fake.items_for(DealType.APT_SALE, GANGNAM, newest)
    victim = next(item for item in items if item["cdealType"].strip() != "O" and item["dealAmount"] != "999,999")
    victim.update(cdealType="O", cdealDay=f"{newest[2:4]}.{newest[4:]}.28")
    fake.replace(DealType.APT_SALE, GANGNAM, newest, items)
    assert main(["update", "--no-notify"]) == 0
    out = capsys.readouterr().out
    assert "해제" in out and victim["aptNm"] in out
    with TransactionStore(tmp_path / "deals.db") as store:
        assert store.count() == expected + 1
