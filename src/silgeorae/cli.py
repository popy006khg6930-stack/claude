"""명령행 인터페이스 — ``silgeorae <명령> [옵션]``.

명령: init · regions · collect · update · report · new · search · status · demo

종료 코드: 0 성공 · 1 실행 중 오류(API 오류·수집 중단·결과 없음 등)
· 2 설정·사용법·지역 오류 · 130 사용자 중단(Ctrl+C)

다른 팀 모듈(regions, collector, processing, analysis)은 명령을 실행할 때 불러온다.
그래서 그 모듈에 문제가 있어도 ``silgeorae --help`` 는 동작한다.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import sys
import traceback
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, NoReturn, Optional, Sequence

from . import __version__
from .config import (
    DEFAULT_CONFIG_NAME,
    DEFAULT_REGIONS_FILE,
    REGION_CODE_API_TITLE,
    Config,
    default_config_text,
    load_config,
    mask_secret,
    normalize_formats,
    parse_deal_types,
    require_service_key,
)
from .console import format_summary, format_table, format_task_line, format_transactions, terminal_width
from .errors import AmbiguousRegionError, ConfigError, ExportError, RegionError, SilgeoraeError
from .models import DealType, Transaction
from .utils import KST, month_range, now_kst, parse_ym, today_kst, ym_add, ym_label, ym_of

log = logging.getLogger("silgeorae")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

DEFAULT_HISTORY_MONTHS = 36
DEFAULT_LIMIT = 50


# =========================================================================== argparse (한국어)
class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage: Any, actions: Any, groups: Any, prefix: Optional[str] = None) -> None:
        super().add_usage(usage, actions, groups, "사용법: " if prefix is None else prefix)


_ARGPARSE_MESSAGES = [
    (r"^the following arguments are required: (.+)$", r"다음 인자가 필요합니다: \1"),
    (r"^unrecognized arguments: (.+)$", r"알 수 없는 인자입니다: \1"),
    (r"^argument (.+?): expected one argument$", r"\1: 값이 필요합니다"),
    (r"^argument (.+?): expected at least one argument$", r"\1: 값이 하나 이상 필요합니다"),
    (r"^argument (.+?): not allowed with argument (.+)$", r"\1: \2 와(과) 함께 쓸 수 없습니다"),
    (r"^argument (.+?): invalid choice: (.+?) \(choose from (.+)\)$", r"\1: 잘못된 값 \2 (가능한 값: \3)"),
    (r"^argument (.+?): invalid \S+ value: (.+)$", r"\1: 올바르지 않은 값 \2"),
    (r"^argument (.+?): ignored explicit argument (.+)$", r"\1: 값을 받지 않는 옵션입니다 (\2)"),
    (r"^ambiguous option: (.+?) could match (.+)$", r"옵션 \1 이(가) 모호합니다 (후보: \2)"),
    (r"^argument (.+?): (.+)$", r"\1: \2"),
]


def _translate_argparse(message: str) -> str:
    for pattern, replacement in _ARGPARSE_MESSAGES:
        if re.match(pattern, message):
            return re.sub(pattern, replacement, message)
    return message


class _Parser(argparse.ArgumentParser):
    """도움말·오류 메시지를 한국어로 보여 주는 ArgumentParser."""

    def __init__(self, *args: Any, add_help: bool = True, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", _HelpFormatter)
        super().__init__(*args, add_help=False, **kwargs)
        self._positionals.title = "위치 인자"
        self._optionals.title = "옵션"
        if add_help:
            self.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS, help="이 도움말을 보여 줍니다.")

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"오류: {_translate_argparse(message)}\n  자세한 사용법: {self.prog} -h\n")


def _ym_arg(text: str) -> str:
    try:
        return parse_ym(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"1 이상의 정수를 입력하세요: {text!r}")
    return value


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = -1
    if value < 0:
        raise argparse.ArgumentTypeError(f"0 이상의 정수를 입력하세요: {text!r}")
    return value


def _float_arg(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"숫자를 입력하세요: {text!r}") from None


def _date_arg(text: str) -> date:
    cleaned = text.strip().replace(".", "-").replace("/", "-")
    if re.fullmatch(r"\d{8}", cleaned):
        cleaned = f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:]}"
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜 형식이 올바르지 않습니다: {text!r} (예: 2025-01-31)") from None


def _add_common_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """전역 옵션. 명령 앞(``silgeorae --db x status``)과 뒤(``silgeorae status --db x``) 모두 받는다."""
    group = parser.add_argument_group("공통 옵션")
    default: Any = argparse.SUPPRESS if suppress else None
    group.add_argument(
        "--config", metavar="PATH", default=default,
        help=f"설정 파일 경로 (기본: 현재 폴더의 {DEFAULT_CONFIG_NAME}, 없으면 기본값)",
    )
    group.add_argument("--db", metavar="PATH", default=default, help="거래 DB(SQLite) 경로 (설정 파일의 db_path 대신)")
    group.add_argument("--key", metavar="KEY", default=default, help="공공데이터포털 인증키 (설정 파일·환경변수보다 우선)")
    group.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS if suppress else False,
        help="자세한 진행 상황과 로그를 보여 줍니다.",
    )


def _add_filter_options(parser: argparse.ArgumentParser, *, region_help: str, type_help: str) -> None:
    parser.add_argument("-r", "--region", action="append", metavar="지역", help=region_help)
    parser.add_argument("-t", "--type", action="append", metavar="유형", help=type_help)


def _add_period_options(parser: argparse.ArgumentParser, *, months_help: Optional[str]) -> None:
    parser.add_argument("--from", dest="start", type=_ym_arg, metavar="YYYY-MM", help="시작 연월 (예: 2024-01)")
    parser.add_argument("--to", dest="end", type=_ym_arg, metavar="YYYY-MM", help="끝 연월 (기본: 이번 달)")
    if months_help:
        parser.add_argument("--months", type=_positive_int, metavar="N", help=months_help)


_REGION_HELP = "지역 이름 또는 5자리 코드. 여러 번 쓰거나 쉼표로 구분 (예: -r 강남구,마포구)"
_TYPE_HELP = "거래 유형 (예: apt_sale, 아파트, '아파트 전월세', 오피스텔, housing, all). 여러 번 또는 쉼표로 구분"

_DESCRIPTION = """\
국토교통부 부동산 실거래가를 자동으로 수집·정리합니다.

