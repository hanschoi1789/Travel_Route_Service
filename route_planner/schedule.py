"""시간 제약(운영시간, 식사시간, 브레이크타임)과 일정 시뮬레이션.

각 장소는 '방문 시작 가능 시각'의 구간 리스트(domain)를 가진다.
  일반 장소 : [open, close - stay]
  식당      : 배정된 식사 구간(점심/저녁) 안에 체류 전체가 들어가고,
              브레이크타임과 체류가 겹치지 않으며, 운영시간 안이어야 함
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import config
from .models import Leg, Place, TripRequest, hm

Interval = tuple[int, int]  # [lo, hi] 닫힌 구간, 방문 시작 시각(분)


def _intersect(a: list[Interval], lo: int, hi: int) -> list[Interval]:
    out = []
    for x, y in a:
        x2, y2 = max(x, lo), min(y, hi)
        if x2 <= y2:
            out.append((x2, y2))
    return out


def _subtract(a: list[Interval], lo: int, hi: int) -> list[Interval]:
    """열린 구간 (lo, hi) 를 제거."""
    out = []
    for x, y in a:
        if y <= lo or x >= hi:
            out.append((x, y))
            continue
        if x <= lo:
            out.append((x, lo))
        if y >= hi:
            out.append((hi, y))
    return out


def meal_slots(req: TripRequest) -> list[str]:
    return list(req.meal_windows.keys())


def start_domain(p: Place, req: TripRequest, slot: Optional[str] = None,
                 stay: Optional[int] = None) -> list[Interval]:
    """장소 p 에 stay 분 머물 때 방문 시작 가능 시각 구간들. 식당이면 slot(lunch/dinner) 지정 필요."""
    stay = p.stay if stay is None else stay
    o, c = p.hours
    day_lo, day_hi = hm(req.start_time), hm(req.end_time)
    dom = _intersect([(o, c - stay)], day_lo, day_hi - stay)
    if p.category == "restaurant":
        if slot is None:
            raise ValueError("restaurant needs a meal slot")
        ms, me = (hm(t) for t in req.meal_windows[slot])
        dom = _intersect(dom, ms, me - stay)
        if req.break_time:
            bs, be = (hm(t) for t in req.break_time)
            # 체류 [s, s+stay] 가 (bs, be) 와 겹치지 않으려면 s <= bs - stay 또는 s >= be
            dom = _subtract(dom, bs - stay, be)
    return dom


def earliest_in(dom: list[Interval], t: int) -> Optional[int]:
    for lo, hi in dom:
        if t <= hi:
            return max(t, lo)
    return None


def latest_in(dom: list[Interval], t: int) -> Optional[int]:
    for lo, hi in reversed(dom):
        if t >= lo:
            return min(t, hi)
    return None


# ---------------------------------------------------------------------------
@dataclass
class Stop:
    place_idx: int          # req.places 의 인덱스
    slot: Optional[str]     # 식당이면 lunch/dinner
    leg_in: Leg             # 이 장소로 오는 구간
    arrive: int
    start: int
    depart: int

    @property
    def wait(self) -> int:
        return self.start - self.arrive

    @property
    def stay(self) -> int:
        return self.depart - self.start


@dataclass
class Schedule:
    stops: list[Stop]
    leg_out: Leg            # 마지막 장소 -> 도착지
    depart_origin: int
    arrive_dest: int
    total_won: int = 0
    total_walk_km: float = 0.0
    total_transfers: int = 0
    total_travel_min: int = 0
    total_wait_min: int = 0
    objective: float = 0.0
    latest_departure: Optional[int] = None   # 이 시각까지 늦게 출발해도 같은 시각에 끝남
    extra: dict = field(default_factory=dict)

    @property
    def order(self) -> list[int]:
        return [s.place_idx for s in self.stops]


def simulate(order: list[int], slots: dict[int, str], legs, req: TripRequest,
             w: dict, chosen: Optional[list[Leg]] = None,
             stays: Optional[dict[int, int]] = None) -> Optional[Schedule]:
    """방문 순서가 주어지면 가장 이른 일정으로 시뮬레이션. 불가능하면 None.

    order  : req.places 인덱스 리스트 (방문하지 않는 장소는 빠져 있어도 됨)
    slots  : 식당 place 인덱스 -> 'lunch' / 'dinner'
    legs   : choose_legs 결과 (노드 번호 = place 인덱스 + 1)
    chosen : 구간별 이동수단을 직접 지정할 때 (len(order)+1 개, 마지막은 도착지로 가는 구간)
             이때 legs 는 무시됨
    stays  : place 인덱스 -> 체류시간(분). 없으면 각 장소의 선호(최대) 체류시간
    """
    places = req.places
    N = len(places) + 2
    t = hm(req.start_time)
    prev = 0
    stops = []
    for k, pi in enumerate(order):
        node = pi + 1
        leg = chosen[k] if chosen is not None else legs[prev][node]
        if leg is None:
            return None
        arrive = t + leg.minutes
        p = places[pi]
        stay = stays.get(pi, p.stay) if stays else p.stay
        dom = start_domain(p, req, slots.get(pi), stay)
        s = earliest_in(dom, arrive)
        if s is None:
            return None
        stops.append(Stop(pi, slots.get(pi), leg, arrive, s, s + stay))
        t = s + stay
        prev = node
    leg_out = chosen[-1] if chosen is not None else legs[prev][N - 1]
    if leg_out is None:
        return None
    end = t + leg_out.minutes
    if end > hm(req.end_time):
        return None

    all_legs = [s.leg_in for s in stops] + [leg_out]
    fees = sum(places[s.place_idx].fee for s in stops)
    sch = Schedule(stops, leg_out, hm(req.start_time), end)
    sch.total_won = sum(l.won for l in all_legs) + fees
    sch.total_walk_km = round(sum(l.walk_km for l in all_legs), 2)
    sch.total_transfers = sum(l.transfers for l in all_legs)
    sch.total_travel_min = sum(l.minutes for l in all_legs)
    sch.total_wait_min = sum(s.wait for s in stops)
    sch.objective = objective(sch, req, w)
    if req.budget is not None and sch.total_won > req.budget:
        sch.extra["over_budget"] = True
    sch.latest_departure = _latest_departure(sch, req)
    return sch


def objective(sch: Schedule, req: TripRequest, w: dict) -> float:
    """목적함수 = w_time * (코스 전체 소요시간) + Σ 구간의 (돈, 도보, 환승) 가중합
              + 체류 단축 벌점(프리셋별) × (줄인 분의 합).

    코스 전체 소요시간 = 도착지 도착 - 출발 시각 = 이동 + 체류 + 대기.
    체류는 순서와 무관한 상수라서, 실제로는 '이동 + 대기'를 줄이는 효과.
    """
    legs = [s.leg_in for s in sch.stops] + [sch.leg_out]
    cut = sum(req.places[s.place_idx].stay - s.stay for s in sch.stops)
    return (w["w_time"] * (sch.arrive_dest - sch.depart_origin)
            + sum(l.non_time_cost(w) for l in legs)
            + config.stay_cut_weight(w) * cut)


def _latest_departure(sch: Schedule, req: TripRequest) -> int:
    """끝나는 시각을 바꾸지 않고 출발을 최대한 늦출 수 있는 시각 (역방향 계산)."""
    t = sch.arrive_dest - sch.leg_out.minutes      # 마지막 장소를 떠나야 하는 가장 늦은 시각
    for s in reversed(sch.stops):
        p = req.places[s.place_idx]
        dom = start_domain(p, req, s.slot, s.stay)
        latest = latest_in(dom, t - s.stay)
        t = latest - s.leg_in.minutes
    return t
