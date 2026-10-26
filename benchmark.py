"""검증용 벤치마크.

무작위 서울 시나리오를 여러 개 만들어서
  1) 모든 결과가 제약(운영시간, 식사시간, 브레이크타임, 종료시각, 예산)을 지키는지 독립적으로 검사
  2) 완전탐색(정답) 대비 입력순서 / Nearest Neighbor / OR-Tools 의 목적함수 차이(최적성 격차)
를 출력한다.

python benchmark.py [시나리오 수]
"""
import random
import statistics
import sys
import time
from dataclasses import replace

from route_planner import Location, Place, TripRequest, build_candidates, config, plan
from route_planner.models import Leg
from route_planner.providers import haversine_km
from route_planner.matrix import choose_legs
from route_planner.models import hm
from route_planner.solvers import (solve_exact, solve_exact_fixed_modes, solve_given_order,
                                   solve_nearest_neighbor, solve_ortools)

CATS = ["attraction", "attraction", "cafe", "other"]


def random_request(rng: random.Random, n: int) -> TripRequest:
    # 서울 도심 대략 범위
    def pt():
        return 37.53 + rng.random() * 0.06, 126.95 + rng.random() * 0.07

    n_rest = rng.choice([1, 2, 2])
    places = []
    for i in range(n):
        lat, lng = pt()
        cat = "restaurant" if i < n_rest else rng.choice(CATS)
        stay = rng.choice([60, 90, 120]) if cat != "restaurant" else None
        places.append(Place(f"P{i}({cat[:4]})", lat, lng, cat, stay_min=stay))
    rng.shuffle(places)
    lat, lng = pt()
    lat2, lng2 = pt()
    return TripRequest(places, Location("출발", lat, lng), Location("도착", lat2, lng2),
                       start_time="09:00", end_time="22:00",
                       budget=rng.choice([None, 25000, 40000]),
                       preset=rng.choice(list(config.PRESETS)))


def check(res) -> list[str]:
    """결과가 모든 제약을 지키는지 schedule 계산과 별개로 다시 검사."""
    req, sch = res.req, res.schedule
    errs = []
    lo_day, hi_day = hm(req.start_time), hm(req.end_time)
    bs, be = (hm(t) for t in req.break_time)
    t = lo_day
    used_slots = set()
    for s in sch.stops:
        p = req.places[s.place_idx]
        o, c = p.hours
        if s.arrive != t + s.leg_in.minutes:
            errs.append(f"{p.name}: 도착시각 계산 오류")
        if s.start < s.arrive:
            errs.append(f"{p.name}: 도착 전에 시작")
        lo, hi = p.stay_range
        if not (lo <= s.depart - s.start <= hi):
            errs.append(f"{p.name}: 체류시간 범위 밖 {s.depart - s.start}분 (허용 {lo}~{hi})")
        if s.start < o or s.depart > c:
            errs.append(f"{p.name}: 운영시간 위반 {s.start}-{s.depart} vs {o}-{c}")
        if p.category == "restaurant":
            ms, me = (hm(x) for x in req.meal_windows[s.slot])
            if s.start < ms or s.depart > me:
                errs.append(f"{p.name}: 식사시간 위반")
            if s.start < be and s.depart > bs:
                errs.append(f"{p.name}: 브레이크타임 위반")
            if s.slot in used_slots:
                errs.append(f"{p.name}: 같은 식사시간에 식당 2곳")
            used_slots.add(s.slot)
        t = s.depart
    if sch.arrive_dest > hi_day:
        errs.append("종료시각 초과")
    if req.budget is not None and sch.total_won > req.budget:
        errs.append(f"예산 초과 {sch.total_won} > {req.budget}")
    return errs