처음이라면:
  silgeorae demo                  가상 데이터로 전체 흐름 체험 (인증키·인터넷 불필요)
  silgeorae init                  설정 파일(silgeorae.toml) 만들기
  silgeorae collect -r 강남구     실거래 수집 (공공데이터포털 인증키 필요)
  silgeorae report -r 강남구      엑셀·HTML 리포트 만들기
  silgeorae update --report       관심 지역의 새 거래 확인 + 알림 + 리포트 (매일 자동 실행용)"""

_EPILOG = """\
명령별 도움말: silgeorae <명령> -h
종료 코드: 0 성공 · 1 실행 중 오류(API 오류·수집 중단 등) · 2 설정·입력 오류 · 130 중단"""


def build_parser() -> argparse.ArgumentParser:
    """명령행 파서를 만든다."""
    parser = _Parser(prog="silgeorae", description=_DESCRIPTION, epilog=_EPILOG)
    parser.add_argument("--version", action="version", version=f"silgeorae {__version__}", help="버전을 보여 줍니다.")
    _add_common_options(parser, suppress=False)
    sub = parser.add_subparsers(title="명령", dest="command", metavar="<명령>")

    # init ------------------------------------------------------------------
    p = sub.add_parser(
        "init", help="설정 파일(silgeorae.toml) 만들기",
        description="주석이 달린 설정 파일을 만듭니다 (--config 로 경로 지정, --key 를 주면 인증키도 채움).",
    )
    p.add_argument("--force", action="store_true", help="이미 있는 파일을 덮어씁니다.")
    _add_common_options(p, suppress=True)
    p.set_defaults(handler=cmd_init)

    # regions ---------------------------------------------------------------
    p = sub.add_parser(
        "regions", help="지역(시군구) 이름·코드 찾기",
        description=(
            "시군구 이름과 5자리 코드(LAWD_CD)를 찾습니다. 수집할 때 -r 에 이름이나 코드를 씁니다.\n"
            "예: silgeorae regions 강남 / silgeorae regions 분당 / silgeorae regions --all"
        ),
    )
    p.add_argument("query", nargs="?", metavar="검색어", help="찾을 지역 이름 또는 코드 (예: 강남, 성남, 11680)")
    p.add_argument("--all", action="store_true", help="전체 목록을 보여 줍니다.")
    p.add_argument(
        "--sync", action="store_true",
        help=f"행정안전부 법정동코드 API 로 최신 지역코드표를 받아 저장합니다 (인증키와 '{REGION_CODE_API_TITLE}' 활용신청 필요).",
    )
    _add_common_options(p, suppress=True)
    p.set_defaults(handler=cmd_regions)

    # collect ---------------------------------------------------------------
    p = sub.add_parser(
        "collect", help="실거래 데이터 수집",
        description=(
            "지정한 지역·유형·기간의 실거래를 받아 DB 에 저장합니다.\n"
            "최근 몇 개월(설정 refresh_months, 기본 3)은 매번 다시 받고, 그 이전에 이미 받은 달은 건너뜁니다.\n"
            "예: silgeorae collect -r 강남구 -r 마포구 -t 아파트,아파트전월세 --months 24"
        ),
    )
    _add_filter_options(
        p, region_help=_REGION_HELP + ". 생략하면 설정 파일의 [watch] regions",
        type_help=_TYPE_HELP + ". 생략하면 설정 파일의 deal_types (기본: 아파트 매매·전월세)",
    )
    _add_period_options(p, months_help="이번 달을 포함한 최근 N개월 (기본: 설정 파일 months, 12)")
    p.add_argument("--force", action="store_true", help="이미 받은 달도 모두 다시 받습니다.")
    _add_common_options(p, suppress=True)
    p.set_defaults(handler=cmd_collect)

    # update ----------------------------------------------------------------
    p = sub.add_parser(
        "update", help="관심 지역 새 거래 확인 (+알림, +리포트)",
        description=(
            "설정 파일의 [watch] 지역·유형을 수집하고, 새로 등록·해제된 거래를 보여 줍니다.\n"
            "알림([notify])이 설정되어 있으면 새 소식이 있을 때 웹훅·텔레그램으로 보냅니다.\n"
            "매일 자동 실행(cron·작업 스케줄러·GitHub Actions)에 쓰는 명령입니다."
        ),
    )
    p.add_argument("--months", type=_positive_int, metavar="N", help="확인할 기간 (기본: 설정 파일 months, 12)")
    p.add_argument(
        "--notify", action=argparse.BooleanOptionalAction, default=None,
        help="알림을 보낼지 여부 (기본: 알림 설정이 있고 새 소식이 있으면 보냄)",
    )
    p.add_argument("--report", action="store_true", help="수집 후 리포트 파일도 만듭니다 ([report] 설정 사용).")
    _add_common_options(p, suppress=True)
    p.set_defaults(handler=cmd_update)

    # report ----------------------------------------------------------------
    p = sub.add_parser(
        "report", help="엑셀·HTML·CSV 리포트 만들기",
        description=(
            "DB 에 모인 거래로 요약 리포트를 만듭니다. 지역·유형을 생략하면 DB 에 있는 전체가 대상입니다.\n"
            "예: silgeorae report -r 강남구 --months 24 --format xlsx,html"
        ),
    )
    _add_filter_options(p, region_help=_REGION_HELP, type_help=_TYPE_HELP)
    _add_period_options(p, months_help="이번 달을 포함한 최근 N개월 (기본: 설정 파일 months, 12)")
    p.add_argument("--format", action="append", metavar="형식", help="xlsx, html, csv 중 선택, 쉼표로 여러 개 (기본: 설정 파일 formats)")
    p.add_argument("--out", metavar="DIR", help="저장할 폴더 (기본: 설정 파일 output_dir, reports)")
    p.add_argument("--title", metavar="제목", help="리포트 제목")
    p.add_argument(
        "--history-months", type=_non_negative_int, default=DEFAULT_HISTORY_MONTHS, metavar="N",
        help=f"신고가 비교 등에 함께 쓸 이전 기간(개월, 기본: {DEFAULT_HISTORY_MONTHS})",
    )
    p.add_argument(
        "--new-days", type=_non_negative_int, default=7, metavar="N",
        help="최근 N일 안에 새로 수집된 거래를 '신규 등록 거래'로 표시 (기본: 7)",
    )
    _add_common_options(p, suppress=True)
    p.set_defaults(handler=cmd_report)

    # new -------------------------------------------------------------------
    p = sub.add_parser(
        "new", help="새로 수집된 거래 보기",
        description="최근에 처음 수집된(새로 신고된) 거래를 보여 줍니다. 예: silgeorae new --days 7 -r 강남구",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--since", type=_date_arg, metavar="YYYY-MM-DD", help="이 날짜 0시 이후 처음 수집된 거래")
    group.add_argument("--days", type=_positive_int, default=1, metavar="N", help="최근 N일 안에 처음 수집된 거래 (기본: 1)")
    _add_filter_options(p, region_help=_REGION_HELP, type_help=_TYPE_HELP)
    p.add_argument("--limit", type=_positive_int, default=DEFAULT_LIMIT, metavar="N", help=f"화면에 보여 줄 최대 건수 (기본: {DEFAULT_LIMIT})")
    _add_common_options(p, suppress=True)
    p.set_defaults(handler=cmd_new)

    # search ----------------------------------------------------------------
    p = sub.add_parser(
        "search", help="저장된 거래 검색",
        description="DB 에서 조건에 맞는 거래를 찾습니다 (최근 계약 순). 예: silgeorae search -r 강남구 --name 한빛 --min-area 80",
    )
    _add_filter_options(p, region_help=_REGION_HELP, type_help=_TYPE_HELP)
    p.add_argument("--name", metavar="이름", help="단지·건물 이름 (일부만 적어도 됨)")
    p.add_argument("--dong", metavar="법정동", help="법정동 이름 (예: 대치동)")
    _add_period_options(p, months_help=None)
    p.add_argument("--min-area", type=_float_arg, metavar="㎡", help="최소 면적(㎡)")
    p.add_argument("--max-area", type=_float_arg, metavar="㎡", help="최대 면적(㎡)")
    p.add_argument("--include-cancelled", action="store_true", help="해제된 거래도 포함합니다.")
    p.add_argument("--limit", type=_positive_int, default=DEFAULT_LIMIT, metavar="N", help=f"화면에 보여 줄 최대 건수 (기본: {DEFAULT_LIMIT})")
    p.add_argument("--csv", metavar="PATH", help="검색 결과 전체를 CSV 파일로 저장합니다 (엑셀에서 바로 열림).")
    _add_common_options(p, suppress=True)
    p.set_defaults(handler=cmd_search)

    # status ----------------------------------------------------------------
    p = sub.add_parser(
        "status", help="설정·DB·최근 수집 상태 보기",
        description="설정 파일, 인증키 설정 여부, DB 에 저장된 거래 수, 최근 수집 기록을 보여 줍니다.",
    )
    _add_common_options(p, suppress=True)
    p.set_defaults(handler=cmd_status)

    # demo ------------------------------------------------------------------
    p = sub.add_parser(
        "demo", help="가상 데이터로 전체 흐름 체험 (인증키 불필요)",
        description=(
            "가상 단지의 합성 실거래 데이터로 수집 → 저장 → 리포트를 실행합니다. 인증키·인터넷이 필요 없습니다.\n"
            "※ 단지명·지번·가격은 모두 가상 데이터입니다."
        ),
    )
    p.add_argument("--out", metavar="DIR", default="demo_output", help="결과를 저장할 폴더 (기본: demo_output)")
    p.add_argument("--months", type=_positive_int, default=24, metavar="N", help="만들 기간(개월, 기본: 24)")
    _add_common_options(p, suppress=True)
    p.set_defaults(handler=cmd_demo)
    return parser


# =========================================================================== 공통 도우미
def _out(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _load_config(args: argparse.Namespace) -> Config:
    cfg = load_config(args.config)
    if args.db:
        cfg.db_path = Path(args.db).expanduser()
    return cfg


def _split_values(values: Optional[Sequence[str]]) -> list[str]:
    """``-r 강남구 -r 마포구,서초구`` → ``["강남구", "마포구", "서초구"]``."""
    result: list[str] = []
    for value in values or ():
        for token in str(value).split(","):
            token = token.strip()
            if token and token not in result:
                result.append(token)
    return result


def _deal_types(values: Optional[Sequence[str]]) -> list[DealType]:
    tokens = _split_values(values)
    return parse_deal_types(tokens, where="거래 유형(-t)") if tokens else []


def _region_table(cfg: Config) -> Any:
    from .regions import RegionTable

    if cfg.regions_file is not None and Path(cfg.regions_file).is_file():
        log.debug("지역코드표 파일 사용: %s", cfg.regions_file)
        return RegionTable.load(cfg.regions_file)
    return RegionTable.load()


def _filter_codes(table: Any, values: Optional[Sequence[str]]) -> Optional[list[str]]:
    queries = _split_values(values)
    if not queries:
        return None
    return [region.lawd_cd for region in table.resolve_many(queries)]


def _region_label(table: Any, code: str, *, short: bool = False) -> str:
    region = table.get(code)
    if region is None:
        return code
    return region.short_name if short else region.name


def _period(args: argparse.Namespace, cfg: Config) -> tuple[str, str]:
    """``--from/--to/--months`` → (시작, 끝) 연월. 기본은 이번 달까지 최근 N개월."""
    current = ym_of(today_kst())
    months = getattr(args, "months", None) or cfg.months
    start: Optional[str] = getattr(args, "start", None)
    end: Optional[str] = getattr(args, "end", None)
    if end is None:
        end = current if start is None or start <= current else start
    if start is None:
        start = ym_add(end, -(months - 1))
    if start > end:
        raise ConfigError(f"시작 연월({ym_label(start)})이 끝 연월({ym_label(end)})보다 늦습니다.")
    return start, end


def _open_store(cfg: Config) -> Any:
    from .processing import TransactionStore

    path = Path(cfg.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return TransactionStore(path)


def _open_existing_store(cfg: Config) -> Any:
    """DB 파일이 있을 때만 연다 (조회 명령이 빈 DB 파일을 만들지 않도록). 없으면 안내 후 None."""
    if not Path(cfg.db_path).is_file():
        _err(
            f"아직 수집한 데이터가 없습니다 (DB: {cfg.db_path}).\n"
            "  먼저 'silgeorae collect -r 강남구' 로 수집하거나 'silgeorae demo' 로 데모를 실행해 보세요.\n"
            "  데모 DB 를 보려면: silgeorae --db demo_output/demo.db status"
        )
        return None
    return _open_store(cfg)


def _make_client(cfg: Config, key: str) -> Any:
    from .collector import MolitClient

    return MolitClient(
        key,
        timeout=cfg.api_timeout,
        min_interval=cfg.api_min_interval,
        service_overrides=cfg.service_overrides or None,
        **({"base_url": cfg.api_base_url} if cfg.api_base_url else {}),
    )


def _quote(path: Path | str) -> str:
    text = str(path)
    return f'"{text}"' if any(ch.isspace() for ch in text) else text


def _recent_first(tx: Transaction) -> tuple:
    amount = tx.price if tx.price is not None else (tx.deposit or 0)
    return (-tx.deal_date.toordinal(), -amount)


# =========================================================================== init
def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser() if args.config else Path(DEFAULT_CONFIG_NAME)
    if path.exists() and not args.force:
        _err(f"오류: 설정 파일이 이미 있습니다: {path}\n  덮어쓰려면 --force 를 붙이세요.")
        return EXIT_USAGE
    text = default_config_text()
    if args.key:
        text = text.replace('service_key = ""', f"service_key = {json.dumps(args.key.strip())}", 1)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise ConfigError(f"설정 파일을 쓸 수 없습니다: {path} ({exc})") from exc
    _out(f"설정 파일을 만들었습니다: {path.resolve()}")
    _out("다음 단계:")
    if args.key:
        _out("  1) 인증키는 입력되었습니다.")
    else:
        _out("  1) 파일을 열어 [api] service_key 에 공공데이터포털 '일반 인증키(Decoding)' 를 넣고")
        _out("     (또는 환경변수 SILGEORAE_SERVICE_KEY 사용 — 발급 방법은 README.md '인증키 발급')")
    _out('  2) [watch] regions 에 관심 지역을 적은 뒤 (예: regions = ["강남구", "마포구"])')
    _out("  3) silgeorae update --report 를 실행하세요.")
    return EXIT_OK


# =========================================================================== regions
def cmd_regions(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    if args.sync:
        from .collector import sync_regions

        key = require_service_key(cfg, args.key, apis=[REGION_CODE_API_TITLE])
        _out("행정안전부 법정동코드 API 에서 최신 시군구 목록을 받는 중입니다…")
        table = sync_regions(key)
        target = Path(cfg.regions_file) if cfg.regions_file is not None else Path(DEFAULT_REGIONS_FILE)
        target.parent.mkdir(parents=True, exist_ok=True)
        table.save(target)
        _out(f"지역코드표를 저장했습니다: {target} (조회 단위 시군구 {len(table.all(leaves_only=True)):,}곳)")
        _out("이제부터 내장 지역코드표 대신 이 파일을 씁니다 (지우면 내장 표로 돌아갑니다).")
        if not args.query and not args.all:
            return EXIT_OK
    else:
        table = _region_table(cfg)

    headers = ["코드", "시도", "시군구", "조회단위", "비고"]
    if args.query:
        found = table.search(args.query)
        if not found:
            _out(f"'{args.query}' 에 해당하는 지역이 없습니다. 다른 이름으로 찾아보세요 (예: 강남, 분당, 수원).")
            return EXIT_OK
        _out(format_table(headers, _region_rows(found), align="lllll", max_width=40))
        _out(f"\n{len(found):,}곳. 수집할 때 이름이나 코드를 -r 에 쓰세요 (예: silgeorae collect -r {found[0].lawd_cd}).")
        if any(not region.is_leaf for region in found):
            _out("'하위 구로 조회' 인 시는 이름을 쓰면 소속 구 전체를 수집합니다.")
        return EXIT_OK
    if args.all:
        regions = table.all()
        _out(format_table(headers, _region_rows(regions), align="lllll", max_width=40))
        _out(f"\n전체 {len(regions):,}곳 (조회 단위 {len(table.all(leaves_only=True)):,}곳)")
        return EXIT_OK

    counts: Counter[str] = Counter()
    for region in table.all(leaves_only=True):
        counts[region.sido] += 1
    _out(format_table(["시도", "시군구 수"], list(counts.items()), align="lr"))
    _out(f"\n조회 단위 시군구 {sum(counts.values()):,}곳.")
    _out("검색: silgeorae regions 강남   /   전체 목록: silgeorae regions --all")
    _out("최신화: silgeorae regions --sync (행정구역 개편 반영, 인증키 필요)")
    return EXIT_OK


def _region_rows(regions: Sequence[Any]) -> list[list[str]]:
    rows = []
    for region in regions:
        note = region.note or ""
        if region.former_cd:
            note = f"{note} (옛 코드 {region.former_cd})".strip()
        rows.append(
            [region.lawd_cd, region.sido, region.sigungu or "-", "O" if region.is_leaf else "하위 구로 조회", note]
        )
    return rows


# =========================================================================== collect / update
def _collect(
    cfg: Config,
    store: Any,
    client: Any,
    table: Any,
    regions: Sequence[Any],
    types: Sequence[DealType],
    months: Sequence[str],
    *,
    force: bool,
    verbose: bool,
) -> Any:
    from .pipeline import plan_tasks, run_collect

    names = {region.lawd_cd: region.name for region in regions}
    tasks = plan_tasks(types, list(names), months)
    _out(
        f"수집 대상: 지역 {len(names)}곳 × 유형 {len(types)}종 × {len(months)}개월 "
        f"({ym_label(months[0])}~{ym_label(months[-1])}) = 작업 {len(tasks):,}건"
    )
    _out(f"  지역: {', '.join(names.values())}")
    _out(f"  유형: {', '.join(dt.label for dt in types)}")
    if force:
        _out("  --force: 이미 받은 달도 모두 다시 받습니다.")
    else:
        _out(f"  최근 {cfg.refresh_months}개월은 매번 다시 받고, 그 이전에 이미 받은 달은 건너뜁니다.")
    _out()

    def progress(index: int, total: int, result: Any) -> None:
        if result.status == "skipped" and not verbose:
            return
        _out(format_task_line(index, total, result, names.get(result.task.lawd_cd, "")))

    summary = run_collect(
        client, store, tasks, regions=table, refresh_months=cfg.refresh_months, force=force, progress=progress
    )
    _out()
    _out(format_summary(summary))
    if summary.aborted:
        hint = _abort_hint(summary, types)
        if hint:
            _err(hint)
    elif summary.errors:
        _err("일부 작업이 실패했습니다. 같은 명령을 다시 실행하면 실패한 달을 다시 받습니다.")
    return summary


def _abort_hint(summary: Any, types: Sequence[DealType]) -> str:
    if summary.abort_kind == "service_key":
        apis = "\n".join(f"       - {dt.api_title}" for dt in types)
        return (
            "인증키 문제로 수집을 멈췄습니다. 다음을 확인하세요.\n"
            "  1) 공공데이터포털 마이페이지에서 아래 API 가 활용신청·승인되었는지\n"
            f"{apis}\n"
            "  2) '일반 인증키(Decoding)' 를 앞뒤 공백 없이 넣었는지\n"
            "  3) 승인 직후라면 최대 1시간쯤 뒤에 다시 시도"
        )
    if summary.abort_kind == "quota":
        return (
            "오늘의 API 호출 한도(트래픽)를 모두 썼습니다. 내일 같은 명령을 다시 실행하면\n"
            "  이미 받은 달은 건너뛰고 남은 작업부터 이어서 받습니다."
        )
    if summary.abort_kind == "storage":
        return "DB 에 저장하지 못했습니다. 다른 프로그램이 DB 파일을 쓰고 있지 않은지, 저장 공간이 충분한지 확인하세요."
    return ""


def _collect_exit_code(summary: Any) -> int:
    return EXIT_ERROR if summary.aborted or summary.errors else EXIT_OK


def cmd_collect(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    table = _region_table(cfg)
    queries = _split_values(args.region) or list(cfg.regions)
    if not queries:
        raise ConfigError(
            "수집할 지역을 -r 로 지정하세요. 예: silgeorae collect -r 강남구 -r 마포구\n"
            "  (설정 파일 [watch] regions 에 적어 두면 생략할 수 있습니다. 지역 찾기: silgeorae regions 검색어)"
        )
    regions = table.resolve_many(queries)
    types = _deal_types(args.type) or list(cfg.deal_types)
    start, end = _period(args, cfg)
    key = require_service_key(cfg, args.key, deal_types=types)
    store = _open_store(cfg)
    try:
        summary = _collect(
            cfg, store, _make_client(cfg, key), table, regions, types, month_range(start, end),
            force=args.force, verbose=args.verbose,
        )
    finally:
        store.close()
    _out(f"DB: {cfg.db_path}")
    if summary.ok:
        region_arg = " ".join(f"-r {_quote(q)}" for q in queries)
        _out(f"리포트 만들기: silgeorae report {region_arg}")
    return _collect_exit_code(summary)


def _series_in_store(store: Any) -> set[tuple[DealType, str]]:
    """DB 에 이미 거래가 있는 (유형, 시군구) 묶음."""
    series: set[tuple[DealType, str]] = set()
    for row in store.summary():
        try:
            deal_type = DealType(row["deal_type"])
            count = int(row.get("count") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if count > 0:
            series.add((deal_type, str(row["lawd_cd"])))
    return series


def _collected_changes(
    store: Any, summary: Any, known_series: set[tuple[DealType, str]]
) -> tuple[list[Transaction], list[Transaction], int]:
    """이번 수집의 신규·해제 거래. 처음 수집한 (유형, 시군구)의 거래는 '신규'에서 뺀다 (첫 적재 알림 폭탄 방지)."""
    new_keys: list[str] = []
    initial = 0
    for result in summary.results:
        if result.partition is None:
            continue
        if (result.task.deal_type, result.task.lawd_cd) in known_series:
            new_keys.extend(result.partition.new_keys)
        else:
            initial += len(result.partition.new_keys)
    return _get_many(store, new_keys), _get_many(store, summary.cancelled_keys), initial


def _get_many(store: Any, keys: Sequence[str]) -> list[Transaction]:
    getter = getattr(store, "get_many", None)  # 있으면 한 번에 (계약에는 get 만 있음)
    if getter is not None:
        return list(getter(keys))
    return [tx for tx in (store.get(key) for key in keys) if tx is not None]


def _record_highs(store: Any, new_txs: Sequence[Transaction]) -> set[str]:
    from .notify import find_record_highs

    sales = [tx for tx in new_txs if tx.deal_type.is_sale and not tx.is_cancelled and tx.price is not None]
    if not sales:
        return set()
    history = store.query(
        deal_types=sorted({tx.deal_type for tx in sales}, key=list(DealType).index),
        lawd_cds=sorted({tx.lawd_cd for tx in sales}),
        include_cancelled=False,
    )
    return find_record_highs(sales, history)


def _send_notifications(cfg: Config, flag: Optional[bool], message: str, has_news: bool) -> None:
    from .notify import send_all

    if flag is False:
        return
    if not cfg.notify_configured:
        if flag:
            _err(
                "알림 설정이 없어 알림을 보내지 않았습니다.\n"
                "  설정 파일 [notify] 에 webhook_url 또는 telegram_bot_token·telegram_chat_id 를 적으세요."
            )
        return
    if not has_news:
        _out("새 소식이 없어 알림을 보내지 않았습니다.")
        return
    results = send_all(
        message,
        webhook_url=cfg.webhook_url,
        telegram_bot_token=cfg.telegram_bot_token,
        telegram_chat_id=cfg.telegram_chat_id,
    )
    for channel, ok in results.items():
        _out(f"알림 전송 ({channel}): {'성공' if ok else '실패 — 주소·토큰을 확인하세요 (-v 로 자세히)'}")


def cmd_update(args: argparse.Namespace) -> int:
    from .notify import build_message

    cfg = _load_config(args)
    if not cfg.regions:
        where = f"현재 설정 파일: {cfg.config_path}" if cfg.config_path else "설정 파일이 없습니다 → 'silgeorae init' 으로 만드세요"
        raise ConfigError(
            "관심 지역이 설정되어 있지 않습니다.\n"
            '  설정 파일(silgeorae.toml)의 [watch] 에 regions = ["강남구", "마포구"] 처럼 적으세요.\n'
            f"  ({where})"
        )
    table = _region_table(cfg)
    regions = table.resolve_many(cfg.regions)
    types = list(cfg.deal_types)
    months = month_range(*_period(args, cfg))
    key = require_service_key(cfg, args.key, deal_types=types)
    run_start = now_kst()
    store = _open_store(cfg)
    try:
        known = _series_in_store(store)
        summary = _collect(
            cfg, store, _make_client(cfg, key), table, regions, types, months, force=False, verbose=args.verbose
        )
        new_txs, cancelled, initial = _collected_changes(store, summary, known)
        if summary.ok or new_txs or cancelled:  # 하나도 못 받았으면 '새 거래 없음'이라고 말하지 않는다
            message = build_message(new_txs, cancelled_txs=cancelled, record_high_keys=_record_highs(store, new_txs))
            _out()
            _out(message)
            if initial:
                _out(f"(처음 수집한 지역·유형의 기존 거래 {initial:,}건은 신규 알림에서 제외했습니다)")
            _send_notifications(cfg, args.notify, message, has_news=bool(new_txs or cancelled))
        code = _collect_exit_code(summary)
        if args.report:
            _out()
            report_code = _make_reports(
                cfg, store, table, regions, types, months[0], months[-1],
                formats=cfg.report_formats, out_dir=Path(cfg.report_dir), title=cfg.report_title,
                history_months=DEFAULT_HISTORY_MONTHS, new_since=run_start,
            )
            code = max(code, report_code)
    finally:
        store.close()
    return code


# =========================================================================== report
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def _safe_filename(text: str) -> str:
    return _UNSAFE_CHARS.sub("_", text).strip("._ ") or "report"


def report_basename(short_names: Sequence[str], start: str, end: str) -> str:
    """``실거래_강남구외1_202401-202506`` (파일 이름으로 쓸 수 없는 글자는 ``_``)."""
    if not short_names:
        region = "전체"
    else:
        region = short_names[0].replace(" ", "")
        if len(short_names) > 1:
            region += f"외{len(short_names) - 1}"
    return _safe_filename(f"실거래_{region}_{start}-{end}")


def _make_reports(
    cfg: Config,
    store: Any,
    table: Any,
    regions: Optional[Sequence[Any]],
    types: Optional[Sequence[DealType]],
    start: str,
    end: str,
    *,
    formats: Sequence[str],
    out_dir: Path,
    title: str,
    history_months: int,
    new_since: Optional[datetime],
) -> int:
    from .analysis import build_report, export_csv, export_excel, export_html, export_tables_csv

    lawd_cds = [region.lawd_cd for region in regions] if regions else None
    query_start = ym_add(start, -history_months) if history_months > 0 else start
    transactions = store.query(
        deal_types=list(types) if types else None, lawd_cds=lawd_cds, start_ym=query_start, end_ym=end
    )
    in_period = [tx for tx in transactions if start <= tx.deal_ym <= end]
    if not in_period:
        where = ", ".join(region.name for region in regions) if regions else "전체 지역"
        example = _quote(regions[0].short_name) if regions else "강남구"
        _err(
            f"{ym_label(start)}~{ym_label(end)} 기간에 해당하는 거래가 DB 에 없습니다 ({where}).\n"
            f"  먼저 수집하세요. 예: silgeorae collect -r {example} "
            f"--from {start[:4]}-{start[4:]} --to {end[:4]}-{end[4:]}\n"
            "  저장된 지역·기간 확인: silgeorae status"
        )
        return EXIT_ERROR
    if regions:
        names = [region.name for region in regions]
        short_names = [region.short_name for region in regions]
    else:
        codes = sorted({tx.lawd_cd for tx in in_period})
        names = [_region_label(table, code) for code in codes]
        short_names = [_region_label(table, code, short=True) for code in codes]
    report = build_report(transactions, title=title, regions=names, period=(start, end), new_since=new_since)

    base = report_basename(short_names, start, end)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    table_dirs: dict[Path, int] = {}  # 표별 CSV 폴더 → 파일 수
    failed: list[str] = []
    for fmt in formats:
        try:
            if fmt == "xlsx":
                written.append(Path(export_excel(report, out_dir / f"{base}.xlsx", transactions=in_period)))
            elif fmt == "html":
                written.append(Path(export_html(report, out_dir / f"{base}.html")))
            elif fmt == "csv":
                written.append(Path(export_csv(in_period, out_dir / f"{base}_거래내역.csv")))
                tables_dir = out_dir / f"{base}_표"
                tables = export_tables_csv(report, tables_dir)
                table_dirs[tables_dir] = len(tables)
        except ExportError as exc:  # 분석팀 메시지를 그대로 (파일이 엑셀에서 열려 있음 등)
            failed.append(fmt)
            _err(f"경고: {fmt} 리포트를 만들지 못했습니다: {exc}")
            if fmt == "xlsx" and importlib.util.find_spec("openpyxl") is None:
                _err('  엑셀 파일에는 openpyxl 이 필요합니다: pip install "silgeorae[excel]" 또는 pip install openpyxl')
    if written or table_dirs:
        _out(f"리포트를 만들었습니다 ({ym_label(start)}~{ym_label(end)}, 거래 {len(in_period):,}건, {', '.join(names)}):")
        for path in written:
            _out(f"  {path}")
        for folder, count in table_dirs.items():
            _out(f"  {folder}{os.sep} (표별 CSV {count}개)")
    return EXIT_ERROR if failed else EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    table = _region_table(cfg)
    queries = _split_values(args.region)
    regions = table.resolve_many(queries) if queries else None
    types = _deal_types(args.type) or None
    start, end = _period(args, cfg)
    formats = normalize_formats(_split_values(args.format), where="--format") if args.format else list(cfg.report_formats)
    out_dir = Path(args.out).expanduser() if args.out else Path(cfg.report_dir)
    store = _open_existing_store(cfg)
    if store is None:
        return EXIT_ERROR
    try:
        return _make_reports(
            cfg, store, table, regions, types, start, end,
            formats=formats, out_dir=out_dir, title=args.title or cfg.report_title,
            history_months=args.history_months,
            new_since=now_kst() - timedelta(days=args.new_days) if args.new_days else None,
        )
    finally:
        store.close()


# =========================================================================== new / search
def cmd_new(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    table = _region_table(cfg)
    lawd_cds = _filter_codes(table, args.region)
    types = _deal_types(args.type) or None
    if args.since is not None:
        since = datetime.combine(args.since, time(0), tzinfo=KST)
    else:
        since = now_kst() - timedelta(days=args.days)
    store = _open_existing_store(cfg)
    if store is None:
        return EXIT_ERROR
    try:
        found = store.query(deal_types=types, lawd_cds=lawd_cds, first_seen_since=since)
    finally:
        store.close()
    found.sort(key=_recent_first)
    _out(f"{since:%Y-%m-%d %H:%M} 이후 새로 수집된 거래: {len(found):,}건")
    if not found:
        _out("  (없음) — 'silgeorae update' 또는 'silgeorae collect' 로 새 거래를 받아 오세요.")
        return EXIT_OK
    _out(format_transactions(found, max_rows=args.limit, with_first_seen=True))
    return EXIT_OK


def cmd_search(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    table = _region_table(cfg)
    lawd_cds = _filter_codes(table, args.region)
    types = _deal_types(args.type) or None
    if args.start and args.end and args.start > args.end:
        raise ConfigError(f"시작 연월({ym_label(args.start)})이 끝 연월({ym_label(args.end)})보다 늦습니다.")
    store = _open_existing_store(cfg)
    if store is None:
        return EXIT_ERROR
    try:
        found = store.query(
            deal_types=types,
            lawd_cds=lawd_cds,
            start_ym=args.start,
            end_ym=args.end,
            name=args.name or None,
            dong=args.dong or None,
            include_cancelled=args.include_cancelled,
            min_area=args.min_area,
            max_area=args.max_area,
        )
    finally:
        store.close()
    found.sort(key=_recent_first)
    shown = f" (최근 계약 {args.limit:,}건 표시)" if len(found) > args.limit else ""
    _out(f"검색 결과: {len(found):,}건{shown}")
    if found:
        _out(format_transactions(found, max_rows=args.limit))
    else:
        _out("  조건에 맞는 거래가 없습니다. 조건을 줄이거나 먼저 수집하세요 (silgeorae status 로 저장 현황 확인).")
    if args.csv:
        from .analysis import export_csv

        path = export_csv(found, Path(args.csv).expanduser())
        _out(f"CSV 로 저장했습니다: {path}")
    return EXIT_OK


# =========================================================================== status
def _use_color() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM"))
    return True


def _file_size(path: Path) -> str:
    size = float(path.stat().st_size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return ""  # pragma: no cover


def _as_kst(value: Optional[datetime]) -> str:
    if value is None:
        return "-"
    if value.tzinfo is not None:
        value = value.astimezone(KST)
    return value.strftime("%Y-%m-%d %H:%M")


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    key = (args.key or "").strip() or cfg.service_key
    source = "--key 옵션" if (args.key or "").strip() else cfg.service_key_source
    notify = []
    if cfg.webhook_url:
        notify.append("웹훅")
    if cfg.telegram_configured:
        notify.append("텔레그램")
    regions_file = cfg.regions_file if cfg.regions_file is not None and Path(cfg.regions_file).is_file() else None
    _out(f"설정 파일  : {cfg.config_path or '(없음 — 기본값 사용, silgeorae init 으로 만들 수 있음)'}")
    _out(f"인증키     : {f'설정됨 ({mask_secret(key)}, {source})' if key else '없음 (silgeorae collect 실행 시 발급 안내)'}")
    _out(f"관심 지역  : {', '.join(cfg.regions) or '(없음 — [watch] regions)'}")
    _out(f"관심 유형  : {', '.join(dt.label for dt in cfg.deal_types)}")
    _out(f"알림       : {', '.join(notify) or '(없음)'}")
    _out(f"지역코드표 : {regions_file or '내장 표'}")
    db_path = Path(cfg.db_path)
    if not db_path.is_file():
        _out(f"DB         : {db_path} (아직 없음)")
        _out("\n아직 수집한 데이터가 없습니다. 'silgeorae collect -r 강남구' 또는 'silgeorae demo' 로 시작하세요.")
        return EXIT_OK
    _out(f"DB         : {db_path} ({_file_size(db_path)})")

    table = _region_table(cfg)
    store = _open_store(cfg)
    try:
        rows = store.summary()
        entries = list(store.fetch_log(limit=10))
        failing = list(store.fetch_log(status="error"))  # 파티션마다 마지막 시도가 오류인 것
    finally:
        store.close()

    _out("\n[저장된 거래]")
    order = list(DealType)
    body = []
    total = cancelled_total = 0
    for row in sorted(rows, key=lambda r: (order.index(DealType(r["deal_type"])), str(r["lawd_cd"]))):
        count = int(row.get("count") or 0)
        cancelled = int(row.get("cancelled") or 0)
        total += count
        cancelled_total += cancelled
        period = ""
        if row.get("min_ym") and row.get("max_ym"):
            period = f"{ym_label(row['min_ym'])}~{ym_label(row['max_ym'])}"
        body.append([DealType(row["deal_type"]).label, _region_label(table, str(row["lawd_cd"])), count, cancelled, period])
    if body:
        _out(format_table(["유형", "지역", "건수", "해제", "계약 기간"], body, align="llrrl"))
        _out(f"합계 {total:,}건 (해제 {cancelled_total:,}건)")
    else:
        _out("  (저장된 거래 없음)")

    _out("\n[최근 수집 기록]")
    if not entries:
        _out("  (기록 없음)")
        return EXIT_OK
    entries.sort(key=lambda e: e.fetched_at.timestamp() if e.fetched_at else 0.0, reverse=True)
    log_rows = []
    for entry in entries[:10]:
        is_error = entry.status != "ok"
        log_rows.append(
            [
                _as_kst(entry.fetched_at),
                DealType(entry.deal_type).label,
                _region_label(table, entry.lawd_cd, short=True),
                ym_label(entry.deal_ym),
                entry.item_count,
                "[오류]" if is_error else "정상",
                entry.message if is_error else "",
            ]
        )
    text = format_table(
        ["시각", "유형", "지역", "연월", "건수", "상태", "메시지"], log_rows,
        align="llllrll", max_width=[16, 14, 16, 7, 8, 6, 60], fit=terminal_width(), shrink=(6, 2),
    )
    lines = text.split("\n")
    if _use_color():
        for index, row in enumerate(log_rows):
            if row[5] == "[오류]":
                lines[index + 2] = f"\033[31m{lines[index + 2]}\033[0m"
    _out("\n".join(lines))
    if failing:
        months = sorted({entry.deal_ym for entry in failing})
        span = ym_label(months[0]) if len(months) == 1 else f"{ym_label(months[0])}~{ym_label(months[-1])}"
        _out(
            f"마지막 수집이 실패한 작업 {len(failing):,}건 ({span}) — "
            "같은 수집 명령을 다시 실행하면 실패한 달을 다시 받습니다."
        )
    return EXIT_OK


# =========================================================================== demo
def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import DEMO_REFRESH_MONTHS, run_demo

    out = Path(args.out).expanduser()
    _out(f"가상 데이터로 데모를 실행합니다 (인증키·인터넷 불필요, {args.months}개월)…")
    result = run_demo(out, months=args.months)
    initial, latest = result.initial_summary, result.summary
    if initial is not None:
        _out(f"  1) 일주일 전 상황으로 전체 수집: 거래 {initial.inserted:,}건 저장")
    _out(
        f"  2) 오늘 다시 수집 (update 처럼 최근 {DEMO_REFRESH_MONTHS}개월만 다시 받음): "
        f"신규 {latest.inserted:,}건 · 새로 해제 {latest.newly_cancelled:,}건 · 변경 {latest.updated:,}건"
    )
    start, end = result.period
    _out(
        f"\nDB 에 거래 {result.transaction_count:,}건 ({ym_label(start)}~{ym_label(end)}, "
        f"{', '.join(result.regions)})"
    )
    _out("만든 파일:")
    for path in [result.db_path, *result.report_paths]:
        _out(f"  {path}")
    db = _quote(result.db_path)
    _out("\n다음 단계:")
    _out(f"  silgeorae --db {db} status")
    _out(f"  silgeorae --db {db} new --days 1")
    _out(f"  silgeorae --db {db} search --name 한빛 --limit 10")
    _out(f"  silgeorae --db {db} report -r 강남구 --out {_quote(out)}")
    _out("  실제 데이터: silgeorae init → 인증키 입력 → silgeorae collect -r 강남구")
    _out("※ 데모의 단지명·지번·가격은 모두 가상 데이터입니다.")
    return EXIT_OK


# =========================================================================== main
class _StderrHandler(logging.StreamHandler):
    """항상 '현재의' sys.stderr 로 쓰는 로그 핸들러 (테스트에서 stderr 가 바뀌어도 안전)."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property  # type: ignore[override]
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        pass


