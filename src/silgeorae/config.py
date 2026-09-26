"""설정 파일(``silgeorae.toml``) 읽기와 인증키 처리.

값의 우선순위: 명령행 옵션(``--key``, ``--db`` …) > 환경변수 > 설정 파일 > 기본값.
설정 파일 안의 상대 경로는 **설정 파일이 있는 폴더**를 기준으로 해석한다.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .errors import ConfigError
from .models import DealType

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 에서는 tomli 사용
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

DEFAULT_CONFIG_NAME = "silgeorae.toml"
"""``--config`` 가 없을 때 현재 폴더에서 찾는 설정 파일 이름."""

DEFAULT_REGIONS_FILE = "regions.csv"
"""``regions --sync`` 결과를 저장하는 기본 파일 이름 (설정 파일 폴더 기준)."""

ENV_SERVICE_KEY = "SILGEORAE_SERVICE_KEY"
ENV_SERVICE_KEY_ALT = "DATA_GO_KR_SERVICE_KEY"
ENV_WEBHOOK_URL = "SILGEORAE_WEBHOOK_URL"
ENV_TELEGRAM_BOT_TOKEN = "SILGEORAE_TELEGRAM_BOT_TOKEN"
ENV_TELEGRAM_CHAT_ID = "SILGEORAE_TELEGRAM_CHAT_ID"

REPORT_FORMATS = ("xlsx", "html", "csv")
_FORMAT_ALIASES = {"excel": "xlsx", "xls": "xlsx", "htm": "html"}

DATA_GO_KR_URL = "https://www.data.go.kr"
REGION_CODE_API_TITLE = "행정안전부_행정표준코드_법정동코드"
"""``regions --sync`` 에 필요한 API (실거래가 API 와 별도로 활용신청해야 한다)."""


def _default_deal_types() -> list[DealType]:
    return [DealType.APT_SALE, DealType.APT_RENT]


@dataclass
class Config:
    """실행 설정. ``load_config`` 로 만들고 명령행 옵션이 있으면 그 값으로 덮어쓴다."""

    service_key: str = ""  # 공공데이터포털 일반 인증키 (Decoding)
    db_path: Path = Path("silgeorae.db")  # SQLite 저장소
    regions: list[str] = field(default_factory=list)  # 관심 지역 (이름 또는 5자리 코드)
    deal_types: list[DealType] = field(default_factory=_default_deal_types)  # 관심 거래 유형
    months: int = 12  # update 가 확인할 기간 (이번 달 포함 최근 N개월)
    refresh_months: int = 3  # 이미 받았어도 매번 다시 받는 최근 개월 수
    report_dir: Path = Path("reports")  # 리포트 저장 폴더
    report_formats: list[str] = field(default_factory=lambda: ["xlsx", "html"])
    report_title: str = "부동산 실거래 정리"
    webhook_url: str = ""  # Slack/Discord 웹훅
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    api_timeout: float = 20.0  # 요청 제한 시간(초)
    api_min_interval: float = 0.0  # 요청 사이 최소 간격(초)
    service_overrides: dict[DealType, str] = field(default_factory=dict)  # 서비스명 교체
    regions_file: Optional[Path] = None  # 있으면 내장 지역코드표 대신 사용
    config_path: Optional[Path] = None  # 읽어 들인 설정 파일 (없으면 None)
    service_key_source: str = ""  # 인증키 출처 설명 (상태 표시용)

    @property
    def notify_configured(self) -> bool:
        """웹훅 또는 텔레그램 알림이 하나라도 설정되어 있는지."""
        return bool(self.webhook_url or self.telegram_configured)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


# --------------------------------------------------------------------------- 불러오기
def load_config(path: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> Config:
    """설정을 읽는다.

    ``path`` 가 None 이면 현재 폴더의 ``silgeorae.toml`` 을 쓰고, 없으면 기본값으로 시작한다.
    ``path`` 를 직접 주었는데 파일이 없으면 ``ConfigError``.
    그 뒤 환경변수(인증키·알림)를 반영한다.
    """
    if env is None:
        env = os.environ
    config_path: Optional[Path] = None
    if path is not None:
        config_path = Path(path).expanduser()
        if not config_path.is_file():
            raise ConfigError(
                f"설정 파일을 찾을 수 없습니다: {config_path}\n"
                f"  'silgeorae init --config {config_path}' 로 새로 만들 수 있습니다."
            )
    else:
        candidate = Path(DEFAULT_CONFIG_NAME)
        if candidate.is_file():
            config_path = candidate

    if config_path is None:
        cfg = Config(regions_file=Path(DEFAULT_REGIONS_FILE))
    else:
        config_path = config_path.resolve()
        data = _read_toml(config_path)
        cfg = _config_from_data(data, where=str(config_path), base_dir=config_path.parent)
        cfg.config_path = config_path
        if cfg.service_key:
            cfg.service_key_source = f"설정 파일 ({config_path.name})"
    _apply_env(cfg, env)
    return cfg


def _read_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:  # pragma: no cover - Python 3.10 + tomli 미설치
        raise ConfigError("설정 파일을 읽으려면 tomli 패키지가 필요합니다: pip install tomli")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"설정 파일을 읽을 수 없습니다: {path} ({exc})") from exc
    try:
        text = raw.decode("utf-8-sig")  # 메모장이 붙이는 BOM 허용
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"설정 파일을 UTF-8 로 읽을 수 없습니다: {path}\n"
            "  메모장이라면 '다른 이름으로 저장' → 인코딩 'UTF-8' 로 다시 저장하세요."
        ) from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        hint = ""
        detail = str(exc).lower()
        if any(word in detail for word in ("escape", "hex value", "unicode scalar")):  # 역슬래시 문제
            hint = "\n  윈도우 경로는 작은따옴표로 감싸거나('C:\\data\\silgeorae.db') 슬래시(/)를 쓰세요."
        raise ConfigError(f"설정 파일 형식(TOML)이 올바르지 않습니다: {path}\n  {exc}{hint}") from exc


_KNOWN_KEYS: dict[str, set[str]] = {
    "api": {"service_key", "timeout", "min_interval", "services"},
    "storage": {"db_path", "regions_file"},
    "watch": {"regions", "deal_types", "months", "refresh_months"},
    "report": {"output_dir", "formats", "title"},
    "notify": {"webhook_url", "telegram_bot_token", "telegram_chat_id"},
}


def _config_from_data(data: Mapping[str, Any], *, where: str, base_dir: Path) -> Config:
    _warn_unknown(data, where)
    cfg = Config()

    api = _section(data, "api", where)
    cfg.service_key = _get_str(api, "api", "service_key", "", where).strip()
    cfg.api_timeout = _get_float(api, "api", "timeout", cfg.api_timeout, where, positive=True)
    cfg.api_min_interval = _get_float(api, "api", "min_interval", cfg.api_min_interval, where)
    services = api.get("services", {})
    if not isinstance(services, Mapping):
        raise _bad(where, "api", "services", "'유형 코드 = \"서비스명\"' 형식의 표여야 합니다")
    for code, service in services.items():
        try:
            deal_type = DealType.parse(str(code))
        except ValueError as exc:
            raise _bad(where, "api.services", str(code), str(exc)) from exc
        if not isinstance(service, str) or not service.strip():
            raise _bad(where, "api.services", str(code), "서비스명(문자열)을 적어야 합니다")
        cfg.service_overrides[deal_type] = service.strip()

    storage = _section(data, "storage", where)
    cfg.db_path = _resolve(_get_str(storage, "storage", "db_path", "silgeorae.db", where), base_dir)
    cfg.regions_file = _resolve(
        _get_str(storage, "storage", "regions_file", DEFAULT_REGIONS_FILE, where), base_dir
    )

    watch = _section(data, "watch", where)
    cfg.regions = _get_str_list(watch, "watch", "regions", [], where)
    if "deal_types" in watch:
        values = _get_str_list(watch, "watch", "deal_types", [], where)
        cfg.deal_types = parse_deal_types(values, where=f"설정 오류 ({where}) [watch] deal_types")
        if not cfg.deal_types:
            raise _bad(where, "watch", "deal_types", "거래 유형을 하나 이상 적어야 합니다")
    cfg.months = _get_int(watch, "watch", "months", cfg.months, where, minimum=1)
    cfg.refresh_months = _get_int(watch, "watch", "refresh_months", cfg.refresh_months, where, minimum=0)

    report = _section(data, "report", where)
    cfg.report_dir = _resolve(_get_str(report, "report", "output_dir", "reports", where), base_dir)
    if "formats" in report:
        values = _get_str_list(report, "report", "formats", [], where)
        cfg.report_formats = normalize_formats(values, where=f"설정 오류 ({where}) [report] formats")
    cfg.report_title = _get_str(report, "report", "title", cfg.report_title, where).strip() or cfg.report_title

    notify = _section(data, "notify", where)
    cfg.webhook_url = _get_str(notify, "notify", "webhook_url", "", where).strip()
    cfg.telegram_bot_token = _get_str(notify, "notify", "telegram_bot_token", "", where).strip()
    cfg.telegram_chat_id = _get_chat_id(notify, where)
    return cfg


def _apply_env(cfg: Config, env: Mapping[str, str]) -> None:
    for name in (ENV_SERVICE_KEY, ENV_SERVICE_KEY_ALT):
        value = (env.get(name) or "").strip()
        if value:
            cfg.service_key = value
            cfg.service_key_source = f"환경변수 {name}"
            break
    webhook = (env.get(ENV_WEBHOOK_URL) or "").strip()
    if webhook:
        cfg.webhook_url = webhook
    token = (env.get(ENV_TELEGRAM_BOT_TOKEN) or "").strip()
    if token:
        cfg.telegram_bot_token = token
    chat_id = (env.get(ENV_TELEGRAM_CHAT_ID) or "").strip()
    if chat_id:
        cfg.telegram_chat_id = chat_id


# --------------------------------------------------------------------------- 값 검사 도우미
def _bad(where: str, section: str, key: str, problem: str) -> ConfigError:
    return ConfigError(f"설정 오류 ({where}) [{section}] {key}: {problem}")


def _warn_unknown(data: Mapping[str, Any], where: str) -> None:
    for section, value in data.items():
        if section not in _KNOWN_KEYS:
            log.warning("알 수 없는 설정 섹션 [%s] 은(는) 무시합니다 (%s)", section, where)
            continue
        if isinstance(value, Mapping):
            for key in value:
                if key not in _KNOWN_KEYS[section]:
                    log.warning("알 수 없는 설정 항목 [%s] %s 은(는) 무시합니다 (%s)", section, key, where)


def _section(data: Mapping[str, Any], name: str, where: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"설정 오류 ({where}): [{name}] 는 섹션(표)이어야 합니다.")
    return value


def _get_str(section: Mapping[str, Any], name: str, key: str, default: str, where: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise _bad(where, name, key, f'문자열이어야 합니다 (따옴표로 감싸세요, 현재 값: {value!r})')
    return value


def _get_chat_id(section: Mapping[str, Any], where: str) -> str:
    value = section.get("telegram_chat_id", "")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _bad(where, "notify", "telegram_chat_id", f"문자열 또는 숫자여야 합니다 (현재 값: {value!r})")
    return str(value).strip()


def _get_int(
    section: Mapping[str, Any], name: str, key: str, default: int, where: str, *, minimum: int
) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(where, name, key, f"정수여야 합니다 (현재 값: {value!r})")
    if value < minimum:
        raise _bad(where, name, key, f"{minimum} 이상이어야 합니다 (현재 값: {value})")
    return value


def _get_float(
    section: Mapping[str, Any], name: str, key: str, default: float, where: str, *, positive: bool = False
) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(where, name, key, f"숫자여야 합니다 (현재 값: {value!r})")
    if positive and value <= 0:
        raise _bad(where, name, key, f"0 보다 커야 합니다 (현재 값: {value})")
    if value < 0:
        raise _bad(where, name, key, f"0 이상이어야 합니다 (현재 값: {value})")
    return float(value)


def _get_str_list(
    section: Mapping[str, Any], name: str, key: str, default: list[str], where: str
) -> list[str]:
    value = section.get(key, default)
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise _bad(where, name, key, f'["값1", "값2"] 형식의 목록이어야 합니다 (현재 값: {value!r})')
    result: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise _bad(where, name, key, f"목록 안의 값은 문자열이어야 합니다 (현재 값: {item!r})")
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _resolve(value: str, base_dir: Path) -> Path:
    path = Path(value.strip()).expanduser()
    return path if path.is_absolute() else base_dir / path


# --------------------------------------------------------------------------- 공개 도우미
def parse_deal_types(values: Iterable[str] | str, *, where: str = "거래 유형") -> list[DealType]:
    """거래 유형 목록을 해석한다 (``DealType.parse_many`` + 한국어 ``ConfigError``)."""
    try:
        return DealType.parse_many(values)
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def normalize_formats(values: Iterable[str] | str, *, where: str = "리포트 형식") -> list[str]:
    """리포트 형식 목록을 ``xlsx`` / ``html`` / ``csv`` 로 정리한다 (별칭 ``excel`` 등 허용)."""
    if isinstance(values, str):
        values = [values]
    result: list[str] = []
    for value in values:
        for token in str(value).split(","):
            name = token.strip().lower().lstrip(".")
            if not name:
                continue
            name = _FORMAT_ALIASES.get(name, name)
            if name not in REPORT_FORMATS:
                raise ConfigError(
                    f"{where}: 알 수 없는 형식 {token.strip()!r} (가능한 값: {', '.join(REPORT_FORMATS)})"
                )
            if name not in result:
                result.append(name)
    if not result:
        raise ConfigError(f"{where}: 형식을 하나 이상 지정하세요 (가능한 값: {', '.join(REPORT_FORMATS)})")
    return result


def mask_secret(value: str, *, keep: int = 4) -> str:
    """비밀 값을 화면에 보일 때 앞뒤 몇 글자만 남긴다 (``abcd…wxyz``)."""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


def service_key_help(
    deal_types: Iterable[DealType] | None = None, *, apis: Iterable[str] | None = None
) -> str:
    """인증키가 없을 때 보여 줄 발급·설정 안내문."""
    titles = list(apis) if apis is not None else [dt.api_title for dt in (deal_types or _default_deal_types())]
    api_lines = "\n".join(f"       - {title}" for title in dict.fromkeys(titles))
    return (
        "공공데이터포털 인증키가 없습니다. 다음 순서로 발급받아 설정하세요.\n"
        f"  1) 공공데이터포털({DATA_GO_KR_URL}) 회원가입 후 로그인\n"
        "  2) 아래 API 를 검색해 각각 '활용신청' (개발계정은 자동 승인)\n"
        f"{api_lines}\n"
        "  3) 마이페이지 → 데이터활용 → Open API → 활용신청 현황에서 '일반 인증키(Decoding)' 복사\n"
        f"  4) {DEFAULT_CONFIG_NAME} 의 [api] service_key 에 붙여 넣거나\n"
        f"     환경변수 {ENV_SERVICE_KEY} 로 설정 (한 번만 쓸 때는 --key 옵션)\n"
        "  ※ 승인 직후에는 키가 동작하기까지 최대 1시간 정도 걸릴 수 있습니다.\n"
        "  ※ 인증키 없이 먼저 둘러보려면: silgeorae demo"
    )


def require_service_key(
    cfg: Config,
    cli_key: str | None = None,
    *,
    deal_types: Iterable[DealType] | None = None,
    apis: Iterable[str] | None = None,
) -> str:
    """사용할 인증키를 돌려준다. 우선순위: ``--key`` > 환경변수 > 설정 파일.

    없으면 발급 절차를 담은 ``ConfigError`` 를 낸다.
    """
    key = (cli_key or "").strip() or (cfg.service_key or "").strip()
    if key:
        return key
    raise ConfigError(service_key_help(deal_types or cfg.deal_types, apis=apis))


def default_config_text() -> str:
    """``silgeorae init`` 이 만드는 설정 파일 내용 (``config.example.toml`` 과 같다)."""
    return _TEMPLATE


_TEMPLATE = """\
# silgeorae 설정 파일
# =============================================================================
# 'silgeorae init' 으로 만든 기본 설정입니다. 필요한 곳만 고쳐 쓰세요.
#  - '#' 뒤는 주석입니다. 값을 바꾼 뒤 따로 저장만 하면 됩니다.
#  - 상대 경로는 이 파일이 있는 폴더를 기준으로 합니다.
#  - 윈도우 경로는 작은따옴표로 감싸거나('C:\\data\\silgeorae.db') 슬래시(/)를 쓰세요.
#  - 명령행 옵션(--key, --db 등)이 이 파일보다 우선합니다.