class EstimateProvider:
    """솔버 검증 전용 직선거리 스텁. 무작위 시나리오 수십 개를 카카오로 돌리면 일일 한도를 넘기므로
    benchmark.py 안에서만 쓴다. 서비스 코드(route_planner 패키지)에는 추정 모드가 없다."""

    def __init__(self):
        self.p = dict(config.EST)

    def legs(self, a, b, mode):
        import math
        p = self.p
        d = haversine_km(a, b) * p["detour"]
        if mode == "walk":
            return [Leg("walk", round(d / p["walk_kmh"] * 60), 0, d, 0, d)]
        if mode == "transit":
            if d < 1.0:
                return []
            minutes = p["transit_overhead_min"] + d / p["transit_kmh"] * 60
            extra = max(0.0, d - p["transit_base_km"])
            fare = p["transit_base_fare"] + math.ceil(extra / 5) * p["transit_extra_per_5km"]
            transfers = 0 if d < 5 else (1 if d < 12 else 2)
            walk = p["transit_access_km"] + 0.2 * transfers
            minutes += walk / p["walk_kmh"] * 60
            return [Leg("transit", round(minutes), int(fare), walk, transfers, d)]
        if mode == "taxi":
            minutes = p["taxi_overhead_min"] + d / p["taxi_kmh"] * 60
            fare = p["taxi_base_fare"] + max(0.0, d - p["taxi_base_km"]) * p["taxi_per_km"]
            return [Leg("taxi", round(minutes), int(round(fare, -2)), 0.0, 0, d)]
        if mode == "car":
            minutes = p["car_overhead_min"] + d / p["car_kmh"] * 60
            fuel = d / p["car_km_per_l"] * p["fuel_won_per_l"]
            return [Leg("car", round(minutes), int(round(fuel, -1)), 0.0, 0, d)]
        raise ValueError(mode)


def main(num=40, seed=7):
    """솔버 비교는 plan() 을 거치지 않고 '같은 비용행렬 + 같은 가중치 + 같은 예산'에서 직접 비교.
    (plan() 은 예산 때문에 λ 를 조정할 수 있어서 솔버마다 가중치가 달라질 수 있기 때문)
    제약 위반 검사는 plan() 결과에 대해 수행.
    """
    rng = random.Random(seed)
    provider = EstimateProvider()
    names = ("given", "nearest", "ortools", "exact_fixed")
    gaps = {s: [] for s in names}
    fails = {s: 0 for s in names}
    times = {"exact": [], "ortools": []}
    violations, infeasible, solved = 0, 0, 0
    for k in range(num):
        req = random_request(rng, rng.choice([5, 6, 7]))
        cands = build_candidates(req, provider)

        # (1) plan() 결과 제약 검사
        for s in ("exact", "ortools"):
            r = plan(req, cands=cands, solver=s, time_limit_s=1)
            if r.ok:
                errs = check(r)
                if errs:
                    violations += 1
                    print(f"[#{k}] {s} 제약 위반: {errs}")

        # (2) 같은 조건에서 최적성 격차
        w = req.resolved_weights()
        legs = choose_legs(cands, w)
        t0 = time.time()
        ex = solve_exact(req, cands, w)       # 순서 + 이동수단 동시 최적화 (정답)
        times["exact"].append(time.time() - t0)
        if ex is None:
            infeasible += 1
            continue
        solved += 1
        for s in names:
            t0 = time.time()
            if s == "given":
                sch = solve_given_order(req, legs, w)
            elif s == "nearest":
                sch = solve_nearest_neighbor(req, legs, w)
            elif s == "exact_fixed":           # 수단을 미리 고정하고 순서만 완전탐색 (이전 방식)
                sch = solve_exact_fixed_modes(req, legs, w)
            else:
                sch, dropped = solve_ortools(req, legs, w, time_limit_s=1)
                times["ortools"].append(time.time() - t0)
                if dropped:
                    sch = None
            if sch is None:
                fails[s] += 1
                continue
            gap = (sch.objective - ex.objective) / ex.objective * 100
            if gap < -1e-6:
                print(f"[#{k}] {s} 가 exact 보다 좋음?! gap={gap:.3f}%")
            gaps[s].append(gap)

    print(f"\n시나리오 {num}개 | 풀림 {solved} | 해 없음(정확해 기준) {infeasible} | 제약 위반 {violations}")
    print(f"완전탐색 평균 {statistics.mean(times['exact']):.2f}s, "
          f"OR-Tools 평균 {statistics.mean(times['ortools']):.2f}s (시간제한 1s)")
    print(f"\n{'솔버':10s} {'실패':>4s} {'평균 격차':>9s} {'최대 격차':>9s} {'최적해 비율':>10s}")
    for s, g in gaps.items():
        if not g:
            continue
        opt = sum(1 for x in g if x < 1e-6) / len(g) * 100
        print(f"{s:10s} {fails[s]:4d} {statistics.mean(g):8.2f}% {max(g):8.2f}% {opt:9.0f}%")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
