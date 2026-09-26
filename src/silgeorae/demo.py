"""오프라인 데모 — 가상 단지의 합성 실거래 데이터로 수집 → 저장 → 리포트 전 과정을 보여 준다.

인증키·인터넷 없이 ``silgeorae demo`` 로 실행한다. **단지명·지번·가격은 모두 가상**이다.

데이터는 실제 API 응답(``tests/fixtures/apt_trade.xml``, ``apt_rent.xml``)과 같은 형식의
원본 dict 이며, ``FakeTransport`` → ``MolitClient`` 를 그대로 거쳐 저장된다.
"update" 흐름을 보여 주기 위해 두 번 수집한다.

1. 일주일 전 시점의 신고 현황으로 전체 기간을 수집
2. 오늘 시점으로 다시 수집 (최근 3개월만 다시 받음) → 그사이 신고된 **신규 거래**,
   새로 **해제**된 거래, 채워진 등기일자가 반영된다.
"""

from __future__ import annotations

import calendar
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

from .errors import ConfigError, ExportError, SilgeoraeError
from .models import DealType
from .pipeline import CollectSummary, plan_tasks, run_collect
from .utils import KST, now_kst, parse_ym, recent_months, today_kst

log = logging.getLogger(__name__)

DEMO_TITLE = "[데모] 부동산 실거래 정리 (가상 데이터)"
DEMO_KEY = "DEMO-KEY"
DEMO_DB_NAME = "demo.db"
DEMO_DEAL_TYPES = (DealType.APT_SALE, DealType.APT_RENT)
DEMO_UPDATE_GAP_DAYS = 7  # 1차 수집을 며칠 전 상황으로 볼지
DEMO_REFRESH_MONTHS = 3

RawItems = dict[tuple[DealType, str, str], list[dict[str, str]]]


@dataclass(frozen=True)
class _Complex:
    """가상 아파트 단지."""

    seq: str  # aptSeq
    name: str
    dong: str
    umd_cd: str
    jibun: str
    build_year: int
    areas: tuple[float, ...]
    area_weights: tuple[int, ...]
    weight: int  # 거래 빈도 가중치
    premium: float  # 지역 평균 대비 가격 수준
    max_floor: int
    buildings: int  # 동 수 (101동부터)
    road: str
    road_no: int
    road_cd: str


@dataclass(frozen=True)
class _Region:
    lawd_cd: str
    price_per_m2: int  # 2025년 1월 기준 ㎡당 매매가(만원)
    jeonse_ratio: float  # 전세가율
    agent: str  # 중개사 소재지
    sales_per_month: tuple[int, int]
    rents_per_month: tuple[int, int]
    complexes: tuple[_Complex, ...]


DEMO_REGIONS: tuple[_Region, ...] = (
    _Region(
        "11680", 3300, 0.50, "서울 강남구", (5, 12), (10, 18),
        (
            _Complex("11680-9001", "한빛마을1단지", "대치동", "10600", "316", 2015, (59.97, 84.97, 114.8), (3, 5, 2), 5, 1.10, 25, 9, "샘플로", 51, "3121022"),
            _Complex("11680-9002", "푸른숲", "개포동", "10300", "12-3", 1998, (59.96, 84.91), (5, 4), 4, 0.86, 15, 6, "예시로", 12, "3121023"),
            _Complex("11680-9003", "은하수파크", "역삼동", "10100", "823", 2008, (84.93, 114.85, 134.9), (5, 3, 1), 3, 1.00, 30, 4, "샘플로", 210, "3121022"),
            _Complex("11680-9004", "새솔마을", "대치동", "10600", "501", 2004, (59.92, 84.99), (4, 5), 3, 0.95, 20, 7, "예시로", 88, "3121023"),
        ),
    ),
    _Region(
        "11440", 1850, 0.58, "서울 마포구", (5, 11), (8, 15),
        (
            _Complex("11440-9001", "가람뜰", "아현동", "10100", "771", 2014, (59.98, 84.95, 114.7), (4, 5, 1), 5, 1.05, 22, 12, "가상로", 45, "3113011"),
            _Complex("11440-9002", "솔바람채", "공덕동", "10200", "456-2", 2011, (59.9, 84.97), (5, 5), 4, 0.98, 18, 5, "견본길", 7, "3113012"),
            _Complex("11440-9003", "하늘정원", "공덕동", "10200", "38", 2019, (59.99, 84.98, 101.9), (4, 5, 1), 3, 1.12, 25, 6, "가상로", 120, "3113011"),
        ),
    ),
    _Region(
        "41135", 1700, 0.55, "경기 성남시 분당구", (6, 12), (8, 16),
        (
            _Complex("41135-9001", "별빛마을", "정자동", "10300", "110", 1995, (59.94, 84.9, 131.6), (3, 5, 2), 5, 0.90, 15, 10, "모의로", 30, "4113521"),
            _Complex("41135-9002", "누리뜰", "서현동", "10500", "287", 1994, (59.88, 84.96), (4, 5), 4, 0.93, 20, 8, "보기로", 15, "4113522"),
            _Complex("41135-9003", "햇살채", "정자동", "10300", "15-1", 2016, (84.99, 114.93), (6, 3), 2, 1.15, 35, 3, "모의로", 72, "4113521"),
            _Complex("41135-9004", "새빛마을", "서현동", "10500", "301", 2003, (59.97, 84.94), (4, 5), 3, 1.00, 25, 7, "보기로", 101, "4113522"),
        ),
    ),
)

