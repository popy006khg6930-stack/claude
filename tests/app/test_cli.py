from __future__ import annotations

import json

import pytest

from silgeorae import cli
from silgeorae.cli import main, report_basename
from silgeorae.config import default_config_text, load_config
from silgeorae.errors import ExportError
from silgeorae.models import DealType
from silgeorae.utils import recent_months

SALE, RENT = DealType.APT_SALE, DealType.APT_RENT


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    """``main([...])`` 를 임시 폴더에서 실행하고 (종료 코드, stdout, stderr) 를 돌려준다."""
    monkeypatch.chdir(tmp_path)

    def _run(*args):
        code = main([str(a) for a in args])
        out, err = capsys.readouterr()
        return code, out, err

    return _run


@pytest.fixture
def fake_api(monkeypatch):
    """CLI 가 만드는 ``MolitClient`` 가 가짜 API(FakeTransport)를 쓰게 한다."""
    from silgeorae import collector

    transport = collector.FakeTransport()
    real = collector.MolitClient

    def factory(key, **kwargs):
        kwargs.pop("transport", None)
        return real(key, transport=transport, sleep=lambda seconds: None, **kwargs)

    monkeypatch.setattr(collector, "MolitClient", factory)
    return transport


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    from silgeorae.demo import run_demo

    return run_demo(tmp_path_factory.mktemp("shared_demo"), months=6).db_path


# --------------------------------------------------------------------------- 도움말·사용법
def test_help_lists_commands(run):
    code, out, _ = run("--help")
    assert code == 0
    assert "사용법: silgeorae" in out
    for command in ("init", "regions", "collect", "update", "report", "new", "search", "status", "demo"):
        assert command in out


def test_subcommand_help_and_version(run):
    code, out, _ = run("collect", "-h")
    assert code == 0 and "--region" in out and "공통 옵션" in out
    code, out, _ = run("--version")
    assert code == 0 and out.startswith("silgeorae ")


def test_no_command_prints_help(run):
    code, out, _ = run()
    assert code == 0 and "사용법" in out


def test_usage_errors_exit_2_in_korean(run):
    code, _, err = run("collect", "--months", "0")
    assert code == 2 and "1 이상의 정수" in err
    code, _, err = run("unknown-command")
    assert code == 2 and "오류:" in err
    code, _, err = run("new", "--since", "2025-01-01", "--days", "2")
    assert code == 2 and "함께 쓸 수 없습니다" in err


# --------------------------------------------------------------------------- init
def test_init_writes_template_and_refuses_overwrite(run, tmp_path):
    code, out, _ = run("init")
    assert code == 0 and "설정 파일을 만들었습니다" in out
    assert (tmp_path / "silgeorae.toml").read_text(encoding="utf-8") == default_config_text()
    code, _, err = run("init")
    assert code == 2 and "이미 있습니다" in err and "--force" in err
    assert run("init", "--force")[0] == 0


def test_init_custom_path_with_key(run, tmp_path):
    code, _, _ = run("init", "--config", "conf/my.toml", "--key", "MY-KEY")
    assert code == 0
    cfg = load_config(tmp_path / "conf" / "my.toml", env={})
    assert cfg.service_key == "MY-KEY"


# --------------------------------------------------------------------------- regions
def test_regions_search(run):
    code, out, _ = run("regions", "강남")
    assert code == 0 and "11680" in out and "강남구" in out
    code, out, _ = run("regions", "성남")
    assert code == 0 and "41135" in out and "하위 구로 조회" in out


def test_regions_overview_and_all(run):
    code, out, _ = run("regions")
    assert code == 0 and "서울특별시" in out and "silgeorae regions 강남" in out
    code, out, _ = run("regions", "--all")
    assert code == 0 and "11680" in out and "52111" in out
    code, out, _ = run("regions", "우주정거장")
    assert code == 0 and "없습니다" in out


def test_regions_sync_requires_key(run):
    code, _, err = run("regions", "--sync")
    assert code == 2 and "행정안전부_행정표준코드_법정동코드" in err


