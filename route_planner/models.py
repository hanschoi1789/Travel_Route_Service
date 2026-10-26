"""입력/출력 데이터 구조."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import config


def hm(s: str) -> int:
    """'HH:MM' -> 자정 기준 분."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def fmt(minutes: int) -> str:
    """자정 기준 분 -> 'HH:MM'."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Location:
    name: str
    lat: float
    lng: float


@dataclass
class Place(Location):
    """방문할 장소.

    open_time / close_time 를 비워두면 카테고리 기본값을 사용한다.
    체류시간:
      stay_min            : 값을 주면 그 시간으로 고정
      min_stay / max_stay : 조정 가능한 범위 (비우면 config.DEFAULT_STAY_RANGE)
    fee: 입장료 등 장소 고정비용(원). 순서와 무관하지만 예산 계산에 포함.
    """
    category: str = "other"
    stay_min: Optional[int] = None
    min_stay: Optional[int] = None
    max_stay: Optional[int] = None
    open_time: Optional[str] = None
    close_time: Optional[str] = None
    fee: int = 0

    def __post_init__(self):
        if self.category not in config.CATEGORIES:
            raise ValueError(f"unknown category: {self.category}")

    @property
    def stay_range(self) -> tuple[int, int]:
        if self.stay_min is not None:
            return self.stay_min, self.stay_min
        lo, hi = config.DEFAULT_STAY_RANGE[self.category]
        lo = self.min_stay if self.min_stay is not None else lo
        hi = self.max_stay if self.max_stay is not None else hi
        return min(lo, hi), hi

    @property
    def stay(self) -> int:
        """선호 체류시간 (= 최대값). 줄일 필요가 없으면 이 값을 씀."""
        return self.stay_range[1]

    @property
    def stay_options(self) -> list[int]:
        """가능한 체류시간들, 긴 것부터 (예: 120, 105, 90, 75, 60)."""
        lo, hi = self.stay_range
        opts = list(range(hi, lo, -config.STAY_STEP))
        return opts + [lo] if not opts or opts[-1] != lo else opts

    @property
    def hours(self) -> tuple[int, int]:
        o, c = config.DEFAULT_OPEN_HOURS[self.category]
        return hm(self.open_time or o), hm(self.close_time or c)

    @property
    def hours_is_default(self) -> bool:
        return self.open_time is None and self.close_time is None


@dataclass
class TripRequest:
    """사용자 입력."""
    places: list[Place]
    origin: Location
    destination: Location
    start_time: str          # "HH:MM"
    end_time: str            # "HH:MM" (도착지에 이 시각까지 도착)
    budget: Optional[int] = None   # 원, None 이면 제한 없음
    preset: str = "balanced"       # config.PRESETS 의 key
    weights: Optional[dict] = None  # 직접 가중치 지정 시 preset 보다 우선
    modes: tuple[str, ...] = config.MODES_PUBLIC  # 또는 ("car",)
    max_walk_leg_km: float = config.MAX_WALK_LEG_KM
    meal_windows: dict = field(default_factory=lambda: dict(config.MEAL_WINDOWS))
    break_time: Optional[tuple[str, str]] = config.BREAK_TIME

    def resolved_weights(self) -> dict:
        return dict(self.weights) if self.weights else dict(config.PRESETS[self.preset])


@dataclass
class Leg:
    """한 구간을 특정 수단으로 이동할 때의 비용."""
    mode: str
    minutes: int
    won: int
    walk_km: float
    transfers: int = 0
    distance_km: float = 0.0
    source: str = "kakao"
    # 대중교통 세부 구간 (카카오 데이터일 때만 채워짐). 예:
    # [{"type": "SUBWAY", "lines": ["4호선"], "from": "서울역", "to": "혜화",
    #   "stops": 5, "minutes": 12, "path": [[lng, lat], ...]}, {"type": "WALKING", ...}]
    detail: list = field(default_factory=list)

    def weighted(self, w: dict) -> float:
        return (w["w_time"] * self.minutes + w["w_money"] * self.won / 1000
                + w["w_walk"] * self.walk_km + w["w_transfer"] * self.transfers)

    def non_time_cost(self, w: dict) -> float:
        return (w["w_money"] * self.won / 1000 + w["w_walk"] * self.walk_km
                + w["w_transfer"] * self.transfers)
