"""시군구(LAWD_CD) 지역코드표 — 지역명·코드를 실거래가 API 조회 단위로 해석한다.

* ``RegionTable.load()`` 는 번들 ``data/sigungu.csv`` (또는 지정한 CSV)를 읽는다.
* ``resolve("강남구")`` → 조회할 leaf 시군구 목록 (``[Region("11680", ...)]``).
  구를 둔 시(``수원시``)와 시도(``경기``)는 하위 leaf 로 펼친다.
* 코드표에 없는 5자리 코드도 그대로 조회할 수 있게 임시 ``Region`` 을 만든다
  (코드표 이후 신설된 구 등 — 최신화는 ``silgeorae regions --sync``).
"""

from __future__ import annotations

import csv
import io
import logging
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Iterable, Iterator

from .errors import AmbiguousRegionError, ConfigError, RegionNotFoundError

logger = logging.getLogger(__name__)

CSV_COLUMNS = ("lawd_cd", "sido", "sigungu", "parent_cd", "is_leaf", "former_cd", "note")
"""코드표 CSV 열 순서 (``data/README.md`` 참고)."""

UNKNOWN_NOTE = "코드표에 없음"
"""코드표에 없는 코드로 만든 임시 ``Region`` 의 ``note``."""

_SUFFIXES = ("구", "시", "군")
_SUGGEST_LIMIT = 5

# 표준 시도명 → 입력 별칭 (첫 번째가 안내 메시지에 쓰는 짧은 이름).
# '광주시'는 경기도 광주시이므로 광주광역시 별칭에 넣지 않는다.
_SIDO_ALIASES: dict[str, tuple[str, ...]] = {
    "서울특별시": ("서울", "서울시", "서울특별시"),
    "부산광역시": ("부산", "부산시", "부산광역시"),
    "대구광역시": ("대구", "대구시", "대구광역시"),
    "인천광역시": ("인천", "인천시", "인천광역시"),
    "광주광역시": ("광주", "광주광역시"),
    "대전광역시": ("대전", "대전시", "대전광역시"),
    "울산광역시": ("울산", "울산시", "울산광역시"),
    "세종특별자치시": ("세종", "세종시", "세종특별자치시"),
    "경기도": ("경기", "경기도"),
    "강원특별자치도": ("강원", "강원도", "강원특별자치도"),
    "충청북도": ("충북", "충청북도"),
    "충청남도": ("충남", "충청남도"),
    "전북특별자치도": ("전북", "전라북도", "전북특별자치도"),
    "전라남도": ("전남", "전라남도"),
    "경상북도": ("경북", "경상북도"),
    "경상남도": ("경남", "경상남도"),
    "제주특별자치도": ("제주", "제주도", "제주특별자치도"),
}
_ALIAS_TO_SIDO = {alias: sido for sido, aliases in _SIDO_ALIASES.items() for alias in aliases}


@dataclass(frozen=True)
class Region:
    """시군구 1곳 = 실거래가 API 의 ``LAWD_CD`` 조회 단위."""

    lawd_cd: str  # 법정동코드 앞 5자리 (예: "11680")
    sido: str  # 시도명 (예: "서울특별시")
    sigungu: str  # 시군구명 ("강남구", "수원시 장안구", 세종은 "")
    parent_cd: str = ""  # 구를 둔 시의 하위 구이면 상위 시 코드
    is_leaf: bool = True  # False 면 하위 구로 펼쳐서 조회해야 하는 시
    former_cd: str = ""  # 코드 변경 전 옛 코드
    note: str = ""  # 변경 사유 등

    @property
    def name(self) -> str:
        """전체 이름: ``"서울특별시 강남구"`` (세종: ``"세종특별자치시"``)."""
        return " ".join(part for part in (self.sido, self.sigungu) if part) or self.lawd_cd

    @property
    def short_name(self) -> str:
        """짧은 이름: ``"강남구"``, ``"수원시 장안구"`` (세종: ``"세종특별자치시"``)."""
        return self.sigungu or self.sido or self.lawd_cd