[api]
# 공공데이터포털(data.go.kr) '일반 인증키(Decoding)' 를 붙여 넣으세요.
# 파일에 적기 싫다면 비워 두고 환경변수 SILGEORAE_SERVICE_KEY 를 쓰거나
# 명령마다 --key 옵션을 붙여도 됩니다. (우선순위: --key > 환경변수 > 이 파일)
service_key = ""
# 요청 한 번의 제한 시간(초)
timeout = 20
# 요청 사이 최소 간격(초). 0 이면 쉬지 않고 요청합니다.
min_interval = 0

[api.services]
# API 서비스명이 바뀌었을 때만 '유형 코드 = "서비스명"' 형식으로 적습니다.
# apt_sale = "RTMSDataSvcAptTradeDev"

[storage]
# 수집한 거래를 모아 두는 SQLite 파일
db_path = "silgeorae.db"
# 'silgeorae regions --sync' 로 받은 최신 지역코드표. 파일이 있으면 내장 표 대신 씁니다.
regions_file = "regions.csv"

[watch]
# 'silgeorae update' 가 매번 확인할 관심 지역 (시군구 이름 또는 5자리 코드)
#   예: regions = ["강남구", "마포구", "성남시 분당구", "11650"]
#   '중구'처럼 여러 곳에 있는 이름은 "서울특별시 중구" 처럼 시도를 붙이거나 코드를 쓰세요.
#   지역 이름·코드 찾기: silgeorae regions 검색어
regions = []
# 거래 유형 (한글도 가능: "아파트", "아파트 전월세", "오피스텔" …)
#   apt_sale  아파트 매매        apt_rent  아파트 전월세
#   offi_sale 오피스텔 매매      offi_rent 오피스텔 전월세
#   rh_sale   연립다세대 매매    rh_rent   연립다세대 전월세
#   sh_sale   단독/다가구 매매   sh_rent   단독/다가구 전월세
#   presale   분양·입주권 전매   land      토지
#   commercial 상업업무용        industrial 공장·창고
#   "housing" = 주거용 8종, "all" = 전체 12종
#   유형마다 공공데이터포털에서 해당 API 를 따로 활용신청해야 합니다.
deal_types = ["apt_sale", "apt_rent"]
# update 가 확인할 기간 (이번 달을 포함한 최근 N개월)
months = 12
# 최근 몇 개월은 이미 받았어도 매번 다시 받습니다.
# (계약 후 30일 안에 신고되고, 해제·등기 정보가 나중에 반영되기 때문)
refresh_months = 3

[report]
# 리포트 파일을 저장할 폴더
output_dir = "reports"
# 만들 형식: "xlsx"(엑셀, openpyxl 필요), "html", "csv"
formats = ["xlsx", "html"]
# 리포트 제목
title = "부동산 실거래 정리"

[notify]
# 'silgeorae update' 가 새 거래·해제 거래를 찾으면 보낼 알림 (비워 두면 보내지 않음)
# Slack 또는 Discord 웹훅 주소
webhook_url = ""
# 텔레그램: @BotFather 로 만든 봇 토큰과 메시지를 받을 채팅 ID
telegram_bot_token = ""
telegram_chat_id = ""
"""
