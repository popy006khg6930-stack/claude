# silgeorae — 부동산 실거래가 자동 정리

국토교통부 **부동산 실거래가 공개 API**(공공데이터포털)에서 관심 지역의 거래를 모아
SQLite 에 쌓아 두고, **엑셀·HTML·CSV 리포트**와 **신규·해제·신고가 알림**으로 정리해 주는 명령행 도구입니다.

```text
$ silgeorae update --report
…
[실거래 알림] 신규 거래 6건 (아파트 매매 1, 아파트 전월세 5)
· [아파트 전월세] 강남구 대치동 한빛마을1단지 59.97㎡ 6층 전세 11억 8,000만원 (2026-09-24)
· [아파트 매매] 마포구 아현동 가람뜰 59.98㎡ 6층 13억 6,500만원 (2026-09-19)
· [아파트 전월세] 마포구 공덕동 하늘정원 101.9㎡ 7층 월세 1억 3,000만원/495만원 (2026-09-08)
…
리포트를 만들었습니다 (2025.10~2026.09, 거래 483건, 서울특별시 강남구, 서울특별시 마포구):
  reports/실거래_강남구외1_202510-202609.xlsx
  reports/실거래_강남구외1_202510-202609.html
```

> 위 예시와 `silgeorae demo` 의 단지명·가격은 모두 **가상 데이터**입니다.

## 주요 기능

- **12종 거래 유형** — 아파트·오피스텔·연립다세대·단독/다가구의 매매·전월세, 분양·입주권 전매, 토지, 상업업무용, 공장·창고
- **지역 이름으로 입력** — `강남구`, `분당`, `서울 중구`, `성남시`(소속 구 전체), `경기`(도 전체), 5자리 코드 모두 가능
- **똑똑한 재수집** — 과거 달은 한 번만 받고, 신고·해제가 계속 반영되는 **최근 3개월은 매번 다시** 받아 정확도 유지
- **변화 감지** — 새로 신고된 거래, 새로 해제된 거래, 같은 단지·면적의 **신고가**를 찾아 콘솔·Slack·Discord·텔레그램으로 알림
- **리포트** — 엑셀(시트별 요약·차트), 파일 하나로 열리는 HTML(차트 포함), 엑셀 호환 CSV
- **자동 실행** — cron, Windows 작업 스케줄러, GitHub Actions 예제 포함
- **인증키 없이 체험** — `silgeorae demo` 가 가상 데이터로 수집 → 저장 → 리포트 전 과정을 실행
- **가벼움** — 파이썬 표준 라이브러리만 사용 (엑셀 파일 만들 때만 `openpyxl`)

## 설치

Python 3.10 이상이 필요합니다.

```bash
git clone <이 저장소 주소> silgeorae
cd silgeorae
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[excel]"          # 엑셀(xlsx) 리포트까지 (openpyxl 포함)
silgeorae --help
```

- 엑셀이 필요 없으면 `pip install -e .` 만 해도 됩니다 (HTML·CSV 리포트는 그대로 동작).
- `silgeorae` 명령을 찾지 못하면 `python -m silgeorae …` 로 실행해도 똑같습니다.

## 인증키 발급 (공공데이터포털)

실제 데이터를 받으려면 공공데이터포털의 **일반 인증키**가 필요합니다 (무료).