def test_regions_sync_saves_table_used_afterwards(run, monkeypatch, tmp_path):
    from silgeorae import collector
    from silgeorae.regions import Region, RegionTable

    synced = RegionTable([
        Region("11680", "서울특별시", "강남구"),
        Region("28190", "인천광역시", "제물포구", note="2026-07-01 신설"),
    ])
    seen_keys = []
    monkeypatch.setattr(collector, "sync_regions", lambda key, **kw: seen_keys.append(key) or synced)
    code, out, _ = run("regions", "--sync", "--key", "SYNC-KEY")
    assert code == 0 and seen_keys == ["SYNC-KEY"]
    assert (tmp_path / "regions.csv").is_file() and "regions.csv" in out
    code, out, _ = run("regions", "제물포")
    assert code == 0 and "28190" in out and "2026-07-01 신설" in out
    code, out, _ = run("status")
    assert "regions.csv" in out


# --------------------------------------------------------------------------- collect
def test_ambiguous_region_lists_candidates(run):
    code, _, err = run("collect", "-r", "중구", "--key", "K")
    assert code == 2
    assert "후보" in err and "11140  서울특별시 중구" in err and "26110  부산광역시 중구" in err


def test_unknown_region_exit_2(run):
    code, _, err = run("collect", "-r", "없는구", "--key", "K")
    assert code == 2 and "찾을 수 없습니다" in err


def test_missing_key_shows_instructions(run):
    code, out, err = run("collect", "-r", "강남구")
    assert code == 2 and out == ""
    assert "공공데이터포털" in err and "SILGEORAE_SERVICE_KEY" in err
    assert DealType.APT_SALE.api_title in err and DealType.APT_RENT.api_title in err


def test_collect_needs_region(run):
    code, _, err = run("collect", "--key", "K")
    assert code == 2 and "-r" in err


def test_collect_bad_type_and_period(run):
    code, _, err = run("collect", "-r", "강남구", "-t", "우주선", "--key", "K")
    assert code == 2 and "거래 유형" in err
    code, _, err = run("collect", "-r", "강남구", "--from", "2025-05", "--to", "2025-01", "--key", "K")
    assert code == 2 and "늦습니다" in err


def test_collect_with_fake_api(run, fake_api, make_sale, tmp_path):
    months = recent_months(2)
    fake_api.add(SALE, "11680", months[0], [make_sale(months[0], day=1), make_sale(months[0], day=2, floor="3")])
    code, out, err = run("collect", "-r", "강남구", "-t", "아파트", "--months", "2", "--key", "TEST-KEY")
    assert code == 0, err
    assert "수집 대상: 지역 1곳 × 유형 1종 × 2개월" in out
    assert f"아파트 매매 · 서울특별시 강남구 · {months[0][:4]}.{months[0][4:]} → 2건 (신규 2" in out
    assert "신규 2" in out and (tmp_path / "silgeorae.db").is_file()
    assert fake_api.calls and fake_api.calls[0][1]["serviceKey"] == "TEST-KEY"

    code, out, _ = run("search", "-r", "강남구")
    assert code == 0 and "검색 결과: 2건" in out and "한빛마을1단지" in out


def test_collect_service_key_error_exits_1(run, fake_api):
    from silgeorae.collector import HttpResponse, build_error_xml

    error = HttpResponse(200, build_error_xml("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR").encode("utf-8"), {})
    fake_api.fail(None, None, None, error)
    code, out, err = run("collect", "-r", "강남구,마포구", "--months", "3", "--key", "BAD")
    assert code == 1
    assert "수집이 중단되었습니다" in out and "남은 작업" in out
    assert "활용신청" in err
    assert len(fake_api.calls) == 1


# --------------------------------------------------------------------------- update
def test_update_requires_regions(run):
    code, _, err = run("update", "--key", "K")
    assert code == 2 and "[watch]" in err and "silgeorae init" in err