_OTHER_AGENTS = ("서울 서초구", "서울 송파구", "서울 용산구", "경기 용인시 수지구")
_PARTY = (("개인", 93), ("법인", 5), ("기타", 2))
_BLANK = " "  # 실제 API 는 빈 값을 공백 한 칸으로 준다


# --------------------------------------------------------------------------- 데이터 생성
@dataclass
class _Deal:
    deal_date: date
    reported: date  # 신고(공개)일 — 이 날 이후 조회에 나타난다
    item: dict[str, str]
    cancel_date: Optional[date] = None
    rgst_date: Optional[date] = None

    def snapshot(self, as_of: Optional[date]) -> Optional[dict[str, str]]:
        """``as_of`` 날짜에 API 가 돌려줄 모습 (아직 신고 전이면 None)."""
        if as_of is not None and self.reported > as_of:
            return None
        item = dict(self.item)
        if self.cancel_date is not None and (as_of is None or self.cancel_date <= as_of):
            item["cdealType"] = "O"
            item["cdealDay"] = _yymmdd(self.cancel_date)
        if self.rgst_date is not None and (as_of is None or self.rgst_date <= as_of):
            item["rgstDate"] = _yymmdd(self.rgst_date)
        return item


def _yymmdd(d: date) -> str:
    return d.strftime("%y.%m.%d")


def _num(value: int) -> str:
    return f"{value:,}"


def _area_str(area: float) -> str:
    return f"{area:.4f}".rstrip("0").rstrip(".")


def _jibun_parts(jibun: str) -> tuple[str, str]:
    main, _, sub = jibun.partition("-")
    return f"{int(main):04d}", f"{int(sub or 0):04d}"


def _trend(d: date) -> float:
    """2025년 1월 = 1.0, 한 달에 0.45%씩 오르는 가상 시세."""
    months = (d.year - 2025) * 12 + (d.month - 1)
    return 1.0045 ** months


def _size_factor(area: float) -> float:
    if area < 70:
        return 1.06
    if area < 100:
        return 1.0
    return 0.96


def _floor_factor(floor: int, max_floor: int) -> float:
    if floor <= 2:
        return 0.93
    if floor <= 5:
        return 0.97
    return 1.0 + 0.04 * floor / max_floor


def _round_to(value: float, unit: int) -> int:
    return max(unit, int(round(value / unit)) * unit)


def _expected_price(region: _Region, cx: _Complex, area: float, floor: int, d: date) -> float:
    return (
        region.price_per_m2 * cx.premium * area * _size_factor(area) * _trend(d) * _floor_factor(floor, cx.max_floor)
    )


def _noise(rng: random.Random, sigma: float, cap: float) -> float:
    return min(max(rng.gauss(0.0, sigma), -cap), cap)