class _KoreanFormatter(logging.Formatter):
    _LEVELS = {"DEBUG": "디버그", "INFO": "정보", "WARNING": "경고", "ERROR": "오류", "CRITICAL": "심각"}

    def format(self, record: logging.LogRecord) -> str:
        record.level_ko = self._LEVELS.get(record.levelname, record.levelname)
        return super().format(record)


def _setup_logging(verbose: bool) -> None:
    logger = logging.getLogger("silgeorae")
    if not any(isinstance(h, _StderrHandler) for h in logger.handlers):
        handler = _StderrHandler()
        handler.setFormatter(_KoreanFormatter("%(level_ko)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.WARNING)


def _setup_console_streams() -> None:
    """윈도우 콘솔 등에서 표현할 수 없는 글자 때문에 죽지 않도록 errors='replace'."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "").replace("_", "")
        if sys.platform == "win32" or encoding not in ("utf8", "utf8sig"):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(errors="replace")
                except (ValueError, OSError, TypeError):  # pragma: no cover
                    pass


def _print_region_error(exc: RegionError) -> None:
    message = str(exc)
    _err(f"오류: {message}")
    if isinstance(exc, AmbiguousRegionError) and exc.candidates:
        _err("  후보 (이름 대신 코드를 써도 됩니다):")
        for candidate in exc.candidates:
            code = getattr(candidate, "lawd_cd", "")
            name = getattr(candidate, "name", "") or str(candidate)
            _err(f"    {code}  {name}" if code else f"    {name}")
    elif "silgeorae regions" not in message:
        _err("  지역 찾기: silgeorae regions 검색어 (예: silgeorae regions 강남)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``silgeorae`` 진입점. 종료 코드를 돌려준다."""
    _setup_console_streams()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version, 사용법 오류
        return exc.code if isinstance(exc.code, int) else (EXIT_OK if exc.code is None else EXIT_USAGE)
    verbose = bool(getattr(args, "verbose", False))
    _setup_logging(verbose)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    try:
        return int(handler(args))
    except KeyboardInterrupt:
        _err("\n중단되었습니다. (이미 저장한 데이터는 그대로 남아 있습니다)")
        return EXIT_INTERRUPTED
    except RegionError as exc:
        _print_region_error(exc)
        return EXIT_USAGE
    except ConfigError as exc:
        _err(f"오류: {exc}")
        return EXIT_USAGE
    except SilgeoraeError as exc:
        _err(f"오류: {exc}")
        return EXIT_ERROR
    except BrokenPipeError:  # 예: silgeorae search ... | head — 종료 시 flush 오류가 나지 않게 stdout 을 devnull 로
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):  # pragma: no cover
            pass
        return EXIT_OK
    except Exception as exc:  # 예기치 못한 오류 — 사용자에게는 짧게, -v 면 전체 추적
        _err(f"오류: 예기치 못한 문제가 발생했습니다: {exc.__class__.__name__}: {exc}")
        if verbose:
            traceback.print_exc()
        else:
            _err("  자세한 내용은 -v 옵션을 붙여 다시 실행해 보세요.")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