1. [공공데이터포털(data.go.kr)](https://www.data.go.kr) 회원가입 후 로그인
2. 아래 API 를 검색해 각각 **활용신청** (활용 목적은 간단히 적으면 됩니다)
3. **개발계정은 자동 승인**됩니다 — 신청 즉시 '승인' 상태가 됩니다
4. **마이페이지 → 데이터활용 → Open API → 활용신청 현황**에서 신청한 API 를 열고
   **일반 인증키(Decoding)** 를 복사합니다 (인증키는 계정마다 하나, 모든 API 에 공통)
5. 인증키를 다음 중 한 곳에 넣습니다 (위에 있을수록 우선)
   - 명령마다 `--key 인증키`
   - 환경변수 `SILGEORAE_SERVICE_KEY` (또는 `DATA_GO_KR_SERVICE_KEY`)
   - 설정 파일 `silgeorae.toml` 의 `[api] service_key`

| 유형 코드 | 거래 유형 | 활용신청할 API 이름 | |
|---|---|---|---|
| `apt_sale` | 아파트 매매 | 국토교통부_아파트 매매 실거래가 상세 자료 | **필수** (기본 수집 대상) |
| `apt_rent` | 아파트 전월세 | 국토교통부_아파트 전월세 실거래가 자료 | **필수** (기본 수집 대상) |
| `offi_sale` | 오피스텔 매매 | 국토교통부_오피스텔 매매 실거래가 자료 | 필요할 때 |
| `offi_rent` | 오피스텔 전월세 | 국토교통부_오피스텔 전월세 실거래가 자료 | 필요할 때 |
| `rh_sale` | 연립다세대 매매 | 국토교통부_연립다세대 매매 실거래가 자료 | 필요할 때 |
| `rh_rent` | 연립다세대 전월세 | 국토교통부_연립다세대 전월세 실거래가 자료 | 필요할 때 |
| `sh_sale` | 단독/다가구 매매 | 국토교통부_단독/다가구 매매 실거래가 자료 | 필요할 때 |
| `sh_rent` | 단독/다가구 전월세 | 국토교통부_단독/다가구 전월세 실거래가 자료 | 필요할 때 |
| `presale` | 분양·입주권 전매 | 국토교통부_아파트 분양권전매 실거래가 자료 | 필요할 때 |
| `land` | 토지 매매 | 국토교통부_토지 매매 실거래가 자료 | 필요할 때 |
| `commercial` | 상업업무용 매매 | 국토교통부_상업업무용 부동산 매매 실거래가 자료 | 필요할 때 |
| `industrial` | 공장·창고 매매 | 국토교통부_공장 및 창고 등 부동산 매매 실거래가 자료 | 필요할 때 |
| (지역코드 최신화) | `regions --sync` 용 | 행정안전부_행정표준코드_법정동코드 | 선택 |

알아 두면 좋은 점

- 승인 직후에는 키가 동작하기까지 **최대 1시간 정도** 걸릴 수 있습니다. 그 사이에는 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR` 가 날 수 있으니 잠시 뒤 다시 실행하세요.
- 인증키는 **Decoding** 값을 권장하지만, **Encoding 값(`%` 가 들어 있는 키)을 넣어도 자동으로 처리**됩니다.
- 수집하려는 유형의 API 를 활용신청하지 않았다면 인증키 오류로 수집이 멈추고, 어떤 API 를 신청해야 하는지 안내가 나옵니다.
- 인증키 없이 `silgeorae collect` 를 실행하면 위 절차가 그대로 화면에 안내됩니다.

## 빠른 시작

```bash
# 1) 인증키 없이 체험 — 가상 데이터로 demo_output/ 에 DB·리포트 생성
silgeorae demo
silgeorae --db demo_output/demo.db search --name 한빛 --limit 10

# 2) 설정 파일 만들기 → silgeorae.toml 을 열어 인증키와 관심 지역 입력
silgeorae init

# 3) 수집 — 강남구 아파트 매매·전월세 최근 12개월
silgeorae collect -r 강남구 --months 12

# 4) 리포트 — reports/ 폴더에 엑셀·HTML
silgeorae report -r 강남구

# 5) 매일 한 번 — 관심 지역 새 거래 확인 + 알림 + 리포트 (자동 실행용)
silgeorae update --report
```

`demo` 는 "일주일 전 상황으로 한 번, 오늘 상황으로 한 번" 두 번 수집해서 `update` 가
새로 신고된 거래와 해제된 거래를 어떻게 찾아내는지도 보여 줍니다.
결과물은 `demo_output/demo.db`, `demo_report.html`, `demo_report.xlsx`(openpyxl 이 있을 때), `demo_transactions.csv` 입니다.

## 명령어 요약

| 명령 | 하는 일 | 예 |
|---|---|---|
| `init` | 주석 달린 설정 파일(`silgeorae.toml`) 만들기 (`--force` 덮어쓰기) | `silgeorae init` |
| `regions` | 지역 이름·코드 찾기, `--all` 전체 목록, `--sync` 최신 코드표 받기 | `silgeorae regions 분당` |
| `collect` | 지정한 지역·유형·기간 수집 (`--force` 이미 받은 달도 다시) | `silgeorae collect -r 강남구 -r 마포구 -t 아파트,아파트전월세 --from 2024-01` |
| `update` | 설정 파일의 관심 지역·유형 갱신 → 신규·해제·신고가 출력·알림 (`--report`, `--no-notify`) | `silgeorae update --report` |
| `report` | 엑셀·HTML·CSV 리포트 (`--format`, `--out`, `--title`, `--history-months`) | `silgeorae report -r 강남구 --months 24 --format xlsx,html` |
| `new` | 최근 새로 수집된(신고된) 거래 (`--days N` 또는 `--since 날짜`) | `silgeorae new --days 7 -r 마포구` |
| `search` | 저장된 거래 검색 (`--name`, `--dong`, `--min-area`, `--max-area`, `--csv`) | `silgeorae search --name 한빛 --min-area 80 --max-area 90 --csv 결과.csv` |
| `status` | 설정·인증키·DB 건수·최근 수집 기록(오류 포함) | `silgeorae status` |
| `demo` | 가상 데이터로 전체 흐름 체험 | `silgeorae demo --out demo_output --months 24` |

- **공통 옵션** (명령 앞·뒤 어디에나): `--config PATH` 설정 파일, `--db PATH` DB 파일, `--key KEY` 인증키, `-v` 자세한 로그
- **지역 입력**: `강남구`·`강남`, `서울 중구`(같은 이름이 여러 곳이면 시도를 붙이거나 코드 사용), `성남시`(수정·중원·분당구 전체), `경기`(도 전체), `11680`, 쉼표로 여러 곳 (`-r 강남구,마포구`)
- **유형 입력**: 코드(`apt_sale`) 또는 한글(`아파트`, `아파트 전월세`, `오피스텔`, `빌라 월세`, `토지`), `housing`(주거용 8종), `all`(12종)
- **기간**: 기본은 이번 달을 포함한 최근 `months`(12)개월. `--months 24`, `--from 2024-01 --to 2024-12` 로 바꿉니다.
- **종료 코드**: `0` 성공 · `1` 실행 중 오류(API 오류·수집 중단·조회 결과 없음 등) · `2` 설정·입력 오류 · `130` Ctrl+C 중단

## 설정 파일 (`silgeorae.toml`)

`silgeorae init` 이 만들어 주는 파일(내용은 [`config.example.toml`](config.example.toml) 과 같음)에 필요한 곳만 고쳐 씁니다.
명령을 실행한 폴더의 `silgeorae.toml` 을 자동으로 읽고, 다른 위치라면 `--config` 로 지정합니다.
**파일 안의 상대 경로는 설정 파일이 있는 폴더 기준**입니다.

```toml
[api]
service_key = ""            # 일반 인증키(Decoding). 비워 두고 환경변수를 써도 됨
timeout = 20                # 요청 제한 시간(초)
min_interval = 0            # 요청 사이 최소 간격(초)

[api.services]              # API 서비스명이 바뀌었을 때만: 유형 코드 = "서비스명"

[storage]
db_path = "silgeorae.db"    # 거래를 모아 두는 SQLite 파일
regions_file = "regions.csv"  # regions --sync 결과 (있으면 내장 코드표 대신 사용)

[watch]                     # update 가 확인할 대상
regions = ["강남구", "마포구", "성남시 분당구"]
deal_types = ["apt_sale", "apt_rent"]
months = 12                 # 이번 달 포함 최근 N개월
refresh_months = 3          # 이미 받았어도 매번 다시 받을 최근 개월 수

[report]
output_dir = "reports"
formats = ["xlsx", "html"]  # xlsx, html, csv
title = "부동산 실거래 정리"

[notify]                    # 비워 두면 알림을 보내지 않음
webhook_url = ""            # Slack 또는 Discord 웹훅 주소
telegram_bot_token = ""     # @BotFather 로 만든 봇 토큰
telegram_chat_id = ""       # 메시지를 받을 채팅 ID
```

환경변수(설정 파일보다 우선) — 비밀 값을 파일에 적고 싶지 않을 때 씁니다.

| 환경변수 | 설정 항목 |
|---|---|
| `SILGEORAE_SERVICE_KEY` (또는 `DATA_GO_KR_SERVICE_KEY`) | `[api] service_key` |
| `SILGEORAE_WEBHOOK_URL` | `[notify] webhook_url` |
| `SILGEORAE_TELEGRAM_BOT_TOKEN`, `SILGEORAE_TELEGRAM_CHAT_ID` | `[notify] telegram_*` |

설정 값이 잘못되면(없는 유형, `months = 0`, 모르는 리포트 형식 등) 어느 항목이 왜 틀렸는지 알려 주고 종료 코드 2로 끝납니다.
윈도우 경로는 `'C:\data\silgeorae.db'` 처럼 **작은따옴표**로 감싸거나 `/` 를 쓰세요. 메모장으로 편집할 때는 UTF-8 로 저장합니다.

### 알림

`update` 는 새로 신고된 거래·새로 해제된 거래가 있을 때 `[notify]` 에 설정된 곳으로 메시지를 보냅니다
(`--no-notify` 로 끄기). 같은 단지·면적타입의 이전 최고가를 넘은 매매에는 `· 신고가` 가 붙습니다.

- **Slack**: 앱의 Incoming Webhook URL (`https://hooks.slack.com/services/…`)
- **Discord**: 채널 설정 → 연동 → 웹후크 URL (메시지는 2,000자에서 잘림)
- **텔레그램**: @BotFather 로 봇을 만들어 토큰을 받고, 봇에게 말을 건 뒤 채팅 ID 를 넣습니다 (4,096자에서 잘림)

처음 수집하는 지역·유형의 기존 거래는 '신규'로 치지 않으므로, 관심 지역을 추가한 날 알림이 수백 건 쏟아지지 않습니다.

## 자동 실행

매일 한 번 `silgeorae update --report` 를 실행해 두면 새 거래 알림과 최신 리포트를 받아 볼 수 있습니다.
신고가 늦게 올라오는 경우가 많으므로 하루 한 번(아침)이면 충분합니다.

### Linux / macOS — cron

`crontab -e` 로 열어 한 줄을 추가합니다 ([`examples/crontab.txt`](examples/crontab.txt) 참고).

```cron
# 매일 07:05 에 실행 (서버 시간 기준 — 서버가 UTC 라면 22:05 로)
5 7 * * * cd $HOME/silgeorae && .venv/bin/silgeorae update --report >> update.log 2>&1
```

### Windows — 작업 스케줄러

1. [`examples/run_update.bat`](examples/run_update.bat) 을 설정 파일(`silgeorae.toml`)이 있는 폴더로 복사합니다 (가상환경 `.venv` 가 있으면 자동으로 사용).
2. **작업 스케줄러 → 기본 작업 만들기** → 트리거 "매일" 오전 7:00 → 동작 "프로그램 시작" → `run_update.bat` 선택.
3. 결과는 같은 폴더의 `logs\update.log` 에 쌓입니다.

### GitHub Actions

PC 를 켜 두지 않아도 GitHub 가 매일 실행하게 할 수 있습니다.
[`examples/github-actions-update.yml`](examples/github-actions-update.yml) 을 `.github/workflows/` 로 복사하고,
저장소 **Settings → Secrets and variables → Actions** 에 `DATA_GO_KR_SERVICE_KEY`(Secret)와
`SILGEORAE_CONFIG`(Variable, 설정 파일 내용)를 등록하면 됩니다. DB 는 Actions 캐시에 보관하고,
리포트는 실행 결과(Artifacts)로 내려받습니다. 자세한 내용은 파일 안의 주석을 보세요.

## 결과물

### 엑셀 (`.xlsx`)

| 시트 | 내용 |
|---|---|
| 요약 | 제목·기간·지역, 주요 지표(거래 건수, 해제 건수, 중위 거래가, 평균 평당가, 신고가·신규 건수), 시트 목록 |
| 월별 추이 | 유형·계약월별 거래량과 중위가·평균 평당가 (신고 기한이 안 끝난 최근 달 표시) |
| 단지별 요약 | 단지·면적타입별 거래 수, 최근 거래가, 최고·최저가, 평균 평당가 |
| 법정동별 | 동별 거래 수와 중위가 |
| 면적대별 | ~60㎡ / 60~85㎡ / 85~135㎡ / 135㎡~ 구간별 거래 |
| 신고가 | 같은 단지·면적타입의 이전 최고가를 넘은 매매 |
| 해제 거래 | 계약 후 해제(취소)된 거래 |
| 전월세 요약 | 전세·월세 비중, 평균 보증금·월세, 갱신계약·갱신요구권 사용 비율 |
| 전세가율 | 같은 단지·면적타입의 전세 중위 보증금 ÷ 매매 중위가 |
| 고가 거래 TOP20 | 유형별 거래금액 상위 거래 |
| 신규 등록 거래 | 최근 새로 수집된 거래 (`report` 는 기본 최근 7일 `--new-days`, `update --report` 는 이번 실행분) |
| 거래내역 | 기간 안의 모든 거래 (필터·정렬해서 쓰기 좋게) |
| 차트 | 월별 거래량·가격 차트와 차트 데이터 |

해당 자료가 없는 시트(예: 매매만 수집했을 때의 전월세 요약)는 만들어지지 않습니다.

### HTML (`.html`)

같은 내용을 **파일 하나**로 담은 리포트입니다. 인터넷 연결 없이 브라우저에서 바로 열리고, 차트가 포함되어 있어
메일·메신저로 공유하기 좋습니다.

### CSV (`.csv`)

`--format csv` 를 주면 거래내역 CSV 와 표마다 CSV 1개(`…_표/` 폴더)를 만듭니다.
UTF-8(BOM) 이라 엑셀에서 더블클릭해도 한글이 깨지지 않습니다. `search --csv 파일.csv` 로 검색 결과만 저장할 수도 있습니다.

파일 이름은 `실거래_강남구외1_202501-202512.xlsx` 처럼 지역과 기간으로 붙습니다.

## 데이터 유의사항

- **계약일 기준**입니다. 실거래가는 계약일로 집계되며, 신고일·등기일과 다릅니다.
- **신고 기한은 계약 후 30일**이라 최근 1~2개월은 계속 거래가 늘어납니다. 그래서 최근 `refresh_months`(기본 3)개월은
  매번 다시 받고, 새로 나타난 거래를 '신규', 사라진 거래는 정정·삭제로 반영합니다. 리포트의 최근 달 수치는 잠정치로 보세요.
- **해제 거래**: 계약 후 해제(취소)되면 나중에 '해제여부 O'와 해제일이 채워집니다. 해제 거래는 건수·가격 통계에서 빼고
  '해제 거래' 시트에 따로 모읍니다. 등기일자도 시간이 지나며 채워집니다.
- **신고가**는 이 프로그램이 **수집한 자료 안에서** 이전 최고가를 넘은 거래입니다. 수집 기간이 짧으면 실제보다 많게 잡힐 수 있으니
  `--history-months`(기본 36) 기간만큼 과거 자료를 모아 두면 정확해집니다.
- **지역코드 변경**: 강원·전북 특별자치도 출범, 군위군 대구 편입, 부천시 일반구 설치 등은 내장 코드표에 반영되어 있습니다.
  그 뒤의 행정구역 개편은 `silgeorae regions --sync`(행정안전부 법정동코드 API)로 코드표를 최신화하거나 5자리 코드를 직접 입력하세요.
- **일일 호출 한도(트래픽)**: API 마다 하루 호출 수가 제한됩니다(마이페이지에서 확인). 한 번 조회는 (유형, 시군구, 월) 1건이라
  12개월 × 2유형 × 3지역 = 호출 약 72회, 이후 매일 `update` 는 최근 3개월분(약 18회)만 받습니다.
  한도를 넘으면 수집이 멈추고, 다음 날 같은 명령을 다시 실행하면 받은 달은 건너뛰고 이어서 받습니다.
- 단독·다가구·토지 등의 지번은 원본에서 일부 가려져(`3**`) 제공됩니다.
- 데이터 출처: 국토교통부 실거래가 공개 자료 (공공데이터포털 Open API).

## 프로젝트 구조와 팀 구성

설계와 모듈 간 계약은 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 에 있습니다. 네 팀이 모듈 경계를 나눠 개발했습니다.

```text
src/silgeorae/
├── models.py, errors.py, utils.py   공통 계약 (총괄)
├── regions.py, data/sigungu.csv     지역코드표·지역 이름 해석          ─┐ A. 수집팀
├── collector/                       API 호출·재시도·오류 분류·FakeTransport ─┘
├── processing/                      원본 → Transaction 정규화, SQLite 저장·변경 감지   B. 정제·저장팀
├── analysis/                        통계, 엑셀·HTML·CSV 리포트                        C. 분석·리포트팀
├── cli.py, config.py, pipeline.py   명령어, 설정, 수집 파이프라인                    ─┐
├── notify.py, console.py, demo.py   알림, 콘솔 표, 오프라인 데모                      ─┘ D. CLI·자동화팀
tests/                               팀별 테스트 (collector, processing, analysis, app) + 공용 픽스처
examples/                            cron·작업 스케줄러·GitHub Actions 예제
```

처리 흐름: `cli` → `pipeline.run_collect` → `MolitClient.fetch`(유형·시군구·월) → `normalize_items` →
`TransactionStore.replace_partition`(신규·변경·삭제·해제 감지) → `build_report` → `export_excel / export_html / export_csv`
→ `notify`.

## 개발·테스트

```bash
pip install -e ".[dev]"      # pytest + openpyxl
python -m pytest             # 네트워크 없이 전체 테스트
python -m pytest tests/app   # CLI·자동화 부분만
```

테스트는 공용 XML 픽스처(`tests/fixtures/`)와 `FakeTransport` 로 실제 API 없이 수집 → 저장 → 리포트 전 과정을 검증합니다.
GitHub Actions 에서 Python 3.10~3.13 으로 자동 실행됩니다 (`.github/workflows/ci.yml`).
