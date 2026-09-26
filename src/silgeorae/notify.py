"""신규·해제 거래 알림 — 메시지 작성과 웹훅(Slack·Discord)·텔레그램 전송.

전송 함수는 실패해도 예외를 내지 않고 경고 로그를 남긴 뒤 ``False`` 를 돌려준다
(알림 실패가 수집 결과를 망치지 않도록). 인증 토큰은 로그에 남기지 않는다.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from typing import Callable, Iterable, Mapping, Optional

from .models import DealType, Transaction

log = logging.getLogger(__name__)

PostFunc = Callable[[str, bytes, Mapping[str, str]], int]
"""``post(url, data, headers) -> HTTP 상태 코드`` (테스트에서 바꿔 끼울 수 있다)."""

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_LIMIT = 4096  # 텔레그램 메시지 최대 글자 수
DISCORD_LIMIT = 2000  # 디스코드 content 최대 글자 수
DEFAULT_TIMEOUT = 10.0

_DEAL_ORDER = {dt: i for i, dt in enumerate(DealType)}


# --------------------------------------------------------------------------- 메시지
def _area(area_m2: Optional[float]) -> str:
    return f"{area_m2:.2f}".rstrip("0").rstrip(".") + "㎡"


def _floor(floor: int) -> str:
    return f"지하{-floor}층" if floor < 0 else f"{floor}층"


def format_transaction_line(tx: Transaction, *, record_high: bool = False) -> str:
    """``[아파트 매매] 강남구 대치동 한빛마을1단지 84.97㎡ 12층 28억 5,000만원 (2025-01-03)``"""
    parts = [f"[{tx.deal_type.label}]"]
    parts.extend(p for p in (tx.sigungu, tx.dong, tx.name) if p)
    if tx.area_m2:
        parts.append(_area(tx.area_m2))
    if tx.floor is not None:
        parts.append(_floor(tx.floor))
    parts.append(tx.price_text)
    when = tx.deal_date.isoformat()
    if tx.is_cancelled:
        when += f", 해제 {tx.cancel_date.isoformat()}" if tx.cancel_date else ", 해제"
    line = " ".join(parts) + f" ({when})"
    if record_high:
        line += " · 신고가"
    return line


def _sort_key(highs: set[str]) -> Callable[[Transaction], tuple]:
    def key(tx: Transaction) -> tuple:
        amount = tx.price if tx.price is not None else (tx.deposit or 0)
        return (tx.key not in highs, -tx.deal_date.toordinal(), -amount)

    return key


def build_message(
    new_txs: Iterable[Transaction],
    *,
    cancelled_txs: Iterable[Transaction] = (),
    title: str = "실거래 알림",
    limit: int = 20,
    record_high_keys: Iterable[str] = (),
) -> str:
    """알림 본문을 만든다.

    첫 줄은 요약(유형별 건수), 이어서 거래 한 줄씩(신고가 먼저, 최근 계약 순) ``limit`` 건까지,
    나머지는 ``· 외 N건`` 으로 줄인다. 전월세는 ``전세 5억원`` / ``월세 1억원/150만원`` 으로 표기한다.
    """
    highs = set(record_high_keys)
    new_list = sorted(new_txs, key=_sort_key(highs))
    cancelled = sorted(cancelled_txs, key=_sort_key(set()))
    limit = max(1, int(limit))
    if not new_list and not cancelled:
        return f"[{title}] 새로 등록되거나 해제된 거래가 없습니다."

    header = f"[{title}] 신규 거래 {len(new_list):,}건"
    counts = Counter(tx.deal_type for tx in new_list)
    if counts:
        by_type = ", ".join(
            f"{dt.label} {n:,}" for dt, n in sorted(counts.items(), key=lambda kv: _DEAL_ORDER[kv[0]])
        )
        header += f" ({by_type})"
    n_highs = sum(1 for tx in new_list if tx.key in highs)
    if n_highs:
        header += f" · 신고가 {n_highs:,}건"
    if cancelled:
        header += f" · 해제 {len(cancelled):,}건"

    lines = [header]
    for tx in new_list[:limit]:
        lines.append("· " + format_transaction_line(tx, record_high=tx.key in highs))
    if len(new_list) > limit:
        lines.append(f"· 외 {len(new_list) - limit:,}건")
    if cancelled:
        lines.append("")
        lines.append(f"해제된 거래 {len(cancelled):,}건")
        for tx in cancelled[:limit]:
            lines.append("· " + format_transaction_line(tx))
        if len(cancelled) > limit:
            lines.append(f"· 외 {len(cancelled) - limit:,}건")
    return "\n".join(lines)


def find_record_highs(candidates: Iterable[Transaction], history: Iterable[Transaction]) -> set[str]:
    """``candidates`` 중 **신고가** 인 매매 거래의 키.

    같은 유형·단지(``complex_key``)·면적타입(``area_type``)에서 계약일이 더 이른
    (해제되지 않은) 거래들의 최고가보다 비싸면 신고가로 본다. 비교할 이전 거래가 없으면 제외.
    """
    previous: dict[tuple, list[tuple[date, int, str]]] = defaultdict(list)
    for tx in history:
        if tx.is_rent or tx.is_cancelled or tx.price is None or tx.area_type is None:
            continue
        previous[(tx.deal_type, tx.complex_key, tx.area_type)].append((tx.deal_date, tx.price, tx.key))
    result: set[str] = set()
    for tx in candidates:
        if tx.is_rent or tx.is_cancelled or tx.price is None or tx.area_type is None:
            continue
        earlier = [
            price
            for deal_date, price, key in previous.get((tx.deal_type, tx.complex_key, tx.area_type), ())
            if deal_date < tx.deal_date and key != tx.key
        ]
        if earlier and tx.price > max(earlier):
            result.add(tx.key)
    return result


# --------------------------------------------------------------------------- 전송
def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _urllib_post(url: str, data: bytes, headers: Mapping[str, str]) -> int:
    request = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _send(post: Optional[PostFunc], url: str, payload: dict, *, what: str, secrets: Iterable[str] = ()) -> bool:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "silgeorae"}
    sender = post or _urllib_post
    try:
        status = int(sender(url, data, headers))
    except Exception as exc:  # 네트워크 오류 등 — 알림 실패는 경고만
        message = str(exc) or exc.__class__.__name__
        for secret in secrets:
            if secret:
                message = message.replace(secret, "***")
        log.warning("%s 알림 전송 실패: %s", what, message)
        return False
    if 200 <= status < 300:
        log.debug("%s 알림 전송 성공 (HTTP %d)", what, status)
        return True
    log.warning("%s 알림 전송 실패: HTTP %d", what, status)
    return False


def send_webhook(url: str, text: str, *, post: Optional[PostFunc] = None) -> bool:
    """Slack·Discord 웹훅으로 보낸다 (``{"text": …, "content": …}`` — 두 서비스 모두 동작)."""
    if not url:
        return False
    host = urllib.parse.urlsplit(url).netloc or "웹훅"
    payload = {"text": text, "content": _clip(text, DISCORD_LIMIT)}
    return _send(post, url, payload, what=f"웹훅({host})", secrets=(url,))


def send_telegram(token: str, chat_id: str, text: str, *, post: Optional[PostFunc] = None) -> bool:
    """텔레그램 봇으로 보낸다 (4096자 넘으면 자른다)."""
    if not token or not chat_id:
        return False
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    payload = {"chat_id": str(chat_id), "text": _clip(text, TELEGRAM_LIMIT), "disable_web_page_preview": True}
    return _send(post, url, payload, what="텔레그램", secrets=(token, url))


def send_all(
    text: str,
    *,
    webhook_url: str = "",
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
    post: Optional[PostFunc] = None,
) -> dict[str, bool]:
    """설정된 채널 모두에 보낸다. 결과: ``{"웹훅": True, "텔레그램": False}`` (설정된 것만)."""
    results: dict[str, bool] = {}
    if webhook_url:
        results["웹훅"] = send_webhook(webhook_url, text, post=post)
    if telegram_bot_token and telegram_chat_id:
        results["텔레그램"] = send_telegram(telegram_bot_token, telegram_chat_id, text, post=post)
    return results
