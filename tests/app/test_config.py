from pathlib import Path

import pytest

from silgeorae.config import (
    ENV_SERVICE_KEY,
    ENV_SERVICE_KEY_ALT,
    Config,
    default_config_text,
    load_config,
    mask_secret,
    normalize_formats,
    parse_deal_types,
    require_service_key,
)
from silgeorae.errors import ConfigError
from silgeorae.models import DealType

REPO_ROOT = Path(__file__).resolve().parents[2]

FULL_TOML = """
[api]
service_key = "FILE-KEY"
timeout = 5
min_interval = 0.5

[api.services]
apt_sale = "RTMSDataSvcAptTradeDevNew"
"오피스텔" = "RTMSDataSvcOffiTrade2"

[storage]
db_path = "data/deals.db"
regions_file = "codes/regions.csv"

[watch]
regions = ["강남구", "11440", 41135]
deal_types = ["아파트", "apt_rent", "오피스텔 전월세"]
months = 24
refresh_months = 2

[report]
output_dir = "out/reports"
formats = ["excel", "CSV"]
title = "우리 동네 실거래"

[notify]
webhook_url = "https://hooks.example.invalid/abc"
telegram_bot_token = "123:ABC"
telegram_chat_id = 987654
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(env={})
    assert cfg.config_path is None
    assert cfg.service_key == ""
    assert cfg.db_path == Path("silgeorae.db")
    assert cfg.regions == []
    assert cfg.deal_types == [DealType.APT_SALE, DealType.APT_RENT]
    assert (cfg.months, cfg.refresh_months) == (12, 3)
    assert cfg.report_dir == Path("reports")
    assert cfg.report_formats == ["xlsx", "html"]
    assert cfg.report_title == "부동산 실거래 정리"
    assert cfg.api_timeout == 20.0 and cfg.api_min_interval == 0.0
    assert cfg.service_overrides == {}
    assert cfg.regions_file == Path("regions.csv")
    assert not cfg.notify_configured


def test_config_in_current_directory_is_used(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "silgeorae.toml", '[watch]\nregions = ["강남구"]\n')
    cfg = load_config(env={})
    assert cfg.config_path == (tmp_path / "silgeorae.toml").resolve()
    assert cfg.regions == ["강남구"]
    # 파일에 없는 경로도 설정 파일 폴더 기준
    assert cfg.db_path == tmp_path.resolve() / "silgeorae.db"
    assert cfg.regions_file == tmp_path.resolve() / "regions.csv"


def test_full_toml_parsing_and_relative_paths(tmp_path):
    path = write(tmp_path / "conf" / "my.toml", FULL_TOML)
    cfg = load_config(path, env={})
    base = path.resolve().parent
    assert cfg.config_path == path.resolve()
    assert cfg.service_key == "FILE-KEY"
    assert cfg.api_timeout == 5.0 and cfg.api_min_interval == 0.5
    assert cfg.service_overrides == {
        DealType.APT_SALE: "RTMSDataSvcAptTradeDevNew",
        DealType.OFFI_SALE: "RTMSDataSvcOffiTrade2",
    }
    assert cfg.db_path == base / "data" / "deals.db"
    assert cfg.regions_file == base / "codes" / "regions.csv"
    assert cfg.regions == ["강남구", "11440", "41135"]
    assert cfg.deal_types == [DealType.APT_SALE, DealType.APT_RENT, DealType.OFFI_RENT]
    assert (cfg.months, cfg.refresh_months) == (24, 2)
    assert cfg.report_dir == base / "out" / "reports"
    assert cfg.report_formats == ["xlsx", "csv"]
    assert cfg.report_title == "우리 동네 실거래"
    assert cfg.webhook_url == "https://hooks.example.invalid/abc"
    assert cfg.telegram_bot_token == "123:ABC" and cfg.telegram_chat_id == "987654"
    assert cfg.notify_configured and cfg.telegram_configured


def test_absolute_paths_are_kept(tmp_path):
    db = (tmp_path / "elsewhere" / "x.db").resolve()
    path = write(tmp_path / "c.toml", f"[storage]\ndb_path = '{db.as_posix()}'\n")
    assert load_config(path, env={}).db_path == Path(db.as_posix())


def test_comma_separated_strings_are_accepted(tmp_path):
    path = write(tmp_path / "c.toml", '[watch]\nregions = "강남구, 마포구"\ndeal_types = "apt_sale,apt_rent"\n')
    cfg = load_config(path, env={})
    assert cfg.regions == ["강남구", "마포구"]
    assert cfg.deal_types == [DealType.APT_SALE, DealType.APT_RENT]


def test_utf8_bom_is_accepted(tmp_path):
    path = tmp_path / "bom.toml"
    path.write_bytes("\ufeff[watch]\nregions = [\"마포구\"]\n".encode("utf-8"))
    assert load_config(path, env={}).regions == ["마포구"]


def test_missing_explicit_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="찾을 수 없습니다"):
        load_config(tmp_path / "nope.toml", env={})


def test_invalid_toml_raises(tmp_path):
    path = write(tmp_path / "bad.toml", "[watch\nregions = \n")
    with pytest.raises(ConfigError, match="TOML"):
        load_config(path, env={})


def test_windows_path_escape_hint(tmp_path):
    path = write(tmp_path / "win.toml", '[storage]\ndb_path = "C:\\Users\\me\\x.db"\n')
    with pytest.raises(ConfigError, match="작은따옴표"):
        load_config(path, env={})


def test_non_utf8_file_raises(tmp_path):
    path = tmp_path / "cp949.toml"
    path.write_bytes('[report]\ntitle = "실거래"\n'.encode("cp949"))
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(path, env={})


@pytest.mark.parametrize(
    "text, fragment",
    [
        ('[watch]\ndeal_types = ["우주정거장"]\n', "deal_types"),
        ("[watch]\ndeal_types = []\n", "하나 이상"),
        ("[watch]\nmonths = 0\n", "months"),
        ('[watch]\nmonths = "12"\n', "정수"),
        ("[watch]\nmonths = true\n", "정수"),
        ("[watch]\nrefresh_months = -1\n", "refresh_months"),
        ('[watch]\nregions = [1.5]\n', "regions"),
        ("[watch]\nregions = 3\n", "regions"),
        ('[report]\nformats = ["pdf"]\n', "pdf"),
        ("[report]\nformats = []\n", "하나 이상"),
        ("[api]\ntimeout = 0\n", "timeout"),
        ("[api]\nmin_interval = -1\n", "min_interval"),
        ("[api]\nservice_key = 1234\n", "service_key"),
        ('[api.services]\nspaceship = "X"\n', "spaceship"),
        ('[api.services]\napt_sale = ""\n', "서비스명"),
        ('watch = "강남구"\n', "[watch]"),
    ],
)
def test_validation_errors(tmp_path, text, fragment):
    path = write(tmp_path / "c.toml", text)
    with pytest.raises(ConfigError) as info:
        load_config(path, env={})
    assert fragment in str(info.value)


def test_unknown_keys_are_warned(tmp_path, caplog):
    path = write(tmp_path / "c.toml", '[watch]\nregionz = ["강남구"]\n[extra]\na = 1\n')
    with caplog.at_level("WARNING", logger="silgeorae.config"):
        cfg = load_config(path, env={})
    assert cfg.regions == []
    assert "regionz" in caplog.text and "[extra]" in caplog.text


def test_service_key_precedence(tmp_path):
    path = write(tmp_path / "c.toml", '[api]\nservice_key = "FROM-FILE"\n')
    assert load_config(path, env={}).service_key == "FROM-FILE"
    assert load_config(path, env={ENV_SERVICE_KEY_ALT: "ALT"}).service_key == "ALT"
    cfg = load_config(path, env={ENV_SERVICE_KEY: "PRIMARY", ENV_SERVICE_KEY_ALT: "ALT"})
    assert cfg.service_key == "PRIMARY"
    assert ENV_SERVICE_KEY in cfg.service_key_source
    # 명령행 --key 가 가장 우선
    assert require_service_key(cfg, "FROM-CLI") == "FROM-CLI"
    assert require_service_key(cfg, None) == "PRIMARY"
    assert require_service_key(cfg, "   ") == "PRIMARY"


def test_env_variables_are_read_from_os_environ_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ENV_SERVICE_KEY, "  FROM-ENV  ")
    monkeypatch.setenv("SILGEORAE_WEBHOOK_URL", "https://hooks.example.invalid/x")
    cfg = load_config()
    assert cfg.service_key == "FROM-ENV"
    assert cfg.webhook_url == "https://hooks.example.invalid/x" and cfg.notify_configured


def test_notify_env_overrides(tmp_path):
    path = write(tmp_path / "c.toml", '[notify]\ntelegram_bot_token = "file"\ntelegram_chat_id = "1"\n')
    cfg = load_config(path, env={"SILGEORAE_TELEGRAM_BOT_TOKEN": "env", "SILGEORAE_TELEGRAM_CHAT_ID": "2"})
    assert (cfg.telegram_bot_token, cfg.telegram_chat_id) == ("env", "2")


def test_missing_service_key_gives_instructions():
    cfg = Config()
    with pytest.raises(ConfigError) as info:
        require_service_key(cfg)
    message = str(info.value)
    for fragment in ("공공데이터포털", "data.go.kr", "회원가입", "활용신청", "마이페이지", "Decoding",
                     "service_key", "SILGEORAE_SERVICE_KEY", DealType.APT_SALE.api_title,
                     DealType.APT_RENT.api_title):
        assert fragment in message
    with pytest.raises(ConfigError) as info:
        require_service_key(cfg, deal_types=[DealType.OFFI_SALE])
    assert DealType.OFFI_SALE.api_title in str(info.value)
    assert DealType.APT_SALE.api_title not in str(info.value)


def test_normalize_formats():
    assert normalize_formats("Excel, HTML,csv,xlsx") == ["xlsx", "html", "csv"]
    assert normalize_formats([".htm"]) == ["html"]
    with pytest.raises(ConfigError, match="pdf"):
        normalize_formats("pdf")
    with pytest.raises(ConfigError):
        normalize_formats(" , ")


def test_parse_deal_types():
    assert parse_deal_types(["아파트 전월세", "offi_sale"]) == [DealType.APT_RENT, DealType.OFFI_SALE]
    with pytest.raises(ConfigError, match="우주"):
        parse_deal_types(["우주"])


def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret("abc") == "***"
    assert mask_secret("abcdefghijklmnop") == "abcd…mnop"


def test_template_matches_example_file():
    example = REPO_ROOT / "config.example.toml"
    assert example.read_text(encoding="utf-8") == default_config_text()


def test_template_loads_to_default_values(tmp_path):
    path = write(tmp_path / "silgeorae.toml", default_config_text())
    cfg = load_config(path, env={})
    defaults = Config()
    base = path.resolve().parent
    assert cfg.service_key == defaults.service_key
    assert cfg.db_path == base / defaults.db_path
    assert cfg.report_dir == base / defaults.report_dir
    assert cfg.regions_file == base / "regions.csv"
    for name in ("regions", "deal_types", "months", "refresh_months", "report_formats", "report_title",
                 "webhook_url", "telegram_bot_token", "telegram_chat_id", "api_timeout", "api_min_interval",
                 "service_overrides"):
        assert getattr(cfg, name) == getattr(defaults, name), name