def _party(rng: random.Random) -> str:
    return rng.choices([p for p, _ in _PARTY], weights=[w for _, w in _PARTY])[0]


def _make_sale(rng: random.Random, region: _Region, cx: _Complex, area: float, floor: int, d: date) -> _Deal:
    price = _expected_price(region, cx, area, floor, d) * (1 + _noise(rng, 0.035, 0.09))
    price_manwon = _round_to(price, 500 if price >= 100_000 else 100)
    brokered = rng.random() < 0.88
    agent = _BLANK
    if brokered:
        agent = region.agent
        if rng.random() < 0.1:
            agent = f"{region.agent}, {rng.choice(_OTHER_AGENTS)}"
    bonbun, bubun = _jibun_parts(cx.jibun)
    item = {
        "aptDong": f"{100 + rng.randint(1, cx.buildings)}" if rng.random() < 0.92 else _BLANK,
        "aptNm": cx.name,
        "aptSeq": cx.seq,
        "bonbun": bonbun,
        "bubun": bubun,
        "buildYear": str(cx.build_year),
        "buyerGbn": _party(rng),
        "cdealDay": _BLANK,
        "cdealType": _BLANK,
        "dealAmount": _num(price_manwon),
        "dealDay": str(d.day),
        "dealMonth": str(d.month),
        "dealYear": str(d.year),
        "dealingGbn": "중개거래" if brokered else "직거래",
        "estateAgentSggNm": agent,
        "excluUseAr": _area_str(area),
        "floor": str(floor),
        "jibun": cx.jibun,
        "landCd": "1",
        "landLeaseholdGbn": "N",
        "rgstDate": _BLANK,
        "roadNm": cx.road,
        "roadNmBonbun": f"{cx.road_no:05d}",
        "roadNmBubun": "00000",
        "roadNmCd": cx.road_cd,
        "roadNmSeq": "01",
        "roadNmSggCd": region.lawd_cd,
        "roadNmbCd": "0",
        "sggCd": region.lawd_cd,
        "slerGbn": _party(rng),
        "umdCd": cx.umd_cd,
        "umdNm": cx.dong,
    }
    reported = d + timedelta(days=min(29, int(rng.expovariate(1 / 7))))
    cancel_date = rgst_date = None
    if rng.random() < 0.03:  # 약 3% 해제
        cancel_date = max(reported + timedelta(days=1), d + timedelta(days=rng.randint(10, 45)))
    elif rng.random() < 0.9:  # 대부분 1~2.5개월 뒤 등기
        rgst_date = d + timedelta(days=rng.randint(30, 80))
    return _Deal(d, reported, item, cancel_date, rgst_date)


def _make_rent(rng: random.Random, region: _Region, cx: _Complex, area: float, floor: int, d: date) -> _Deal:
    jeonse = _expected_price(region, cx, area, floor, d) * region.jeonse_ratio * (1 + _noise(rng, 0.05, 0.12))
    unit = 1000 if jeonse >= 50_000 else 500
    if rng.random() < 0.6:  # 전세
        deposit = _round_to(jeonse, unit)
        monthly = 0
    else:  # 월세 (보증금 일부 + 전월세 전환율 4.2~5.5%)
        deposit = _round_to(jeonse * rng.choice((0.05, 0.1, 0.2, 0.3, 0.5)), 500)
        monthly = _round_to((jeonse - deposit) * rng.uniform(0.042, 0.055) / 12, 5)

    roll = rng.random()
    contract_type = _BLANK if roll < 0.05 else ("갱신" if roll < 0.30 else "신규")
    use_rr = _BLANK
    pre_deposit = pre_monthly = _BLANK
    if contract_type == "갱신":
        renewal_right = rng.random() < 0.6
        use_rr = "사용" if renewal_right else _BLANK
        ratio = 1.05 if renewal_right else rng.uniform(1.0, 1.12)  # 갱신요구권은 5% 상한
        pre_deposit = _num(_round_to(deposit / ratio, 100))
        pre_monthly = str(_round_to(monthly / ratio, 5) if monthly else 0)
    term = _BLANK
    if contract_type != _BLANK or rng.random() < 0.5:
        start_index = d.year * 12 + d.month - 1 + rng.choice((0, 1, 1, 2))
        sy, sm = divmod(start_index, 12)
        term = f"{sy % 100:02d}.{sm + 1:02d}~{(sy + 2) % 100:02d}.{sm + 1:02d}"
    item = {
        "aptNm": cx.name,
        "buildYear": str(cx.build_year),
        "contractTerm": term,
        "contractType": contract_type,
        "dealDay": str(d.day),
        "dealMonth": str(d.month),
        "dealYear": str(d.year),
        "deposit": _num(deposit),
        "excluUseAr": _area_str(area),
        "floor": str(floor),
        "jibun": cx.jibun,
        "monthlyRent": str(monthly),
        "preDeposit": pre_deposit,
        "preMonthlyRent": pre_monthly,
        "sggCd": region.lawd_cd,
        "umdNm": cx.dong,
        "useRRRight": use_rr,
    }
    reported = d + timedelta(days=min(29, int(rng.expovariate(1 / 10))))
    return _Deal(d, reported, item)