class RegionTable:
    """시군구 코드표 — 코드·이름 검색과 조회 단위(leaf) 해석."""

    def __init__(self, regions: Iterable[Region]) -> None:
        by_code: dict[str, Region] = {}
        for region in regions:
            if region.lawd_cd in by_code:
                logger.warning("지역코드 %s 가 코드표에 두 번 있습니다 — 뒤의 행을 씁니다.", region.lawd_cd)
            by_code[region.lawd_cd] = region
        self._by_code = by_code
        self._regions = list(by_code.values())
        self._children: dict[str, list[Region]] = {}
        self._by_former: dict[str, list[Region]] = {}
        self._compact_names = {r.lawd_cd: _compact(r.name) for r in self._regions}
        self._sido_index = dict(_ALIAS_TO_SIDO)  # 별칭(공백 제거) → 표준 시도명
        for region in self._regions:
            if region.parent_cd and region.parent_cd != region.lawd_cd:
                self._children.setdefault(region.parent_cd, []).append(region)
            if region.former_cd and region.former_cd != region.lawd_cd:
                self._by_former.setdefault(region.former_cd, []).append(region)
            if region.sido:
                self._sido_index.setdefault(_compact(region.sido), _canonical_sido(region.sido))
        # 붙여 쓴 입력("서울강남구")에서 시도를 떼어 낼 때 긴 별칭부터 본다.
        self._aliases_longest_first = sorted(self._sido_index, key=len, reverse=True)

    # ------------------------------------------------------------------ 입출력
    @classmethod
    def load(cls, path: str | Path | None = None) -> RegionTable:
        """코드표 CSV 를 읽는다. ``path`` 가 None 이면 패키지에 번들된 ``data/sigungu.csv``."""
        if path is None:
            resource = resources.files("silgeorae") / "data" / "sigungu.csv"
            return cls(_parse_csv(resource.read_text(encoding="utf-8-sig"), "번들 sigungu.csv"))
        try:
            data = Path(path).read_bytes()
        except FileNotFoundError:
            raise ConfigError(f"지역코드표 파일이 없습니다: {path}") from None
        except OSError as exc:
            raise ConfigError(f"지역코드표 파일을 읽을 수 없습니다: {path} ({exc})") from exc
        try:
            text = data.decode("utf-8-sig")  # BOM 이 있어도 된다
        except UnicodeDecodeError:
            try:  # 엑셀에서 'CSV (쉼표로 분리)'로 저장하면 CP949 가 된다
                text = data.decode("cp949")
            except UnicodeDecodeError as exc:
                raise ConfigError(f"지역코드표 파일의 인코딩을 알 수 없습니다 (UTF-8 로 저장하세요): {path}") from exc
            logger.info("지역코드표 %s 를 CP949 로 읽었습니다.", path)
        return cls(_parse_csv(text, str(path)))

    def save(self, path: str | Path) -> None:
        """``load`` 와 같은 형식(UTF-8 CSV)으로 저장한다. 임시 파일에 쓴 뒤 교체한다."""
        target = Path(path)
        buffer = io.StringIO()
        writer = csv.writer(buffer)  # excel 방언 (\r\n) — 번들 파일과 같은 형식
        writer.writerow(CSV_COLUMNS)
        for r in self._regions:
            writer.writerow(
                [r.lawd_cd, r.sido, r.sigungu, r.parent_cd, "1" if r.is_leaf else "0", r.former_cd, r.note]
            )
        tmp = target.with_name(target.name + ".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                fh.write(buffer.getvalue())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise ConfigError(f"지역코드표를 저장할 수 없습니다: {target} ({exc})") from exc

    # ------------------------------------------------------------------ 조회
    def __len__(self) -> int:
        return len(self._regions)

    def __iter__(self) -> Iterator[Region]:
        return iter(self._regions)

    def __contains__(self, lawd_cd: object) -> bool:
        return str(lawd_cd).strip() in self._by_code

    def __repr__(self) -> str:
        return f"RegionTable({len(self._regions)}개 지역)"

    def get(self, lawd_cd: str) -> Region | None:
        """코드표의 시군구 (없으면 None)."""
        return self._by_code.get(str(lawd_cd).strip())

    def all(self, *, leaves_only: bool = False) -> list[Region]:
        """코드표 순서대로 전체 목록. ``leaves_only`` 면 실제 조회 단위만."""
        if leaves_only:
            return [r for r in self._regions if r.is_leaf]
        return list(self._regions)

    def children(self, lawd_cd: str) -> list[Region]:
        """구를 둔 시의 바로 아래 구 목록."""
        return list(self._children.get(str(lawd_cd).strip(), []))

    def sidos(self) -> list[str]:
        """코드표에 있는 시도명 (코드표 순서)."""
        return list(dict.fromkeys(r.sido for r in self._regions if r.sido))

    def name_of(self, lawd_cd: str) -> str:
        """``"11680"`` → ``"서울특별시 강남구"``. 모르는 코드는 코드 그대로 돌려준다 (옛 코드는 현재 이름)."""
        code = str(lawd_cd).strip()
        region = self._by_code.get(code)
        if region is None:
            successors = self._by_former.get(code, [])
            if len(successors) == 1:
                region = successors[0]
        return region.name if region is not None else code

    def search(self, text: str) -> list[Region]:
        """이름·코드 부분 일치 검색 (구를 둔 시 포함). 공백으로 나눈 단어는 모두 맞아야 한다.

        ``"수원"`` → 수원시와 4개 구, ``"서울 강"`` → 강북·강서·강남·강동구, ``"116"`` → 코드에 116 이 든 곳.
        """
        tokens = str(text).split()
        if not tokens:
            return []
        return [r for r in self._regions if all(self._hit(r, token) for token in tokens)]

    # ------------------------------------------------------------------ 해석
    def resolve(self, query: str) -> list[Region]:
        """지역 입력을 조회 가능한 leaf 시군구 목록으로 바꾼다.

        * 5자리 코드 → 그 시군구 (구를 둔 시면 하위 구), 10자리 법정동코드 → 앞 5자리.
          코드표에 없는 코드는 경고 후 임시 ``Region`` 으로 그대로 쓴다. 옛 코드는 현재 코드로 바꾼다.
        * ``"경기"`` 등 시도만 → 그 시도의 모든 leaf.
        * ``"서울 강남구"``, ``"경기도 성남시 분당구"`` → 시도 안에서 찾는다.
        * ``"강남구"``, ``"분당구"``, ``"수원시"``(→4개 구), ``"강남"``(→강남구) — 공백은 무시한다.

        여러 곳에 해당하면 ``AmbiguousRegionError``, 없으면 ``RegionNotFoundError``.
        """
        text = " ".join(str(query).split())
        if not text:
            raise RegionNotFoundError("지역을 입력하세요 (예: '강남구', '서울 중구', '11680').")
        compact = text.replace(" ", "")
        if compact.isdigit():
            return self._resolve_code(compact, text)
        return self._resolve_name(text)

    def resolve_many(self, queries: Iterable[str]) -> list[Region]:
        """여러 입력을 해석해 중복 없이 입력 순서대로 합친다. 쉼표로 구분한 입력도 받는다."""
        if isinstance(queries, str):
            queries = [queries]
        result: list[Region] = []
        seen: set[str] = set()
        for query in queries:
            for part in str(query).split(","):
                if not part.strip():
                    continue
                for region in self.resolve(part):
                    if region.lawd_cd not in seen:
                        seen.add(region.lawd_cd)
                        result.append(region)
        return result

    # ------------------------------------------------------------------ 내부
    def _resolve_code(self, digits: str, text: str) -> list[Region]:
        if len(digits) == 10:  # 법정동코드 → 시군구 코드
            code = digits[:5]
        elif len(digits) == 5:
            code = digits
        else:
            raise RegionNotFoundError(
                f"지역코드 '{text}' 는 5자리(시군구) 또는 10자리(법정동) 숫자여야 합니다 (예: 11680)."
            )
        region = self._by_code.get(code)
        if region is not None:
            return self._expand(region)
        successors = self._by_former.get(code)
        if successors:
            leaves = _unique(leaf for s in successors for leaf in self._expand(s))
            logger.warning(
                "지역코드 %s 는 바뀌기 전 옛 코드입니다 → %s 로 조회합니다.",
                code,
                ", ".join(_label(r) for r in leaves),
            )
            return leaves
        logger.warning(
            "지역코드 %s 가 코드표에 없습니다. 입력한 코드 그대로 조회합니다 "
            "(코드표 최신화: silgeorae regions --sync).",
            code,
        )
        return [Region(lawd_cd=code, sido="", sigungu=code, note=UNKNOWN_NOTE)]

    def _resolve_name(self, text: str) -> list[Region]:
        tokens = text.split()
        compact = "".join(tokens)
        sido = self._sido_index.get(compact)
        if sido is not None:  # 시도만 입력 → 시도 전체
            leaves = self._sido_leaves(sido)
            if not leaves:
                raise RegionNotFoundError(f"코드표에 '{sido}' 의 시군구가 없습니다.")
            return leaves
        if len(tokens) >= 2:
            sido = self._sido_index.get(tokens[0])
            if sido is not None:  # "서울 중구", "경기도 성남시 분당구"
                scope = self._in_sido(sido)
                return self._pick(text, self._match(scope, tokens[1:]), scope, " ".join(tokens[1:]), sido)
        matches = self._match(self._regions, tokens)
        if not matches:  # "서울강남구" 처럼 시도를 붙여 쓴 경우
            for alias in self._aliases_longest_first:
                if len(compact) > len(alias) and compact.startswith(alias):
                    found = self._match(self._in_sido(self._sido_index[alias]), [compact[len(alias):]])
                    if found:
                        matches = found
                        break
        return self._pick(text, matches, self._regions, text, None)

    @staticmethod
    def _match(scope: Iterable[Region], tokens: list[str]) -> list[Region]:
        """시군구명 일치: 정확히(공백 무시) 또는 마지막 단어("분당구") → 없으면 접미사(구·시·군) 생략 허용."""
        rest = "".join(tokens)
        loose_key = "".join(_strip_suffix(t) for t in tokens)
        exact: list[Region] = []
        loose: list[Region] = []
        for region in scope:
            parts = region.sigungu.split()
            if not parts:
                continue
            if rest == "".join(parts) or rest == parts[-1]:
                exact.append(region)
            elif loose_key in ("".join(_strip_suffix(p) for p in parts), _strip_suffix(parts[-1])):
                loose.append(region)
        return exact or loose

    def _pick(
        self, query: str, matches: list[Region], scope: list[Region], rest: str, sido: str | None
    ) -> list[Region]:
        codes = {r.lawd_cd for r in matches}
        matches = [r for r in matches if r.parent_cd not in codes]  # 시와 그 구가 함께 걸리면 시만
        if not matches:
            raise self._not_found(query, scope, rest, sido)
        if len(matches) > 1:
            raise self._ambiguous(query, matches)
        return self._expand(matches[0])

    def _expand(self, region: Region, _depth: int = 0) -> list[Region]:
        """leaf 면 그대로, 구를 둔 시면 하위 leaf 들."""
        if region.is_leaf:
            return [region]
        children = self._children.get(region.lawd_cd, [])
        if not children or _depth > 5:
            logger.warning("%s 의 하위 구가 코드표에 없어 %s 로 조회합니다.", region.name, region.lawd_cd)
            return [region]
        return _unique(leaf for child in children for leaf in self._expand(child, _depth + 1))

    def _in_sido(self, sido: str) -> list[Region]:
        return [r for r in self._regions if r.sido and _canonical_sido(r.sido) == sido]

    def _sido_leaves(self, sido: str) -> list[Region]:
        tops = [r for r in self._in_sido(sido) if not r.parent_cd or r.parent_cd not in self._by_code]
        return _unique(leaf for top in tops for leaf in self._expand(top))

    def _hit(self, region: Region, token: str) -> bool:
        if token.isdigit():
            code = token[:5] if len(token) == 10 else token
            return code in region.lawd_cd or (bool(region.former_cd) and code in region.former_cd)
        sido = self._sido_index.get(token)
        if sido is not None and region.sido and _canonical_sido(region.sido) == sido:
            return True
        return token in self._compact_names.get(region.lawd_cd, "")

    def _suggest(self, text: str, scope: list[Region]) -> list[Region]:
        """비슷한 지역 (검색어를 뒤에서부터 줄여 가며 search)."""
        allowed = {r.lawd_cd for r in scope}
        compact = _compact(text)
        for end in range(len(compact), 1, -1):
            if compact[:end].isdigit():  # 숫자 조각은 코드 부분 일치라 엉뚱한 곳이 걸린다
                continue
            hits = [r for r in self.search(compact[:end]) if r.lawd_cd in allowed]
            if hits:
                return hits[:_SUGGEST_LIMIT]
        return []

    def _not_found(self, query: str, scope: list[Region], rest: str, sido: str | None) -> RegionNotFoundError:
        where = f"{sido}에서 '{rest}'" if sido else f"'{query}'"
        message = f"{where}에 해당하는 시군구를 찾을 수 없습니다."
        suggestions = self._suggest(rest, scope)
        if suggestions:
            message += " 혹시: " + ", ".join(_label(r) for r in suggestions) + "?"
        else:
            message += " 'silgeorae regions' 로 지역 목록을 확인하거나 5자리 지역코드(예: 11680)를 입력하세요."
        return RegionNotFoundError(message)

    def _ambiguous(self, query: str, matches: list[Region]) -> AmbiguousRegionError:
        first = matches[0]
        if len({_canonical_sido(r.sido) for r in matches}) == len(matches):
            hint = f"'{_short_sido(first.sido)} {first.sigungu}'처럼 시도를 함께 입력하세요"
        else:
            hint = f"'{first.name}'처럼 전체 이름을 입력하세요"
        listing = ", ".join(_label(r) for r in matches)
        return AmbiguousRegionError(
            f"'{query}'에 해당하는 지역이 {len(matches)}곳입니다: {listing}. {hint} (또는 5자리 지역코드).",
            candidates=matches,
        )


# ---------------------------------------------------------------------- 도우미
def _parse_csv(text: str, source: str) -> list[Region]:
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [name.strip() for name in (reader.fieldnames or [])]
    missing = [c for c in ("lawd_cd", "sido", "sigungu") if c not in reader.fieldnames]
    if missing:
        raise ConfigError(f"{source}: 지역코드표에 필요한 열이 없습니다: {', '.join(missing)}")
    regions = []
    for row in reader:
        values = {k: (v or "").strip() for k, v in row.items() if isinstance(k, str)}
        if not any(values.values()):
            continue
        code = values.get("lawd_cd", "")
        if len(code) != 5 or not code.isdigit():
            raise ConfigError(f"{source} {reader.line_num}행: 지역코드는 5자리 숫자여야 합니다 ({code!r}).")
        regions.append(
            Region(
                lawd_cd=code,
                sido=values.get("sido", ""),
                sigungu=" ".join(values.get("sigungu", "").split()),
                parent_cd=values.get("parent_cd", ""),
                is_leaf=_parse_bool(values.get("is_leaf", ""), source, reader.line_num),
                former_cd=values.get("former_cd", ""),
                note=values.get("note", ""),
            )
        )
    return regions


def _parse_bool(value: str, source: str, line: int) -> bool:
    text = value.strip().lower()
    if text in ("", "1", "true", "y", "yes"):
        return True
    if text in ("0", "false", "n", "no"):
        return False
    raise ConfigError(f"{source} {line}행: is_leaf 는 1 또는 0 이어야 합니다 ({value!r}).")


def _compact(text: str) -> str:
    return "".join(str(text).split())


def _strip_suffix(token: str) -> str:
    """``"강남구"`` → ``"강남"`` (접미사만 남는 한 글자는 그대로)."""
    if len(token) > 1 and token.endswith(_SUFFIXES):
        return token[:-1]
    return token


def _canonical_sido(sido: str) -> str:
    return _ALIAS_TO_SIDO.get(_compact(sido), sido)


def _short_sido(sido: str) -> str:
    aliases = _SIDO_ALIASES.get(_canonical_sido(sido))
    return aliases[0] if aliases else sido


def _label(region: Region) -> str:
    return f"{region.name}({region.lawd_cd})"


def _unique(regions: Iterable[Region]) -> list[Region]:
    seen: set[str] = set()
    result = []
    for region in regions:
        if region.lawd_cd not in seen:
            seen.add(region.lawd_cd)
            result.append(region)
    return result
