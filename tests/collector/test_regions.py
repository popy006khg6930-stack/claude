from __future__ import annotations

import logging

import pytest

from silgeorae.errors import AmbiguousRegionError, ConfigError, RegionError, RegionNotFoundError
from silgeorae.regions import CSV_COLUMNS, Region, RegionTable

SUWON = ["41111", "41113", "41115", "41117"]


def codes(regions):
    return [r.lawd_cd for r in regions]


# ---------------------------------------------------------------------- 코드표
def test_bundled_table_shape(table):
    assert len(table) == 264
    assert len(table.all(leaves_only=True)) == 252
    parents = [r for r in table.all() if not r.is_leaf]
    assert len(parents) == 12
    for parent in parents:
        children = table.children(parent.lawd_cd)
        assert children and all(c.is_leaf and c.parent_cd == parent.lawd_cd for c in children)
        assert all(c.sigungu.startswith(parent.sigungu + " ") for c in children)
    assert len(table.sidos()) == 17
    assert "11680" in table and "99999" not in table


def test_region_names(table):
    gangnam = table.get("11680")
    assert (gangnam.name, gangnam.short_name) == ("서울특별시 강남구", "강남구")
    jangan = table.get("41111")
    assert (jangan.name, jangan.short_name) == ("경기도 수원시 장안구", "수원시 장안구")
    sejong = table.get("36110")
    assert (sejong.name, sejong.short_name) == ("세종특별자치시", "세종특별자치시")
    assert table.get(" 11680 ") == gangnam
    assert table.get("99999") is None
    assert table.get("51110").former_cd == "42110"


def test_all_sido_leaves_cover_every_leaf(table):
    leaves = [code for sido in table.sidos() for code in codes(table.resolve(sido))]
    assert sorted(leaves) == sorted(codes(table.all(leaves_only=True)))


# ---------------------------------------------------------------------- resolve
@pytest.mark.parametrize(
    "query, expected",
    [
        ("강남구", ["11680"]),
        ("서울 중구", ["11140"]),
        ("서울특별시 중구", ["11140"]),
        ("부산 중구", ["26110"]),
        ("수원시", SUWON),
        ("경기 수원시", SUWON),
        ("수원", SUWON),
        ("세종", ["36110"]),
        ("세종시", ["36110"]),
        ("11680", ["11680"]),
        ("41110", SUWON),
        ("1168010600", ["11680"]),
        ("분당구", ["41135"]),
        ("성남시 분당구", ["41135"]),
        ("성남시분당구", ["41135"]),
        ("경기도 성남시 분당구", ["41135"]),
        ("  강남구 ", ["11680"]),
        ("강남", ["11680"]),
        ("서울강남구", ["11680"]),
        ("강원 고성군", ["51820"]),
        ("경남 고성군", ["48820"]),
        ("강원도 고성", ["51820"]),
        ("부천시", ["41192", "41194", "41196"]),
        ("41190", ["41192", "41194", "41196"]),
        ("광주시", ["41610"]),  # 경기도 광주시 (광주광역시 아님)
        ("경기 광주", ["41610"]),
        ("광주 북구", ["29170"]),
        ("광주북구", ["29170"]),
        ("부산진구", ["26230"]),
        ("제주시", ["50110"]),
        ("전라북도 전주시", ["52111", "52113"]),
        ("강원도 춘천시", ["51110"]),
        ("대구 군위군", ["27720"]),
        ("수원 장안", ["41111"]),
        ("고양 일산동", ["41285"]),
        ("경북 포항", ["47111", "47113"]),
        ("포항시 남구", ["47111"]),
        ("경북 남구", ["47111"]),
    ],
)
def test_resolve(table, query, expected):
    regions = table.resolve(query)
    assert codes(regions) == expected
    assert all(r.is_leaf for r in regions)


