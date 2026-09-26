import json
from datetime import date

from silgeorae import notify
from silgeorae.models import DealType, Transaction
from silgeorae.notify import build_message, find_record_highs, send_all, send_telegram, send_webhook


def make_tx(**kw):
    base = dict(
        deal_type=DealType.APT_SALE, lawd_cd="11680", deal_date=date(2025, 1, 3), sigungu="강남구",
        dong="대치동", jibun="316", name="한빛마을1단지", floor=12, area_m2=84.97, price=285000,
    )
    base.update(kw)
    return Transaction(**base)


def jeonse(**kw):
    kw.setdefault("deal_date", date(2025, 1, 8))
    return make_tx(deal_type=DealType.APT_RENT, price=None, deposit=150000, monthly_rent=0, **kw)


class FakePost:
    def __init__(self, status=200, exc=None):
        self.status, self.exc, self.calls = status, exc, []

    def __call__(self, url, data, headers):
        self.calls.append((url, data, dict(headers)))
        if self.exc:
            raise self.exc
        return self.status


def test_message_lines_and_counts():
    wolse = make_tx(deal_type=DealType.APT_RENT, price=None, deposit=10000, monthly_rent=150,
                    deal_date=date(2025, 1, 2), floor=-1)
    text = build_message([make_tx(), jeonse(), wolse])
    lines = text.split("\n")
    assert lines[0] == "[실거래 알림] 신규 거래 3건 (아파트 매매 1, 아파트 전월세 2)"
    assert "· [아파트 매매] 강남구 대치동 한빛마을1단지 84.97㎡ 12층 28억 5,000만원 (2025-01-03)" in lines
    assert "· [아파트 전월세] 강남구 대치동 한빛마을1단지 84.97㎡ 12층 전세 15억원 (2025-01-08)" in lines
    assert "· [아파트 전월세] 강남구 대치동 한빛마을1단지 84.97㎡ 지하1층 월세 1억원/150만원 (2025-01-02)" in lines
    assert lines[1].endswith("(2025-01-08)")  # 최근 계약부터


def test_message_limit_and_cancelled_section():
    news = [make_tx(floor=i, deal_date=date(2025, 1, 1 + i)) for i in range(1, 26)]
    cancelled = [make_tx(area_m2=114.8, price=312000, is_cancelled=True, cancel_date=date(2025, 1, 28))]
    text = build_message(news, cancelled_txs=cancelled, title="강남 알림", limit=20)
    lines = text.split("\n")
    assert lines[0].startswith("[강남 알림] 신규 거래 25건") and lines[0].endswith("· 해제 1건")
    assert sum(1 for line in lines if line.startswith("· [")) == 21
    assert "· 외 5건" in lines
    assert "해제된 거래 1건" in lines
    assert lines[-1] == "· [아파트 매매] 강남구 대치동 한빛마을1단지 114.8㎡ 12층 31억 2,000만원 (2025-01-03, 해제 2025-01-28)"


def test_message_when_nothing_new():
    assert build_message([]) == "[실거래 알림] 새로 등록되거나 해제된 거래가 없습니다."


def test_record_highs():
    old = make_tx(deal_date=date(2024, 11, 2), price=270000)
    older_cancelled = make_tx(deal_date=date(2024, 12, 2), price=400000, is_cancelled=True)
    high = make_tx(deal_date=date(2025, 1, 3), price=285000)
    lower = make_tx(deal_date=date(2025, 1, 5), price=260000, floor=3)
    other_area = make_tx(deal_date=date(2025, 1, 6), price=500000, area_m2=114.8)  # 비교 대상 없음
    rent = jeonse()
    keys = find_record_highs([high, lower, other_area, rent], [old, older_cancelled, high, lower, other_area, rent])
    assert keys == {high.key}
    text = build_message([lower, high], record_high_keys=keys)
    assert "· 신고가 1건" in text.split("\n")[0]
    assert text.split("\n")[1].endswith("(2025-01-03) · 신고가")  # 신고가가 맨 위


def test_send_webhook_payload_for_slack_and_discord():
    post = FakePost(204)
    long_text = "가" * 2500
    assert send_webhook("https://hooks.example.invalid/T/B/X", long_text, post=post) is True
    url, data, headers = post.calls[0]
    payload = json.loads(data.decode("utf-8"))
    assert url == "https://hooks.example.invalid/T/B/X"
    assert payload["text"] == long_text  # Slack 은 전체
    assert len(payload["content"]) == 2000 and payload["content"].endswith("…")  # Discord 한도
    assert headers["Content-Type"].startswith("application/json")


def test_send_webhook_failures_return_false(caplog):
    url = "https://hooks.example.invalid/SECRET-PATH"
    with caplog.at_level("WARNING", logger="silgeorae.notify"):
        assert send_webhook(url, "x", post=FakePost(500)) is False
        assert send_webhook(url, "x", post=FakePost(exc=OSError(f"연결 실패 {url}"))) is False
    assert "HTTP 500" in caplog.text and "연결 실패" in caplog.text
    assert "SECRET-PATH" not in caplog.text
    assert send_webhook("", "x", post=FakePost()) is False


def test_send_telegram_payload_and_truncation():
    post = FakePost(200)
    assert send_telegram("123:TOKEN", "42", "나" * 5000, post=post) is True
    url, data, _ = post.calls[0]
    payload = json.loads(data.decode("utf-8"))
    assert url == "https://api.telegram.org/bot123:TOKEN/sendMessage"
    assert payload["chat_id"] == "42"
    assert len(payload["text"]) == 4096


def test_send_telegram_failure_hides_token(caplog):
    with caplog.at_level("WARNING", logger="silgeorae.notify"):
        ok = send_telegram("123:TOKEN", "42", "hi", post=FakePost(exc=RuntimeError("bad url bot123:TOKEN")))
    assert ok is False
    assert "123:TOKEN" not in caplog.text and "***" in caplog.text
    assert send_telegram("", "42", "hi", post=FakePost()) is False


def test_send_all_only_uses_configured_channels():
    post = FakePost(200)
    assert send_all("hi", post=post) == {}
    result = send_all("hi", webhook_url="https://hooks.example.invalid/x", telegram_bot_token="t",
                      telegram_chat_id="1", post=post)
    assert result == {"웹훅": True, "텔레그램": True} and len(post.calls) == 2


def test_default_post_is_used_when_not_given(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_urllib_post", lambda url, data, headers: calls.append(url) or 200)
    assert send_webhook("https://hooks.example.invalid/y", "hi") is True
    assert calls == ["https://hooks.example.invalid/y"]
