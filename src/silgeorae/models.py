"""공통 데이터 모델 — 팀 간 계약(contract).

모든 팀은 이 모듈의 타입으로 데이터를 주고받는다.

* 수집팀(collector)   : ``DealType`` 별 API 원본 레코드(dict) 목록을 돌려준다.
* 정제·저장팀(processing): 원본 dict → ``Transaction`` 변환, SQLite 저장.
* 분석·리포트팀(analysis) : ``Transaction`` 목록 → 요약표·리포트 파일.
* CLI·자동화팀(app)   : 위 단계를 엮어 명령어·자동 실행을 제공한다.

필드를 추가·변경할 때는 ``docs/ARCHITECTURE.md`` 도 함께 고친다.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from typing import Any, Iterable, Optional

from .utils import PYEONG_M2, format_manwon, ym_of


class PropertyType(str, enum.Enum):
    """부동산 종류."""

    APT = "apt"
    OFFICETEL = "offi"
    ROWHOUSE = "rh"
    DETACHED = "sh"
    PRESALE = "presale"
    LAND = "land"
    COMMERCIAL = "commercial"
    INDUSTRIAL = "industrial"

    def __str__(self) -> str:
        return self.value

    @property
    def label(self) -> str:
        return _PROPERTY_LABELS[self]


_PROPERTY_LABELS = {
    PropertyType.APT: "아파트",
    PropertyType.OFFICETEL: "오피스텔",
    PropertyType.ROWHOUSE: "연립다세대",
    PropertyType.DETACHED: "단독/다가구",
    PropertyType.PRESALE: "분양·입주권",
    PropertyType.LAND: "토지",
    PropertyType.COMMERCIAL: "상업업무용",
    PropertyType.INDUSTRIAL: "공장·창고",
}


class TradeType(str, enum.Enum):
    """거래 종류."""

    SALE = "sale"
    RENT = "rent"

    def __str__(self) -> str:
        return self.value

    @property
    def label(self) -> str:
        return "매매" if self is TradeType.SALE else "전월세"


class DealType(str, enum.Enum):
    """수집 단위 = 국토교통부 실거래가 API 1종.

    값(value)은 CLI·설정 파일·DB 에 그대로 쓰이는 안정적인 코드다.
    """

    APT_SALE = "apt_sale"
    APT_RENT = "apt_rent"
    OFFI_SALE = "offi_sale"
    OFFI_RENT = "offi_rent"
    RH_SALE = "rh_sale"
    RH_RENT = "rh_rent"
    SH_SALE = "sh_sale"
    SH_RENT = "sh_rent"
    PRESALE = "presale"
    LAND = "land"
    COMMERCIAL = "commercial"
    INDUSTRIAL = "industrial"

    def __str__(self) -> str:
        return self.value

    @property
    def property_type(self) -> PropertyType:
        return _DEAL_META[self][0]

    @property
    def trade_type(self) -> TradeType:
        return _DEAL_META[self][1]

    @property
    def api_service(self) -> str:
        """공공데이터포털 서비스명 (예: ``RTMSDataSvcAptTradeDev``).

        요청 URL 은 ``https://apis.data.go.kr/1613000/{service}/get{service}`` 이다.
        """
        return _DEAL_META[self][2]

    @property
    def api_title(self) -> str:
        """공공데이터포털에서 활용신청할 때 검색할 API 이름."""
        return _DEAL_META[self][3]

    @property
    def label(self) -> str:
        """사람이 읽는 이름 (예: ``"아파트 매매"``)."""
        if self is DealType.PRESALE:
            return "분양·입주권 전매"
        return f"{self.property_type.label} {self.trade_type.label}"

    @property
    def is_rent(self) -> bool:
        return self.trade_type is TradeType.RENT

    @property
    def is_sale(self) -> bool:
        return self.trade_type is TradeType.SALE

    @classmethod
    def parse(cls, text: str) -> "DealType":
        """코드(``apt_sale``)·한글(``아파트매매``, ``아파트 전월세``) 등을 해석한다.

        종류만 적으면(``아파트``, ``apt``) 매매로 본다.
        """
        key = "".join(str(text).split()).lower().replace("-", "_")
        for member in cls:
            if key == member.value:
                return member
        if key in _DEAL_ALIASES:
            return _DEAL_ALIASES[key]
        choices = ", ".join(m.value for m in cls)
        raise ValueError(f"알 수 없는 거래 유형: {text!r} (가능한 값: {choices})")

    @classmethod
    def parse_many(cls, values: Iterable[str] | str) -> list["DealType"]:
        """쉼표로 구분된 문자열 또는 목록을 해석한다. ``all`` 은 전체, ``housing`` 은 주거용 8종."""
        if isinstance(values, str):
            values = [values]
        result: list[DealType] = []
        for value in values:
            for token in str(value).split(","):
                token = token.strip()
                if not token:
                    continue
                if token.lower() in ("all", "전체"):
                    picked = list(cls)
                elif token.lower() in ("housing", "주거", "주택"):
                    picked = [m for m in cls if m.property_type in _HOUSING]
                else:
                    picked = [cls.parse(token)]
                for member in picked:
                    if member not in result:
                        result.append(member)
        return result


# (종류, 거래, 서비스명, 활용신청 API 이름)
_DEAL_META = {
    DealType.APT_SALE: (PropertyType.APT, TradeType.SALE, "RTMSDataSvcAptTradeDev", "국토교통부_아파트 매매 실거래가 상세 자료"),
    DealType.APT_RENT: (PropertyType.APT, TradeType.RENT, "RTMSDataSvcAptRent", "국토교통부_아파트 전월세 실거래가 자료"),
    DealType.OFFI_SALE: (PropertyType.OFFICETEL, TradeType.SALE, "RTMSDataSvcOffiTrade", "국토교통부_오피스텔 매매 실거래가 자료"),
    DealType.OFFI_RENT: (PropertyType.OFFICETEL, TradeType.RENT, "RTMSDataSvcOffiRent", "국토교통부_오피스텔 전월세 실거래가 자료"),
    DealType.RH_SALE: (PropertyType.ROWHOUSE, TradeType.SALE, "RTMSDataSvcRHTrade", "국토교통부_연립다세대 매매 실거래가 자료"),
    DealType.RH_RENT: (PropertyType.ROWHOUSE, TradeType.RENT, "RTMSDataSvcRHRent", "국토교통부_연립다세대 전월세 실거래가 자료"),
    DealType.SH_SALE: (PropertyType.DETACHED, TradeType.SALE, "RTMSDataSvcSHTrade", "국토교통부_단독/다가구 매매 실거래가 자료"),
    DealType.SH_RENT: (PropertyType.DETACHED, TradeType.RENT, "RTMSDataSvcSHRent", "국토교통부_단독/다가구 전월세 실거래가 자료"),
    DealType.PRESALE: (PropertyType.PRESALE, TradeType.SALE, "RTMSDataSvcSilvTrade", "국토교통부_아파트 분양권전매 실거래가 자료"),
    DealType.LAND: (PropertyType.LAND, TradeType.SALE, "RTMSDataSvcLandTrade", "국토교통부_토지 매매 실거래가 자료"),
    DealType.COMMERCIAL: (PropertyType.COMMERCIAL, TradeType.SALE, "RTMSDataSvcNrgTrade", "국토교통부_상업업무용 부동산 매매 실거래가 자료"),
    DealType.INDUSTRIAL: (PropertyType.INDUSTRIAL, TradeType.SALE, "RTMSDataSvcInduTrade", "국토교통부_공장 및 창고 등 부동산 매매 실거래가 자료"),
}

_HOUSING = (PropertyType.APT, PropertyType.OFFICETEL, PropertyType.ROWHOUSE, PropertyType.DETACHED)


def _build_aliases() -> dict[str, DealType]:
    short = {
        PropertyType.APT: ("apt", "아파트"),
        PropertyType.OFFICETEL: ("offi", "officetel", "오피스텔"),
        PropertyType.ROWHOUSE: ("rh", "villa", "연립다세대", "연립", "다세대", "빌라"),
        PropertyType.DETACHED: ("sh", "house", "단독다가구", "단독/다가구", "단독", "다가구"),
    }
    aliases: dict[str, DealType] = {}
    for member in DealType:
        prop = member.property_type
        for name in short.get(prop, ()):
            if member.is_sale:
                aliases[name] = member
                aliases[f"{name}매매"] = member
                aliases[f"{name}_sale"] = member
                aliases[f"{name}_trade"] = member
            else:
                for suffix in ("전월세", "전세", "월세", "_rent", "rent"):
                    aliases[f"{name}{suffix}"] = member
    aliases.update(
        {
            "분양권": DealType.PRESALE,
            "입주권": DealType.PRESALE,
            "분양입주권": DealType.PRESALE,
            "분양·입주권": DealType.PRESALE,
            "silv": DealType.PRESALE,
            "토지": DealType.LAND,
            "토지매매": DealType.LAND,
            "상업업무용": DealType.COMMERCIAL,
            "상업": DealType.COMMERCIAL,
            "nrg": DealType.COMMERCIAL,
            "공장창고": DealType.INDUSTRIAL,
            "공장·창고": DealType.INDUSTRIAL,
            "공장": DealType.INDUSTRIAL,
            "indu": DealType.INDUSTRIAL,
        }
    )
    return aliases


_DEAL_ALIASES = _build_aliases()


def _num_text(value: Optional[float]) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


@dataclass
class Transaction:
    """표준화된 실거래 1건.

    * 금액은 모두 **만원** 단위 정수 (API 원본 단위 그대로).
    * 문자열 필드의 "없음"은 빈 문자열 ``""``, 숫자·날짜의 "없음"은 ``None``.
    * ``lawd_cd`` 는 **조회에 사용한** 시군구 코드 5자리다 (저장소의 파티션 키).
    """

    deal_type: DealType
    lawd_cd: str
    deal_date: date
    dong: str = ""  # 법정동 (umdNm)
    jibun: str = ""  # 지번 (단독·토지 등은 일부 마스킹: "1**")
    name: str = ""  # 단지·건물명 (aptNm / offiNm / mhouseNm), 없으면 ""
    sigungu: str = ""  # 시군구명 (sggNm 또는 지역코드표)
    floor: Optional[int] = None  # 층 (지하는 음수)
    area_m2: Optional[float] = None  # 전용면적 / 연면적(단독) / 거래면적(토지) / 건물면적(상업·공장)
    land_area_m2: Optional[float] = None  # 대지권면적(연립) / 대지면적(단독·상업·공장)
    build_year: Optional[int] = None  # 건축년도
    price: Optional[int] = None  # 거래금액(만원) — 매매
    deposit: Optional[int] = None  # 보증금(만원) — 전월세
    monthly_rent: Optional[int] = None  # 월세(만원) — 전월세 (0 이면 전세)
    is_cancelled: bool = False  # 해제여부 (cdealType == "O")
    cancel_date: Optional[date] = None  # 해제사유발생일
    deal_method: str = ""  # 거래유형: 중개거래 / 직거래
    agent_location: str = ""  # 중개사소재지
    registration_date: Optional[date] = None  # 등기일자
    seller: str = ""  # 매도자 구분 (개인/법인/공공기관/기타)
    buyer: str = ""  # 매수자 구분
    building_dong: str = ""  # 아파트 동 (aptDong)
    house_type: str = ""  # 주택유형 (단독/다가구, 연립/다세대)
    contract_type: str = ""  # 계약구분 (신규/갱신)
    contract_term: str = ""  # 계약기간 (예: "25.03~27.03")
    renewal_right_used: Optional[bool] = None  # 갱신요구권 사용 여부
    prev_deposit: Optional[int] = None  # 종전계약 보증금(만원)
    prev_monthly_rent: Optional[int] = None  # 종전계약 월세(만원)
    complex_id: str = ""  # 단지일련번호 (aptSeq, 아파트 매매 상세에만 존재)
    road_address: str = ""  # 도로명주소 (예: "삼성로 51")
    extra: dict[str, str] = field(default_factory=dict)  # 표준 필드 외 원본 필드 (지목, 용도지역 등)
    seq: int = 0  # 같은 배치에서 natural_key 가 같은 레코드의 순번 (assign_seq 참고)
    first_seen_at: Optional[datetime] = None  # 저장소에 처음 들어온 시각 (저장소가 채움)
    updated_at: Optional[datetime] = None  # 저장소에서 마지막으로 바뀐 시각 (저장소가 채움)

    # ------------------------------------------------------------------ 식별
    def natural_key(self) -> tuple[str, ...]:
        """거래를 식별하는 원본 값들.

        시간이 지나며 바뀔 수 있는 값(해제여부·해제일·등기일자·중개사 등)은 넣지 않는다.
        그래야 같은 거래가 나중에 '해제'되거나 '등기'되어도 같은 키로 갱신된다.
        """
        return (
            self.deal_type.value,
            self.lawd_cd,
            self.deal_date.isoformat(),
            self.dong,
            self.jibun,
            self.name,
            self.building_dong,
            _num_text(self.floor),
            _num_text(self.area_m2),
            _num_text(self.land_area_m2),
            _num_text(self.price),
            _num_text(self.deposit),
            _num_text(self.monthly_rent),
            self.house_type,
        )

    @property
    def key(self) -> str:
        """저장소 기본키 (natural_key + seq 의 SHA-1 앞 24자리)."""
        raw = "|".join(self.natural_key() + (str(self.seq),))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

    @property
    def complex_key(self) -> str:
        """단지(건물) 묶음 키: ``시군구|법정동|지번|이름``.

        매매·전월세 API 모두에 있는 값만 써서, 같은 단지의 매매와 전세를 짝지을 수 있다.
        """
        return "|".join((self.lawd_cd, self.dong, self.jibun, self.name))

    # ------------------------------------------------------------------ 파생 값
    @property
    def deal_ym(self) -> str:
        """계약 연월 ``"YYYYMM"``."""
        return ym_of(self.deal_date)

    @property
    def is_rent(self) -> bool:
        return self.deal_type.is_rent

    @property
    def rent_kind(self) -> str:
        """전월세 구분: ``"전세"`` / ``"월세"`` (매매는 ``""``)."""
        if not self.is_rent:
            return ""
        return "월세" if self.monthly_rent else "전세"

    @property
    def pyeong(self) -> Optional[float]:
        """면적(평), 소수 둘째 자리 반올림."""
        if not self.area_m2:
            return None
        return round(self.area_m2 / PYEONG_M2, 2)

    @property
    def area_type(self) -> Optional[int]:
        """평형 묶음용 정수 면적 (84.97㎡ → 84). 같은 단지 같은 타입 비교에 쓴다."""
        if not self.area_m2:
            return None
        return int(self.area_m2)

    @property
    def price_per_pyeong(self) -> Optional[float]:
        """평당 거래가(만원/평). 매매가 아니거나 값이 없으면 None."""
        if self.price is None or not self.area_m2:
            return None
        return self.price / (self.area_m2 / PYEONG_M2)

    @property
    def price_text(self) -> str:
        """표시용 가격: 매매 ``"8억 2,500만원"``, 전세 ``"전세 5억원"``, 월세 ``"월세 1억원/150만원"``."""
        if self.is_rent:
            if self.monthly_rent:
                return f"월세 {format_manwon(self.deposit)}/{format_manwon(self.monthly_rent)}"
            return f"전세 {format_manwon(self.deposit)}"
        return format_manwon(self.price)

    # ------------------------------------------------------------------ 직렬화
    def to_dict(self) -> dict[str, Any]:
        """JSON 으로 저장 가능한 dict (날짜는 ISO 문자열, 유형은 코드값)."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, enum.Enum):
                value = value.value
            elif isinstance(value, (date, datetime)):
                value = value.isoformat()
            elif isinstance(value, dict):
                value = dict(value)
            out[f.name] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transaction":
        """``to_dict`` 의 역변환."""
        kwargs = dict(data)
        kwargs["deal_type"] = DealType(kwargs["deal_type"])
        for name in ("deal_date", "cancel_date", "registration_date"):
            if kwargs.get(name):
                kwargs[name] = date.fromisoformat(kwargs[name])
        for name in ("first_seen_at", "updated_at"):
            if kwargs.get(name):
                kwargs[name] = datetime.fromisoformat(kwargs[name])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in kwargs.items() if k in known})


def assign_seq(transactions: Iterable[Transaction]) -> list[Transaction]:
    """한 번에 받아온 레코드들 안에서 natural_key 가 같은 것끼리 등장 순서대로 seq=0,1,2… 를 매긴다.

    같은 날 같은 단지·층·면적·가격의 거래가 두 건 있어도 서로 다른 키를 갖게 된다.
    같은 (유형, 시군구, 연월)을 다시 받아와도 같은 결과가 나오므로 갱신이 안정적이다.
    """
    counts: dict[tuple[str, ...], int] = {}
    result = []
    for tx in transactions:
        nk = tx.natural_key()
        tx.seq = counts.get(nk, 0)
        counts[nk] = tx.seq + 1
        result.append(tx)
    return result