@pytest.mark.parametrize(
    "query, count",
    [
        ("서울", 25), ("서울시", 25), ("서울특별시", 25), ("부산", 16), ("대구", 9), ("인천", 10),
        ("광주", 5), ("대전", 5), ("울산", 5), ("세종", 1), ("경기", 44), ("경기도", 44),
        ("강원", 18), ("강원도", 18), ("강원특별자치도", 18), ("충북", 14), ("충청남도", 16),
        ("전북", 15), ("전라북도", 15), ("전남", 22), ("경북", 23), ("경상남도", 22),
        ("제주", 2), ("제주도", 2),
    ],
)
def test_resolve_sido(table, query, count):
    regions = table.resolve(query)
    assert len(regions) == count
    assert all(r.is_leaf for r in regions)
    assert len({r.sido for r in regions}) == 1


@pytest.mark.parametrize(
    "query, expected",
    [
        ("중구", {"11140", "26110", "27110", "28110", "30140", "31110"}),
        ("고성군", {"48820", "51820"}),
        ("고성", {"48820", "51820"}),
        ("강서구", {"11500", "26440"}),
        ("남구", {"26290", "27200", "29155", "31140", "47111"}),
    ],
)
def test_resolve_ambiguous(table, query, expected):
    with pytest.raises(AmbiguousRegionError) as info:
        table.resolve(query)
    assert {r.lawd_cd for r in info.value.candidates} == expected
    assert all(isinstance(r, Region) for r in info.value.candidates)
    assert "시도를 함께 입력하세요" in str(info.value)


def test_ambiguous_message_lists_candidates_and_example(table):
    with pytest.raises(AmbiguousRegionError) as info:
        table.resolve("중구")
    message = str(info.value)
    assert "'서울 중구'처럼" in message
    assert "서울특별시 중구(11140)" in message and "울산광역시 중구(31110)" in message


def test_unknown_code_gives_synthetic_region(table, caplog):
    with caplog.at_level(logging.WARNING, logger="silgeorae.regions"):
        regions = table.resolve("99999")
    assert regions == [Region(lawd_cd="99999", sido="", sigungu="99999", note="코드표에 없음")]
    assert regions[0].name == "99999" and regions[0].short_name == "99999"
    assert "99999" in caplog.text


def test_former_code_maps_to_current(table, caplog):
    with caplog.at_level(logging.WARNING, logger="silgeorae.regions"):
        assert codes(table.resolve("42110")) == ["51110"]  # 강원특별자치도 출범 전 춘천시
    assert "옛 코드" in caplog.text
    assert codes(table.resolve("45110")) == ["52111", "52113"]  # 전북 전주시 → 2개 구
    assert codes(table.resolve("4772000000")) == ["27720"]  # 군위군 대구 편입 전


@pytest.mark.parametrize("query", ["없는구", "서울 없는구", "1234", "123456", "", "   ", "구"])
def test_resolve_not_found(table, query):
    with pytest.raises(RegionNotFoundError):
        table.resolve(query)


def test_not_found_messages(table):
    with pytest.raises(RegionNotFoundError, match="찾을 수 없습니다") as info:
        table.resolve("없는구")
    assert "silgeorae regions" in str(info.value)
    with pytest.raises(RegionNotFoundError, match=r"혹시: 서울특별시 강남구\(11680\)"):
        table.resolve("강남구청")
    with pytest.raises(RegionNotFoundError, match="서울특별시에서") as info:
        table.resolve("서울 강남구청")
    assert "11680" in str(info.value)
    with pytest.raises(RegionNotFoundError) as info:
        table.resolve("마산")
    assert "48125" in str(info.value) and "48127" in str(info.value)


def test_region_errors_share_base_class(table):
    for query in ("중구", "없는구"):
        with pytest.raises(RegionError):
            table.resolve(query)


def test_resolve_many_dedupes_and_keeps_order(table):
    regions = table.resolve_many(["강남구", "11680", "서초구, 강남", "수원시", "41111"])
    assert codes(regions) == ["11680", "11650", *SUWON]
    assert codes(table.resolve_many("강남구,서초구")) == ["11680", "11650"]
    assert table.resolve_many([]) == []
    with pytest.raises(AmbiguousRegionError):
        table.resolve_many(["강남구", "중구"])


