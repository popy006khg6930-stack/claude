# silgeorae 설계 문서 — 부동산 실거래 자동 정리 프로그램

> 이 문서는 "방법 구상" 단계의 산출물이며, 네 개 개발팀이 동시에 작업할 수 있도록
> **모듈 경계와 팀 간 인터페이스(계약)** 를 고정한다.

## 1. 목표와 범위

| 구분 | 내용 |
|---|---|
| 데이터 | 국토교통부 실거래가 공개 API (공공데이터포털 `apis.data.go.kr/1613000`) |
| 대상 | 아파트·오피스텔·연립다세대·단독/다가구의 매매·전월세 8종 + 분양권, 토지, 상업업무용, 공장·창고 (총 12종) |
| 자동화 | 관심 지역·유형을 설정해 두면 `update` 한 번으로 최근 N개월을 다시 받아 **신규 거래·해제 거래·신고가**를 정리 |
| 정리 결과 | 엑셀(시트별 요약), CSV(엑셀 호환 UTF-8 BOM), 단일 파일 HTML 리포트(차트 포함), 콘솔 출력, 알림(웹훅/텔레그램) |
| 원칙 | 표준 라이브러리만으로 동작 (엑셀만 `openpyxl` 선택 설치), 인증키 없이도 `demo` 로 전체 흐름 확인 가능 |

## 2. 데이터 소스 요약