def test_update_detects_new_deals_and_notifies(run, fake_api, make_sale, monkeypatch, tmp_path):
    from silgeorae import notify

    posts = []
    monkeypatch.setattr(notify, "_urllib_post", lambda url, data, headers: posts.append(json.loads(data)) or 200)
    monkeypatch.setenv("SILGEORAE_SERVICE_KEY", "ENV-KEY")
    (tmp_path / "silgeorae.toml").write_text(
        '[watch]\nregions = ["강남구"]\ndeal_types = ["apt_sale"]\nmonths = 2\n'
        '[report]\nformats = ["html", "csv"]\n'
        '[notify]\nwebhook_url = "https://hooks.example.invalid/x"\n',
        encoding="utf-8",
    )
    ym = recent_months(2)[0]  # 지난달 (항상 과거)
    first = make_sale(ym, day=1, price="85,000")
    fake_api.add(SALE, "11680", ym, [first])

    code, out, err = run("update")  # 첫 수집: 기존 거래는 '신규' 알림에서 뺀다
    assert code == 0, err
    assert "처음 수집한 지역·유형의 기존 거래 1건" in out and "새 소식이 없어" in out
    assert posts == []

    fake_api.replace(SALE, "11680", ym, [first, make_sale(ym, day=2, price="99,000", floor="15")])
    code, out, err = run("update")
    assert code == 0, err
    assert "[실거래 알림] 신규 거래 1건 (아파트 매매 1) · 신고가 1건" in out
    assert "9억 9,000만원" in out and "알림 전송 (웹훅): 성공" in out
    assert len(posts) == 1 and "9억 9,000만원" in posts[0]["text"] and posts[0]["content"]

    cancelled = make_sale(ym, day=1, price="85,000", cdealType="O", cdealDay=f"{ym[2:4]}.{ym[4:]}.20")
    fake_api.replace(SALE, "11680", ym, [cancelled, make_sale(ym, day=2, price="99,000", floor="15")])
    code, out, _ = run("update", "--no-notify", "--report")
    assert code == 0
    assert "해제 1건" in out and "해제된 거래 1건" in out
    assert len(posts) == 1  # --no-notify
    assert "리포트를 만들었습니다" in out
    assert list((tmp_path / "reports").glob("실거래_강남구_*.html"))


def test_update_aborted_before_any_fetch_does_not_claim_no_news(run, fake_api, tmp_path):
    from silgeorae.collector import HttpResponse, build_error_xml

    (tmp_path / "silgeorae.toml").write_text('[watch]\nregions = ["강남구"]\nmonths = 2\n', encoding="utf-8")
    error = HttpResponse(200, build_error_xml("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR").encode(), {})
    fake_api.fail(None, None, None, error)
    code, out, err = run("update", "--key", "K")
    assert code == 1
    assert "수집이 중단되었습니다" in out and "내일" in err
    assert "새로 등록되거나 해제된 거래가 없습니다" not in out


def test_status_shows_failed_fetches(run, tmp_path):
    from silgeorae.processing import TransactionStore

    with TransactionStore(tmp_path / "silgeorae.db") as store:
        store.record_fetch(SALE, "11680", "202501", item_count=0, status="error", message="서버 오류 (HTTP 500)")
        store.record_fetch(SALE, "11440", "202501", item_count=3)
    code, out, _ = run("status")
    assert code == 0
    assert "[오류]" in out and "서버 오류 (HTTP 500)" in out
    assert "마지막 수집이 실패한 작업 1건 (2025.01)" in out
    assert "(저장된 거래 없음)" in out


# --------------------------------------------------------------------------- demo 와 조회 명령
def test_demo_command(run, tmp_path):
    code, out, err = run("demo", "--out", "demo", "--months", "3")
    assert code == 0, err
    assert (tmp_path / "demo" / "demo.db").is_file()
    assert (tmp_path / "demo" / "demo_report.html").is_file()
    assert (tmp_path / "demo" / "demo_transactions.csv").is_file()
    assert "다음 단계" in out and "가상 데이터" in out


def test_status_on_demo_db(run, demo_db):
    code, out, _ = run("--db", demo_db, "status")
    assert code == 0
    assert "합계" in out and "아파트 매매" in out and "서울특별시 강남구" in out
    assert "[최근 수집 기록]" in out and "정상" in out
    code, out2, _ = run("status", "--db", demo_db)  # 공통 옵션은 명령 뒤에도 쓸 수 있다
    assert code == 0 and out2 == out


def test_report_on_demo_db(run, demo_db, tmp_path):
    code, out, err = run("--db", demo_db, "report", "-r", "강남구", "--months", "6", "--out", "rep",
                         "--format", "html,csv")
    assert code == 0, err
    months = recent_months(6)
    base = report_basename(["강남구"], months[0], months[-1])
    assert base == f"실거래_강남구_{months[0]}-{months[-1]}"
    assert (tmp_path / "rep" / f"{base}.html").is_file()
    assert (tmp_path / "rep" / f"{base}_거래내역.csv").is_file()
    assert list((tmp_path / "rep" / f"{base}_표").glob("*.csv"))
    assert "리포트를 만들었습니다" in out