# ---------------------------------------------------------------------- search / name_of
def test_search(table):
    assert codes(table.search("수원")) == ["41110", *SUWON]  # 구를 둔 시 포함
    assert [r.sigungu for r in table.search("서울 강")] == ["강북구", "강서구", "강남구", "강동구"]
    assert codes(table.search("전라북도 전주")) == ["52110", "52111", "52113"]
    assert codes(table.search("11680")) == ["11680"]
    assert codes(table.search("1168010600")) == ["11680"]
    assert codes(table.search("42110")) == ["51110"]  # 옛 코드
    assert codes(table.search("성남시분당")) == ["41135"]
    assert table.search("") == [] and table.search("없는지역") == []


def test_name_of(table):
    assert table.name_of("11680") == "서울특별시 강남구"
    assert table.name_of("41111") == "경기도 수원시 장안구"
    assert table.name_of("36110") == "세종특별자치시"
    assert table.name_of("99999") == "99999"
    assert table.name_of("42110") == "강원특별자치도 춘천시"


# ---------------------------------------------------------------------- 입출력
def test_save_load_round_trip(table, tmp_path):
    path = tmp_path / "nested" / "regions.csv"
    table.save(path)
    loaded = RegionTable.load(path)
    assert loaded.all() == table.all()
    assert path.read_text(encoding="utf-8").splitlines()[0] == ",".join(CSV_COLUMNS)
    assert not path.with_name("regions.csv.tmp").exists()


def test_load_tolerates_bom_and_minimal_columns(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_text("lawd_cd,sido,sigungu\n11680,서울특별시,강남구\n\n", encoding="utf-8-sig")
    table = RegionTable.load(path)
    assert codes(table.all()) == ["11680"]
    region = table.get("11680")
    assert region.is_leaf and region.parent_cd == "" and region.name == "서울특별시 강남구"


def test_load_excel_cp949_csv(tmp_path):
    path = tmp_path / "excel.csv"
    path.write_bytes("lawd_cd,sido,sigungu\r\n11680,서울특별시,강남구\r\n".encode("cp949"))
    assert RegionTable.load(path).get("11680").name == "서울특별시 강남구"


def test_digit_typos_get_no_code_suggestions(table):
    with pytest.raises(RegionNotFoundError) as info:
        table.resolve("11-680")
    assert "혹시" not in str(info.value)


def test_load_errors(tmp_path):
    with pytest.raises(ConfigError, match="없습니다"):
        RegionTable.load(tmp_path / "missing.csv")
    bad = tmp_path / "bad.csv"
    bad.write_text("code,name\n11680,강남구\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="lawd_cd"):
        RegionTable.load(bad)
    bad.write_text("lawd_cd,sido,sigungu\n1168,서울특별시,강남구\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="5자리"):
        RegionTable.load(bad)
    bad.write_text("lawd_cd,sido,sigungu,is_leaf\n11680,서울특별시,강남구,maybe\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="is_leaf"):
        RegionTable.load(bad)


def test_custom_table_with_new_sido_and_childless_parent(caplog):
    table = RegionTable(
        [
            Region("99110", "새특별시", "가구"),
            Region("99120", "새특별시", "나시", is_leaf=False),
            Region("99121", "새특별시", "다시 라구", parent_cd="99120"),
            Region("99130", "새특별시", "마시", is_leaf=False),  # 하위 구가 없는 시 → 그대로 조회
        ]
    )
    assert codes(table.resolve("새특별시")) == ["99110", "99121", "99130"]
    assert codes(table.resolve("새특별시 나시")) == ["99121"]
    assert codes(table.resolve("라구")) == ["99121"]
    with caplog.at_level(logging.WARNING, logger="silgeorae.regions"):
        assert codes(table.resolve("마시")) == ["99130"]
    assert "하위 구" in caplog.text