def _generate_partition(deal_type: DealType, region: _Region, ym: str, seed: int) -> list[_Deal]:
    # 파티션마다 따로 난수를 만들어, 기간을 바꿔도 같은 달의 데이터는 똑같이 나온다.
    rng = random.Random(f"silgeorae-demo:{seed}:{deal_type.value}:{region.lawd_cd}:{ym}")
    year, month = int(ym[:4]), int(ym[4:])
    days = calendar.monthrange(year, month)[1]
    low, high = region.sales_per_month if deal_type is DealType.APT_SALE else region.rents_per_month
    maker = _make_sale if deal_type is DealType.APT_SALE else _make_rent
    deals = []
    for _ in range(rng.randint(low, high)):
        cx = rng.choices(region.complexes, weights=[c.weight for c in region.complexes])[0]
        area = rng.choices(cx.areas, weights=cx.area_weights)[0]
        floor = rng.randint(1, cx.max_floor)
        deals.append(maker(rng, region, cx, area, floor, date(year, month, rng.randint(1, days))))
    deals.sort(key=lambda deal: (deal.deal_date, deal.item["aptNm"]))
    return deals


def generate_demo_data(months: Iterable[str], *, seed: int = 42, as_of: Optional[date] = None) -> RawItems:
    """가상 원본 데이터 ``{(유형, 시군구, 연월): [원본 dict, …]}`` 를 만든다 (같은 seed → 같은 결과).

    지역: 서울 강남구(11680)·마포구(11440), 성남시 분당구(41135). 유형: 아파트 매매·전월세.
    ``as_of`` 를 주면 그날까지 신고된 거래만, 그날까지 반영된 해제·등기 정보로 돌려준다.
    """
    data: RawItems = {}
    for ym in sorted({parse_ym(m) for m in months}):
        for region in DEMO_REGIONS:
            for deal_type in DEMO_DEAL_TYPES:
                items = []
                for deal in _generate_partition(deal_type, region, ym, seed):
                    snap = deal.snapshot(as_of)
                    if snap is not None:
                        items.append(snap)
                data[(deal_type, region.lawd_cd, ym)] = items
    return data


# --------------------------------------------------------------------------- 실행
@dataclass
class DemoResult:
    """데모 실행 결과."""

    db_path: Path
    report_paths: list[Path]
    summary: CollectSummary  # 2차(오늘) 수집 결과 — 신규·해제
    initial_summary: Optional[CollectSummary] = None  # 1차(일주일 전) 수집 결과
    transaction_count: int = 0  # DB 에 저장된 거래 수
    period: tuple[str, str] = ("", "")
    regions: list[str] = field(default_factory=list)
    new_since: Optional[datetime] = None


def _clock(today: date) -> datetime:
    now = now_kst()
    if today == now.date():
        return now
    return datetime.combine(today, now.time(), tzinfo=KST)