* 요청: `GET https://apis.data.go.kr/1613000/{서비스명}/get{서비스명}`
  * `serviceKey` — 공공데이터포털 인증키 (**Encoding 키를 넣으면 이중 인코딩되어 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`** 가 난다 → `%` 가 있으면 한 번 unquote 후 사용)
  * `LAWD_CD` — 법정동코드 앞 5자리(시군구), `DEAL_YMD` — 계약연월 `YYYYMM`
  * `pageNo`, `numOfRows` — 페이지네이션 (`totalCount` 로 전체 건수 확인)
* 응답: XML `response/header/resultCode`(정상 `000`, 구버전 `00`), `response/body/items/item[]`, `totalCount`
* 게이트웨이 오류: `OpenAPI_ServiceResponse/cmmMsgHeader/returnReasonCode`
  * `20` 접근거부(미활용신청) · `30` 미등록 키 · `31` 기한만료 · `32` 미등록 IP → **ServiceKeyError** (중단)
  * `22` 일일 트래픽 초과 → **QuotaExceededError** (중단, 다음날 이어서)
  * `01` `04` `05` `99` 등 → 재시도 가능한 **ApiError**
* 신고·반영 특성: 계약 후 **30일 이내 신고**, 이후 **해제(취소)** 가 반영되고 등기일자가 채워짐
  → 최근 몇 개월은 **주기적으로 다시 받아야** 정확하다 (파티션 교체 전략, §6).
* 2024년 개편 이후 필드명은 영문(`dealAmount` 등)이다. 구버전 한글 태그(`거래금액` 등)도 정규화에서 함께 지원한다.

### 2.1 유형별 API 와 필드 (신규 영문 필드)

| DealType | 서비스명 | 주요 item 필드 |
|---|---|---|
| `apt_sale` 아파트 매매(상세) | RTMSDataSvcAptTradeDev | sggCd, umdCd, landCd, bonbun, bubun, roadNm, roadNmSggCd, roadNmCd, roadNmSeq, roadNmbCd, roadNmBonbun, roadNmBubun, umdNm, aptNm, jibun, excluUseAr, dealYear, dealMonth, dealDay, dealAmount, floor, buildYear, aptSeq, cdealType, cdealDay, dealingGbn, estateAgentSggNm, rgstDate, aptDong, slerGbn, buyerGbn, landLeaseholdGbn |
| `apt_rent` 아파트 전월세 | RTMSDataSvcAptRent | sggCd, umdNm, aptNm, jibun, excluUseAr, dealYear, dealMonth, dealDay, deposit, monthlyRent, floor, buildYear, contractTerm, contractType, useRRRight, preDeposit, preMonthlyRent |
| `offi_sale` 오피스텔 매매 | RTMSDataSvcOffiTrade | sggCd, sggNm, umdNm, jibun, offiNm, excluUseAr, dealYear, dealMonth, dealDay, dealAmount, floor, buildYear, cdealType, cdealDay, dealingGbn, estateAgentSggNm, slerGbn, buyerGbn |
| `offi_rent` 오피스텔 전월세 | RTMSDataSvcOffiRent | sggCd, sggNm, umdNm, jibun, offiNm, excluUseAr, dealYear, dealMonth, dealDay, deposit, monthlyRent, floor, buildYear, contractType, contractTerm, useRRRight, preDeposit, preMonthlyRent |
| `rh_sale` 연립다세대 매매 | RTMSDataSvcRHTrade | sggCd, umdNm, mhouseNm, jibun, buildYear, excluUseAr, landAr, dealYear, dealMonth, dealDay, dealAmount, floor, cdealType, cdealDay, dealingGbn, estateAgentSggNm, rgstDate, slerGbn, buyerGbn, houseType |
| `rh_rent` 연립다세대 전월세 | RTMSDataSvcRHRent | sggCd, umdNm, mhouseNm, jibun, buildYear, excluUseAr, dealYear, dealMonth, dealDay, deposit, monthlyRent, floor, contractTerm, contractType, useRRRight, preDeposit, preMonthlyRent, houseType |
| `sh_sale` 단독/다가구 매매 | RTMSDataSvcSHTrade | sggCd, umdNm, houseType, jibun, totalFloorAr, plottageAr, dealYear, dealMonth, dealDay, dealAmount, buildYear, cdealType, cdealDay, dealingGbn, estateAgentSggNm, slerGbn, buyerGbn |
| `sh_rent` 단독/다가구 전월세 | RTMSDataSvcSHRent | sggCd, umdNm, houseType, totalFloorAr, dealYear, dealMonth, dealDay, deposit, monthlyRent, buildYear, contractTerm, contractType, useRRRight, preDeposit, preMonthlyRent |
| `presale` 분양·입주권 | RTMSDataSvcSilvTrade | sggCd, sggNm, umdNm, jibun, aptNm, floor, excluUseAr, ownershipGbn, dealYear, dealMonth, dealDay, dealAmount, cdealType, cdealDay, dealingGbn, estateAgentSggNm, slerGbn, buyerGbn |
| `land` 토지 | RTMSDataSvcLandTrade | sggCd, sggNm, umdNm, jibun, jimok, landUse, dealArea, dealingGbn, dealYear, dealMonth, dealDay, dealAmount, cdealType, cdealDay, estateAgentSggNm, shareDealingType |
| `commercial` 상업업무용 | RTMSDataSvcNrgTrade | sggCd, sggNm, umdNm, jibun, buildingType, buildingUse, landUse, floor, dealYear, dealMonth, dealDay, dealAmount, buildYear, buildingAr, plottageAr, cdealType, cdealDay, dealingGbn, shareDealingType, estateAgentSggNm, slerGbn, buyerGbn |
| `industrial` 공장·창고 | RTMSDataSvcInduTrade | sggCd, sggNm, umdNm, jibun, floor, landUse, plottageAr, buildingType, buildingAr, buildingUse, buildYear, dealingGbn, dealYear, dealMonth, dealDay, dealAmount, cdealType, cdealDay, estateAgentSggNm, shareDealingType, slerGbn, buyerGbn |

샘플 응답은 `tests/fixtures/*.xml` 에 있다 (모든 팀 공용, 읽기 전용).

### 2.2 원본 필드 → 표준 필드 매핑 (`Transaction`)

| 표준 필드 | 신규 API | 구 API (한글 태그) | 비고 |
|---|---|---|---|
| lawd_cd | (조회 파라미터 LAWD_CD) | | **조회에 쓴 코드**를 쓴다. 원본 sggCd 가 다르면 extra 에 보관 |
| sigungu | sggNm | 시군구 | 없으면 지역코드표 이름 |
| dong | umdNm | 법정동 | 앞뒤 공백 제거 |
| jibun | jibun | 지번 | 단독·토지는 마스킹(`3**`) |
| name | aptNm / offiNm / mhouseNm | 아파트 / 단지 / 연립다세대 | |
| deal_date | dealYear·dealMonth·dealDay | 년·월·일 | 필수 — 없으면 NormalizationError |
| floor | floor | 층 | 지하 음수 |
| area_m2 | excluUseAr · totalFloorAr(단독) · dealArea(토지) · buildingAr(상업·공장) | 전용면적 · 연면적/계약면적 · 거래면적 · 건물면적 | |
| land_area_m2 | landAr(연립) · plottageAr(단독·상업·공장) | 대지권면적 · 대지면적 | |
| build_year | buildYear | 건축년도 | 0 이하 → None |
| price | dealAmount | 거래금액 | `"  285,000"` → 285000 (만원) |
| deposit / monthly_rent | deposit / monthlyRent | 보증금액(보증금) / 월세금액(월세) | |
| is_cancelled / cancel_date | cdealType(`O`) / cdealDay(`25.01.28`) | 해제여부 / 해제사유발생일 | 날짜는 `YY.MM.DD` |
| deal_method | dealingGbn | 거래유형 | 중개거래/직거래 |
| agent_location | estateAgentSggNm | 중개사소재지 | |
| registration_date | rgstDate(`25.02.10`) | 등기일자 | |
| seller / buyer | slerGbn / buyerGbn | 매도자 / 매수자 | |
| building_dong | aptDong | 동 | |
| house_type | houseType | 주택유형 | |
| contract_type / contract_term | contractType / contractTerm | 계약구분 / 계약기간 | |
| renewal_right_used | useRRRight(`사용`) | 갱신요구권사용 | 공백 → None |
| prev_deposit / prev_monthly_rent | preDeposit / preMonthlyRent | 종전계약보증금 / 종전계약월세 | |
| complex_id | aptSeq | 일련번호 | |
| road_address | roadNm + roadNmBonbun(-roadNmBubun) | 도로명 + 도로명건물본번호코드… | `"샘플로 51"` (앞자리 0 제거) |
| extra | 위에 없는 나머지 필드 | | 빈 값 제외 |

## 3. 전체 흐름

```
 설정(silgeorae.toml) ─┐
                      ▼
 [CLI·자동화팀] cli ──► pipeline.run_collect ─────────────────────────────┐
                      │                                                  │
                      ▼                                                  ▼
 [수집팀] RegionTable.resolve("강남구") → ["11680"]      [정제·저장팀] TransactionStore
          MolitClient.fetch(유형, 시군구, 연월)                replace_partition()
            └ 페이지네이션·재시도·오류분류 → 원본 dict 목록 ──► normalize_items() ─┘
                                                                   │
 [분석·리포트팀] store.query(...) → build_report() → export_excel / export_html / export_csv
                                                                   │
 [CLI·자동화팀] notify(신규·해제·신고가 요약) ◄─────────────────────┘
```

## 4. 팀 편성과 소유 파일

| 팀 | 역할 | 소유 파일 (이 팀만 수정) |
|---|---|---|
| 총괄(아키텍트) | 설계, 공통 계약, 통합·검증 | `models.py`, `errors.py`, `utils.py`, `pyproject.toml`, `docs/ARCHITECTURE.md`, `tests/fixtures/`, `tests/test_models.py`, `tests/test_utils.py` |
| A. 수집팀 | API 호출, 지역코드 | `src/silgeorae/regions.py`, `src/silgeorae/collector/**`, `src/silgeorae/data/**`, `tests/collector/**` |
| B. 정제·저장팀 | 정규화, SQLite 저장·갱신 | `src/silgeorae/processing/**`, `tests/processing/**` |
| C. 분석·리포트팀 | 통계, 엑셀·CSV·HTML | `src/silgeorae/analysis/**`, `tests/analysis/**` |
| D. CLI·자동화팀 | 명령어, 설정, 파이프라인, 데모, 알림, 문서, CI | `src/silgeorae/cli.py`, `config.py`, `pipeline.py`, `demo.py`, `notify.py`, `console.py`, `tests/app/**`, `README.md`, `config.example.toml`, `examples/**`, `.github/**` |

공통 규칙: Python ≥ 3.10, 표준 라이브러리만 사용(`openpyxl` 은 함수 안에서 선택적 import),
모든 모듈 첫 줄 `from __future__ import annotations`, 사용자 메시지는 한국어,
라이브러리 코드에서 `print` 금지(`logging` 사용), 테스트는 네트워크 없이 동작.

## 5. 팀 간 계약 (공개 인터페이스)

### 5.1 공통 (`silgeorae.models`, `silgeorae.errors`, `silgeorae.utils`) — 구현 완료

* `DealType` (12종, `.label`, `.api_service`, `.api_title`, `.is_rent`, `.parse()`, `.parse_many()`)
* `Transaction` (표준 거래 1건, `.key`, `.natural_key()`, `.complex_key`, `.deal_ym`, `.pyeong`, `.area_type`, `.price_per_pyeong`, `.rent_kind`, `.price_text`, `.to_dict()/.from_dict()`)
* `assign_seq(transactions)` — 같은 배치 안 동일 거래 구분 순번
* 예외: `SilgeoraeError` ⊃ `ConfigError`, `RegionError`(⊃ `RegionNotFoundError`, `AmbiguousRegionError`), `ApiError`(⊃ `ServiceKeyError`, `QuotaExceededError`), `NormalizationError`, `StorageError`, `ExportError`
* 유틸: `parse_ym`, `ym_add`, `month_range`, `recent_months`, `ym_label`, `ym_of`, `ym_to_date`, `format_manwon`, `format_area`, `m2_to_pyeong`, `now_kst`, `today_kst`, `KST`

### 5.2 A. 수집팀

```python
# silgeorae.regions
@dataclass(frozen=True)
class Region:
    lawd_cd: str; sido: str; sigungu: str
    parent_cd: str = ""; is_leaf: bool = True; former_cd: str = ""; note: str = ""
    name: str        # property — "서울특별시 강남구" (세종: "세종특별자치시")
    short_name: str  # property — "강남구", "수원시 장안구" (세종: "세종특별자치시")

class RegionTable:
    def __init__(self, regions: Iterable[Region]) -> None
    @classmethod
    def load(cls, path: str | Path | None = None) -> RegionTable   # None → 번들 data/sigungu.csv
    def get(self, lawd_cd: str) -> Region | None
    def all(self, *, leaves_only: bool = False) -> list[Region]
    def search(self, text: str) -> list[Region]
    def resolve(self, query: str) -> list[Region]          # 조회 가능한 leaf 목록
    def resolve_many(self, queries: Iterable[str]) -> list[Region]
    def name_of(self, lawd_cd: str) -> str
    def save(self, path: str | Path) -> None

# silgeorae.collector
@dataclass
class HttpResponse:
    status: int; body: bytes; headers: dict[str, str] = {}
    text: str        # property (UTF-8 디코드)

Transport = Callable[[str, Mapping[str, str], float], HttpResponse]   # (url, params, timeout)
def urllib_transport(url, params, timeout) -> HttpResponse

@dataclass
class ParsedPage:
    items: list[dict[str, str]]; total_count: int; page_no: int; num_of_rows: int
    result_code: str; result_msg: str
def parse_response(body: bytes | str) -> ParsedPage     # 오류 응답이면 ApiError 계열 예외

class MolitClient:
    DEFAULT_BASE_URL = "https://apis.data.go.kr/1613000"
    def __init__(self, service_key: str, *, transport: Transport | None = None,
                 timeout: float = 20.0, max_retries: int = 3, backoff: float = 1.0,
                 page_size: int = 1000, min_interval: float = 0.0,
                 base_url: str = DEFAULT_BASE_URL,
                 service_overrides: Mapping[DealType, str] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None
    def url_for(self, deal_type: DealType) -> str
    def fetch(self, deal_type: DealType, lawd_cd: str, deal_ym: str) -> list[dict[str, str]]
    request_count: int   # property — 누적 HTTP 호출 수

def build_response_xml(items, *, total_count=None, page_no=1, num_of_rows=None,
                       result_code="000", result_msg="OK") -> str
def build_error_xml(reason_code: str, auth_msg: str, err_msg: str = "SERVICE ERROR") -> str
class FakeTransport:            # 오프라인 테스트·데모용 (Transport 구현)
    def __init__(self, data: Mapping[tuple[DealType, str, str], Sequence[Mapping]] | None = None) -> None
    def add(self, deal_type, lawd_cd, deal_ym, items) -> None
    def fail(self, deal_type, lawd_cd, deal_ym, response: HttpResponse, times: int | None = None) -> None
    calls: list[tuple[str, dict[str, str]]]
    def __call__(self, url, params, timeout) -> HttpResponse

def sync_regions(service_key: str, *, transport: Transport | None = None) -> RegionTable
    # 행정안전부 법정동코드 API(apis.data.go.kr/1741000/StanReginCd/getStanReginCdList)로 최신 시군구표 생성
```

### 5.3 B. 정제·저장팀

```python
# silgeorae.processing
def normalize_item(deal_type: DealType, raw: Mapping[str, Any], *,
                   lawd_cd: str = "", sigungu: str = "") -> Transaction
def normalize_items(deal_type: DealType, raws: Iterable[Mapping[str, Any]], *,
                    lawd_cd: str = "", sigungu: str = "", strict: bool = False,
                    on_error: Callable[[Mapping, Exception], None] | None = None) -> list[Transaction]
    # 잘못된 레코드는 건너뛰고(on_error 호출) strict=True 면 예외. 마지막에 assign_seq 적용.

@dataclass
class PartitionResult:
    inserted: int = 0; updated: int = 0; unchanged: int = 0; removed: int = 0
    newly_cancelled: int = 0
    new_keys: list[str] = []; cancelled_keys: list[str] = []
    def merge(self, other: PartitionResult) -> PartitionResult   # 합산 (in-place, self 반환)

@dataclass
class FetchLogEntry:
    deal_type: DealType; lawd_cd: str; deal_ym: str; fetched_at: datetime
    item_count: int; status: str; message: str

class TransactionStore:
    def __init__(self, path: str | Path = ":memory:") -> None
    def close(self) -> None      # + 컨텍스트 매니저
    def replace_partition(self, deal_type, lawd_cd, deal_ym, transactions, *,
                          fetched_at: datetime | None = None) -> PartitionResult
    def upsert(self, transactions, *, now: datetime | None = None) -> PartitionResult
    def query(self, *, deal_types=None, lawd_cds=None, start_ym=None, end_ym=None,
              name=None, dong=None, include_cancelled=True, min_area=None, max_area=None,
              first_seen_since: datetime | None = None, limit=None) -> list[Transaction]
    def count(self, **filters) -> int                 # query 와 같은 필터
    def get(self, key: str) -> Transaction | None
    def last_fetched(self, deal_type, lawd_cd, deal_ym) -> datetime | None   # status="ok" 기준
    def record_fetch(self, deal_type, lawd_cd, deal_ym, *, item_count: int, status: str = "ok",
                     message: str = "", fetched_at: datetime | None = None) -> None
    def fetch_log(self, *, limit: int | None = None, status: str | None = None) -> list[FetchLogEntry]
    def summary(self) -> list[dict[str, Any]]   # deal_type, lawd_cd, count, cancelled, min_ym, max_ym
```

### 5.4 C. 분석·리포트팀

```python
# silgeorae.analysis
@dataclass
class Table:
    name: str; columns: list[str]; rows: list[list[Any]]
    description: str = ""
    formats: dict[str, str] = {}   # 열 → "int" | "manwon" | "float1" | "float2" | "percent" | "date" | "text"

@dataclass
class Chart:
    title: str; kind: str                       # "bar" | "line" | "bar+line"
    labels: list[str]
    series: list[tuple[str, list[float | None]]]
    unit: str = ""

@dataclass
class Report:
    title: str; generated_at: datetime; period: tuple[str, str] | None
    regions: list[str]; kpis: list[tuple[str, str]]
    tables: list[Table]; charts: list[Chart]; notes: list[str]

def build_report(transactions, *, title="부동산 실거래 정리", regions=(), period=None,
                 new_since: datetime | None = None, top_n: int = 20,
                 today: date | None = None) -> Report
def transactions_table(transactions, name: str = "거래내역") -> Table
def export_excel(report: Report, path, *, transactions=None) -> Path   # openpyxl 없으면 ExportError
def export_csv(transactions, path) -> Path                             # utf-8-sig
def export_tables_csv(report: Report, directory) -> list[Path]
def export_html(report: Report, path) -> Path                          # 외부 리소스 없는 단일 파일
```

### 5.5 D. CLI·자동화팀

```python
# silgeorae.pipeline
@dataclass(frozen=True)
class CollectTask: deal_type: DealType; lawd_cd: str; deal_ym: str
@dataclass
class CollectResult:
    task: CollectTask; status: str   # "ok" | "skipped" | "error"
    item_count: int = 0; partition: PartitionResult | None = None; error: str = ""
@dataclass
class CollectSummary:
    results: list[CollectResult]; aborted: bool = False; abort_reason: str = ""; api_calls: int = 0
    # 합계 property: ok / skipped / errors / inserted / updated / removed / newly_cancelled / new_keys
def plan_tasks(deal_types, lawd_cds, months) -> list[CollectTask]
def run_collect(client, store, tasks, *, regions: RegionTable | None = None,
                refresh_months: int = 3, force: bool = False, today: date | None = None,
                progress: Callable[[int, int, CollectResult], None] | None = None) -> CollectSummary
```

명령어: `init`, `regions`, `collect`, `update`, `report`, `new`, `search`, `status`, `demo`.

## 6. 저장·갱신 전략

* **파티션** = (DealType, 시군구, 계약연월). API 1회 조회 단위와 같다.
* `replace_partition` 은 새로 받은 목록을 그 파티션의 "정답"으로 보고
  * 처음 보는 키 → INSERT (`first_seen_at` 기록 → "신규 거래")
  * 있던 키 → 값 비교 후 바뀌었으면 UPDATE (`해제여부`가 False→True 면 `newly_cancelled`)
  * 새 목록에 없는 기존 키 → DELETE (정정·삭제된 신고)
* 과거 달은 한 번 받으면 건너뛰고, **최근 `refresh_months`(기본 3)개월은 매번 다시 받는다.**
  (`--force` 로 전체 재수집)
* 키 규칙은 `Transaction.natural_key()` + `seq` — 해제·등기처럼 나중에 바뀌는 값은 키에 넣지 않는다.

## 7. 정리(분석) 항목

| 표/차트 | 내용 |
|---|---|
| 요약 KPI | 거래 건수, 해제 건수, 중위 거래가, 평균 평당가, 신고가 건수, 신규 등록 건수 |
| 월별 추이 | 월별 거래량(막대) + 중위가/평균 평당가(선) |
| 단지별 | 단지·면적타입별 거래 수, 최근 거래가, 최고/최저, 평균 평당가 |
| 면적대별 | ~60㎡ / 60~85㎡ / 85~135㎡ / 135㎡~ |
| 법정동별 | 동별 거래 수, 중위가 |
| 신고가 | 같은 단지·면적타입의 이전 최고가를 넘은 거래 |
| 해제 거래 | 해제여부 = O 목록 |
| 전월세 | 전세/월세 비중, 평균 보증금·월세, 갱신계약·갱신요구권 사용 비율 |
| 전세가율 | 같은 단지·면적타입의 전세 중위 보증금 ÷ 매매 중위가 |
| 신규 등록 | `new_since` 이후 처음 수집된 거래 |

## 7.1 지역코드표 (`src/silgeorae/data/sigungu.csv`)

행정안전부 법정동코드(2022-09 스냅샷)에서 말소되지 않은 시군구를 추출하고 이후 변경을 반영했다.

* 2023-06-11 강원특별자치도 출범 (42xxx → 51xxx)
* 2023-07-01 군위군 경북 → 대구 편입 (47720 → 27720)
* 2024-01-01 부천시 일반구 설치 (41192 원미구, 41194 소사구, 41196 오정구)
* 2024-01-18 전북특별자치도 출범 (45xxx → 52xxx)
* 출장소 코드 제외, `is_leaf=0` 인 12개는 구를 둔 시(조회 시 하위 구로 펼침)

그 뒤의 행정구역 개편(예: 2026년 인천 제물포구·영종구·검단구 신설, 화성시 일반구 설치)은
`silgeorae regions --sync`(행정안전부 법정동코드 API)로 최신화하거나 5자리 코드를 직접 입력한다.

## 8. 테스트 전략

* 팀별 단위 테스트: `tests/collector`, `tests/processing`, `tests/analysis`, `tests/app`
* 공용 픽스처: `tests/fixtures/*.xml` (실제 응답 형식의 샘플)
* 통합: `FakeTransport` + 합성 데이터로 `collect → store → report` 전 과정을 네트워크 없이 검증
* 실행: `python -m pytest`