def test_report_excel(run, demo_db, tmp_path):
    pytest.importorskip("openpyxl")
    code, out, err = run("--db", demo_db, "report", "-r", "강남구,마포구", "--format", "xlsx", "--out", ".")
    assert code == 0, err
    assert list(tmp_path.glob("실거래_강남구외1_*.xlsx"))


def test_report_export_failure_is_reported(run, demo_db, monkeypatch, tmp_path):
    import importlib.util

    from silgeorae import analysis

    def locked(*args, **kwargs):  # 예: 엑셀에서 파일을 열어 둔 경우
        raise ExportError("파일을 쓸 수 없습니다 — 엑셀에서 열려 있으면 닫고 다시 실행하세요")

    monkeypatch.setattr(analysis, "export_excel", locked)
    code, _, err = run("--db", demo_db, "report", "--format", "xlsx,html", "--out", "rep")
    assert code == 1
    assert "엑셀에서 열려 있으면 닫고" in err
    assert "pip install" not in err  # openpyxl 은 설치되어 있으므로 설치 안내는 하지 않는다
    assert list((tmp_path / "rep").glob("*.html"))  # 다른 형식은 계속 만든다

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a, **kw: None if name == "openpyxl" else real_find_spec(name, *a, **kw)
    )
    code, _, err = run("--db", demo_db, "report", "--format", "xlsx", "--out", "rep")
    assert code == 1 and "pip install" in err


def test_report_without_matching_data(run, demo_db):
    code, _, err = run("--db", demo_db, "report", "-r", "제주시")
    assert code == 1 and "collect" in err


def test_search_on_demo_db(run, demo_db, tmp_path):
    code, out, _ = run("--db", demo_db, "search", "--name", "한빛", "--min-area", "80", "--max-area", "90",
                       "--limit", "3", "--csv", "found.csv")
    assert code == 0
    assert "검색 결과" in out and "한빛마을1단지" in out and "… 외" in out
    assert (tmp_path / "found.csv").is_file()
    code, out, _ = run("--db", demo_db, "search", "--dong", "없는동")
    assert code == 0 and "검색 결과: 0건" in out


def test_new_on_demo_db(run, demo_db):
    code, out, _ = run("--db", demo_db, "new", "--days", "1")
    assert code == 0 and "새로 수집된 거래" in out
    code, out, _ = run("--db", demo_db, "new", "--since", "2000-01-01", "-r", "마포구", "-t", "아파트 전월세")
    assert code == 0 and "마포구" in out and "강남구" not in out.split("\n", 2)[-1]


def test_read_commands_without_db(run, tmp_path):
    for args in (["report"], ["search"], ["new"]):
        code, _, err = run(*args)
        assert code == 1 and "아직 수집한 데이터가 없습니다" in err
    code, out, _ = run("status")
    assert code == 0 and "아직 없음" in out
    assert not (tmp_path / "silgeorae.db").exists()


# --------------------------------------------------------------------------- 오류 처리
def test_bad_config_file_exit_2(run, tmp_path):
    (tmp_path / "silgeorae.toml").write_text("[watch]\nmonths = 0\n", encoding="utf-8")
    code, _, err = run("status")
    assert code == 2 and "months" in err
    code, _, err = run("--config", "missing.toml", "status")
    assert code == 2 and "찾을 수 없습니다" in err


def test_keyboard_interrupt_returns_130(run, monkeypatch):
    def interrupted(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_status", interrupted)
    code, _, err = run("status")
    assert code == 130 and "중단" in err


def test_unexpected_error_returns_1(run, monkeypatch):
    def broken(args):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "cmd_status", broken)
    code, _, err = run("status")
    assert code == 1 and "boom" in err and "-v" in err
    code, _, err = run("-v", "status")
    assert code == 1 and "Traceback" in err


def test_report_basename_is_filesystem_safe():
    assert report_basename([], "202401", "202506") == "실거래_전체_202401-202506"
    assert report_basename(["성남시 분당구", "강남구"], "202401", "202506") == "실거래_성남시분당구외1_202401-202506"
    assert "/" not in report_basename(["a/b:c"], "202401", "202401")