def _remove_db(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(f"{db_path}{suffix}")
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                raise SilgeoraeError(
                    f"기존 데모 DB 를 지울 수 없습니다: {path} ({exc})\n"
                    "  다른 프로그램이 파일을 열고 있지 않은지 확인하세요."
                ) from exc


def _openpyxl_available() -> bool:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return False
    return True


def run_demo(out_dir: str | Path, *, months: int = 24, today: Optional[date] = None, seed: int = 42) -> DemoResult:
    """가상 데이터로 수집 → 저장 → 리포트(html·csv, openpyxl 이 있으면 xlsx)를 만든다.

    결과는 ``out_dir`` 에 ``demo.db``, ``demo_report.html``, ``demo_report.xlsx``,
    ``demo_transactions.csv`` 로 저장된다. 다시 실행하면 DB 를 새로 만든다.
    """
    from .analysis import build_report, export_csv, export_excel, export_html
    from .collector import FakeTransport, MolitClient
    from .processing import TransactionStore
    from .regions import RegionTable

    if months < 1:
        raise ConfigError("개월 수는 1 이상이어야 합니다.")
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    today = today or today_kst()
    month_list = recent_months(months, today)
    db_path = out / DEMO_DB_NAME
    _remove_db(db_path)

    table = RegionTable.load()
    lawd_cds = [region.lawd_cd for region in DEMO_REGIONS]
    tasks = plan_tasks(DEMO_DEAL_TYPES, lawd_cds, month_list)
    earlier = today - timedelta(days=DEMO_UPDATE_GAP_DAYS)

    def collect(store: object, as_of: date, now: datetime) -> CollectSummary:
        data = generate_demo_data(month_list, seed=seed, as_of=as_of)
        client = MolitClient(DEMO_KEY, transport=FakeTransport(data), sleep=lambda seconds: None)
        summary = run_collect(
            client, store, tasks, regions=table, refresh_months=DEMO_REFRESH_MONTHS, today=as_of, now=now
        )
        if summary.aborted or summary.errors:
            reason = summary.abort_reason or (summary.error_results[0].error if summary.error_results else "")
            raise SilgeoraeError(f"데모 수집 중 오류가 발생했습니다: {reason}")
        return summary

    store = TransactionStore(db_path)
    try:
        log.info("데모 1차 수집: %s 기준 %d개월", earlier, len(month_list))
        initial = collect(store, earlier, _clock(today) - timedelta(days=DEMO_UPDATE_GAP_DAYS))
        new_since = _clock(today)
        log.info("데모 2차 수집: %s 기준 (최근 %d개월 다시 받기)", today, DEMO_REFRESH_MONTHS)
        latest = collect(store, today, new_since)
        transactions = store.query(deal_types=list(DEMO_DEAL_TYPES), lawd_cds=lawd_cds)
    finally:
        store.close()

    period = (month_list[0], month_list[-1])
    names = [table.name_of(code) for code in lawd_cds]
    report = build_report(
        transactions, title=DEMO_TITLE, regions=names, period=period, new_since=new_since, today=today
    )
    exports: list[tuple[str, Callable[[], object]]] = [
        ("HTML", lambda: export_html(report, out / "demo_report.html")),
        ("CSV", lambda: export_csv(transactions, out / "demo_transactions.csv")),
    ]
    if _openpyxl_available():
        exports.insert(1, ("엑셀", lambda: export_excel(report, out / "demo_report.xlsx", transactions=transactions)))
    else:
        log.info("openpyxl 이 없어 엑셀 파일은 건너뜁니다 (pip install openpyxl)")
    paths: list[Path] = []
    failure: Optional[ExportError] = None
    for label, export in exports:
        try:  # 예: 이전 결과 파일이 엑셀에서 열려 있으면 그 파일만 건너뛴다
            paths.append(Path(export()))  # type: ignore[arg-type]
        except ExportError as exc:
            failure = exc
            log.warning("데모 %s 파일을 만들지 못했습니다: %s", label, exc)
    if not paths and failure is not None:
        raise failure
    return DemoResult(
        db_path=db_path,
        report_paths=paths,
        summary=latest,
        initial_summary=initial,
        transaction_count=len(transactions),
        period=period,
        regions=names,
        new_since=new_since,
    )
