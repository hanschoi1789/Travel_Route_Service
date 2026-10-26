"""경로 최적화 솔버.

1) solve_nearest_neighbor : 기준선. 매번 가장 가까운(가중비용 최소) 장소로 이동.
2) solve_exact            : 완전탐색. 장소 8개 정도까지 정확한 최적해 (검증용 정답지).
3) solve_ortools          : Google OR-Tools. 장소가 많아도 빠르게 근사 최적해.
                            제약을 못 맞추면 장소를 빼고(drop) 결과를 알려줌.

세 솔버 모두 같은 simulate() 로 일정을 계산하므로 목적함수 값을 직접 비교할 수 있다.
"""
from __future__ import annotations

import itertools
from typing import Optional

from . import config
from .models import TripRequest, hm
from .matrix import choose_legs
from .schedule import Schedule, earliest_in, meal_slots, simulate, start_domain


def _restaurants(req: TripRequest) -> list[int]:
    return [i for i, p in enumerate(req.places) if p.category == "restaurant"]


def _slot_assignments(req: TripRequest):
    """식당마다 서로 다른 식사 슬롯 배정 (식당 2곳이면 점심/저녁 각각 1곳)."""
    rs = _restaurants(req)
    slots = meal_slots(req)
    if len(rs) > len(slots):
        return  # 식당이 식사 슬롯보다 많으면 불가능
    for perm in itertools.permutations(slots, len(rs)):
        yield dict(zip(rs, perm))


def _within_budget(sch: Schedule, req: TripRequest) -> bool:
    return req.budget is None or sch.total_won <= req.budget


# ---------------------------------------------------------------------------
def solve_exact_fixed_modes(req: TripRequest, legs, w: dict, max_places: int = 9) -> Optional[Schedule]:
    """(이전 방식) 구간별 수단을 미리 고정한 상태에서 순서만 완전탐색. 비교용."""
    n = len(req.places)
    if n > max_places:
        raise ValueError(f"완전탐색은 장소 {max_places}개 이하에서만 사용하세요 (현재 {n}개)")
    best = None
    assignments = list(_slot_assignments(req) or [])
    if not assignments:
        return None
    for order in itertools.permutations(range(n)):
        for slots in assignments:
            sch = simulate(list(order), slots, legs, req, w)
            if sch is None or not _within_budget(sch, req):
                continue
            if best is None or sch.objective < best.objective - 1e-9:
                best = sch
    return best



# ---------------------------------------------------------------------------
def _pareto(legs: list, w: dict) -> list:
    """한 구간의 후보 중 (시간, 요금, 기타 패널티) 모두에서 밀리는 후보는 제거."""
    out = []
    for l in legs:
        k = (l.minutes, l.won, l.non_time_cost(w))
        if not any((o.minutes <= k[0] and o.won <= k[1] and o.non_time_cost(w) <= k[2])
                   and (o.minutes, o.won, o.non_time_cost(w)) != k for o in legs):
            if all((o.minutes, o.won, o.non_time_cost(w)) != k for o in out):
                out.append(l)
    return out


def solve_exact(req: TripRequest, cands, w: dict, max_places: int = 9) -> Optional[Schedule]:
    """정확해: 방문 순서 + 식당 식사 슬롯 + 구간별 이동수단 + 체류시간을 '동시에' 최적화.

    방문 순서를 앞에서부터 하나씩 붙여가는 깊이우선탐색(DFS).
    각 부분 경로마다 '라벨' = (현재 시각, 누적 요금, 누적 기타 패널티) 들을 들고 다니며,
    세 값이 모두 더 나쁜 라벨은 버린다(파레토 지배). 이렇게 하면 구간마다 택시/대중교통/도보
    중 무엇을 고를지가 시간창·예산과 함께 정확히 결정된다.
    체류시간은 장소마다 [최소, 최대] 범위에서 STAY_STEP 단위로 고르며, 줄인 만큼 큰 벌점을
    받으므로 시간 안에 넣기 위해 꼭 필요할 때만 줄어든다.
    가지치기: (지금까지 목적함수 + 남은 최소 체류시간) 이 현재 최선보다 크면 더 볼 필요 없음.
    """
    places = req.places
    n = len(places)
    if n > max_places:
        raise ValueError(f"완전탐색은 장소 {max_places}개 이하에서만 사용하세요 (현재 {n}개)")
    DEST = n + 1
    t0, t_end = hm(req.start_time), hm(req.end_time)
    budget = req.budget
    wt = w["w_time"]
    arcs = [[_pareto(c, w) if c else [] for c in row] for row in cands]
    min_to_dest = [min((l.minutes for l in arcs[i][DEST]), default=10 ** 6) for i in range(n + 2)]
    slots_all = meal_slots(req)
    is_rest = [p.category == "restaurant" for p in places]
    if sum(is_rest) > len(slots_all):
        return None
    doms = {}
    for i, p in enumerate(places):
        for sl in (slots_all if is_rest[i] else [None]):
            for stay in p.stay_options:
                doms[(i, sl, stay)] = start_domain(p, req, sl, stay)
    M = config.stay_cut_weight(w)   # 체류 1분 단축 벌점 (프리셋별)
    # 남은 이동시간의 하한: 각 장소(와 도착지)로 '들어오는' 구간 중 가장 짧은 것
    min_in = [min((l.minutes for r in range(n + 2) for l in arcs[r][j]), default=0) for j in range(n + 2)]
    # 식당 r 을 식사 슬롯 sl 로 쓸 때 가장 늦게 들어갈 수 있는 시각 (체류시간 후보 중 최댓값)
    latest_meal = {}
    for i, p in enumerate(places):
        if is_rest[i]:
            for sl in slots_all:
                his = [doms[(i, sl, st)][-1][1] for st in p.stay_options if doms[(i, sl, st)]]
                latest_meal[(i, sl)] = max(his) if his else -1

    best = {"obj": float("inf"), "path": None, "sched": None}

    # 초기 기록: 빠른 근사해(가까운 곳부터 + 체류시간 균일 단축)로 먼저 하나 만들어 둠
    #  -> 이보다 나쁜 가지는 처음부터 잘라낼 수 있음
    legs0 = choose_legs(cands, w)
    for k in range(0, 1 + max((-(-(p.stay - p.stay_range[0]) // config.STAY_STEP) for p in places), default=0)):
        st = {i: max(p.stay_range[0], p.stay - k * config.STAY_STEP) for i, p in enumerate(places)}
        inc = solve_nearest_neighbor(req, legs0, w, stays=st)
        if inc is not None and _within_budget(inc, req):
            best["obj"], best["sched"] = inc.objective, inc
            break

    # 라벨: (t, won, nt, path)  path = (이전 path, place_idx, slot, leg, stay)
    #   nt = 돈·도보·환승 가중합 + 체류시간 단축 벌점
    def extend(labels, prev_node, i, sl):
        p = places[i]
        out = []
        for stay in p.stay_options:
            dom = doms[(i, sl, stay)]
            if not dom:
                continue
            cut_cost = M * (p.stay - stay)
            for (t, won, nt, path) in labels:
                for leg in arcs[prev_node][i + 1]:
                    arrive = t + leg.minutes
                    st = earliest_in(dom, arrive)
                    if st is None:
                        continue
                    dep = st + stay
                    if dep + min_to_dest[i + 1] > t_end:
                        continue
                    won2 = won + leg.won + p.fee
                    if budget is not None and won2 > budget:
                        continue
                    out.append((dep, won2, nt + leg.non_time_cost(w) + cut_cost,
                                (path, i, sl, leg, stay)))
        return _prune(out)

    def _prune(labels):
        """파레토 지배 제거. 예산이 없으면 쓴 돈(won)은 비교할 필요가 없어서 (시각, 점수) 2개만 봄.
        (돈은 이미 점수 nt 에 가중치로 들어가 있고, won 은 예산 제약 확인용일 뿐)"""
        if budget is None:
            labels.sort(key=lambda x: (x[0], x[2]))
            kept, best_nt = [], float("inf")
            for L in labels:                    # 시각 순으로 보며 점수가 더 낮아질 때만 남김
                if L[2] < best_nt - 1e-12:
                    kept.append(L)
                    best_nt = L[2]
            return kept
        labels.sort(key=lambda x: (x[0], x[1], x[2]))
        kept = []
        for L in labels:
            if not any(K[0] <= L[0] and K[1] <= L[1] and K[2] <= L[2] for K in kept):
                kept.append(L)
        return kept

    def meals_reachable(t, remaining, free_slots):
        """아직 안 간 식당들이 남은 식사 슬롯에 (서로 다른 슬롯으로) 아직 들어갈 수 있는지.
        지금 시각 t 에서 가장 빨리 가도 식사시간을 놓치면 이 가지는 가망 없음."""
        rest = [r for r in remaining if is_rest[r]]
        if not rest:
            return True
        alive = []
        for r in rest:
            a = {sl for sl in free_slots if latest_meal[(r, sl)] >= t + min_in[r + 1]}
            if not a:
                return False
            alive.append(a)
        return len(set().union(*alive)) >= len(rest)   # 식당이 최대 2곳이라 이걸로 충분

    def dfs(labels, prev_node, remaining, free_slots, stay_left):
        # 가지치기: 목적함수 하한 = 지금까지 + 남은 최소 체류시간(시간 점수)
        #          + (종료시각 안에 넣으려면 어차피 줄여야 하는 체류시간) × 단축 벌점
        need = sum(places[j].stay + min_in[j + 1] for j in remaining) + min_in[DEST]
        labels = [L for L in labels
                  if wt * (L[0] - t0 + stay_left) + L[2] + M * max(0, L[0] + need - t_end)
                  < best["obj"] - 1e-9 and meals_reachable(L[0], remaining, free_slots)]
        if not labels:
            return
        if not remaining:
            for (t, won, nt, path) in labels:
                for leg in arcs[prev_node][DEST]:
                    end = t + leg.minutes
                    if end > t_end or (budget is not None and won + leg.won > budget):
                        continue
                    obj = wt * (end - t0) + nt + leg.non_time_cost(w)
                    if obj < best["obj"] - 1e-9:
                        best["obj"], best["path"] = obj, (path, leg)
            return
        # 가까운 곳부터 탐색하면 좋은 해를 빨리 찾아서 가지치기가 잘 됨
        order = sorted(remaining, key=lambda i: min((l.minutes for l in arcs[prev_node][i + 1]), default=10 ** 6))
        for i in order:
            rest = remaining - {i}
            for sl in (sorted(free_slots) if is_rest[i] else [None]):
                new = extend(labels, prev_node, i, sl)
                if new:
                    dfs(new, i + 1, rest, free_slots - {sl} if sl else free_slots,
                        stay_left - places[i].stay_range[0])

    dfs([(t0, 0, 0.0, None)], 0, frozenset(range(n)), frozenset(slots_all),
        sum(p.stay_range[0] for p in places))
    if best["path"] is None:
        return best["sched"]      # 탐색에서 더 좋은 해가 없으면 초기 기록이 최적

    path, leg_out = best["path"]
    order, slots, chosen, stays = [], {}, [], {}
    while path is not None:
        path, i, sl, leg, stay = path
        order.append(i)
        chosen.append(leg)
        stays[i] = stay
        if sl:
            slots[i] = sl
    order.reverse()
    chosen.reverse()
    return simulate(order, slots, None, req, w, chosen=chosen + [leg_out], stays=stays)

# ---------------------------------------------------------------------------
def solve_nearest_neighbor(req: TripRequest, legs, w: dict, stays=None) -> Optional[Schedule]:
    """기준선: 현재 위치에서 '가장 빨리/싸게 방문을 시작할 수 있는' 장소로 욕심쟁이 이동.

    점수 = w_time·(이동 + 대기) + (돈·도보·환승 가중합). 대기를 넣어야 저녁 식당에
    아침부터 가서 기다리는 식의 선택을 피한다.
    """
    n = len(req.places)
    best = None
    open_req = _open_end(req)
    for slots in _slot_assignments(req):
        order, prev, left = [], 0, set(range(n))
        t_prev = hm(req.start_time)
        while left:
            trials = []
            for j in left:
                if legs[prev][j + 1] is None:
                    continue
                sim = simulate(order + [j], slots, legs, open_req, w, stays=stays)
                if sim is None:
                    continue
                st = sim.stops[-1]
                score = w["w_time"] * (st.start - t_prev) + st.leg_in.non_time_cost(w)
                trials.append((score, j, sim))
            trials.sort(key=lambda x: x[0])
            for _, j, sim in trials:
                # 간단한 앞보기: 남은 식당들이 아직 식사시간 안에 갈 수 있는지 확인
                t_now = sim.stops[-1].depart
                if all(
                    earliest_in(start_domain(req.places[r], req, slots[r], (stays or {}).get(r)),
                                t_now + legs[j + 1][r + 1].minutes) is not None
                    for r in left if r != j and r in slots and legs[j + 1][r + 1]
                ):
                    order.append(j)
                    left.remove(j)
                    prev = j + 1
                    t_prev = t_now
                    break
            else:
                break
        if left:
            continue
        sch = simulate(order, slots, legs, req, w, stays=stays)
        if sch and _within_budget(sch, req) and (best is None or sch.objective < best.objective):
            best = sch
    return best


def solve_given_order(req: TripRequest, legs, w: dict, stays=None) -> Optional[Schedule]:
    """비교용: 사용자가 입력한 순서 그대로 (식당 슬롯만 가장 좋은 것으로)."""
    best = None
    for slots in _slot_assignments(req):
        sch = simulate(list(range(len(req.places))), slots, legs, req, w, stays=stays)
        if sch and _within_budget(sch, req) and (best is None or sch.objective < best.objective):
            best = sch
    return best


def _open_end(req: TripRequest) -> TripRequest:
    """중간 단계 검사용: 도착지 도착시각 제한만 느슨하게."""
    from dataclasses import replace
    return replace(req, end_time="23:59")


# ---------------------------------------------------------------------------
def solve_ortools(req: TripRequest, legs, w: dict, time_limit_s: int = 3,
                  scale: int = 100, warm_start: bool = True,
                  stays=None) -> tuple[Optional[Schedule], list[int]]:
    """OR-Tools Routing 으로 풀기. (schedule, dropped_place_indices) 반환.

    warm_start=True 면 Nearest Neighbor 해를 초기해로 넣어서 시작 (NN 보다 나빠지지 않음).
    """
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    places = req.places
    n = len(places)
    slots = meal_slots(req)
    DROP_PENALTY = 10_000_000

    # --- 노드 구성: 식당은 식사 슬롯별로 복제해서 그중 하나만 방문하도록 ---
    # visit_nodes[k] = (place_idx, slot)
    visit_nodes: list[tuple[int, Optional[str]]] = []
    for i, p in enumerate(places):
        if p.category == "restaurant":
            for s in slots:
                visit_nodes.append((i, s))
        else:
            visit_nodes.append((i, None))
    V = len(visit_nodes)
    START, END = V, V + 1          # 솔버 노드 번호
    num_nodes = V + 2

    def loc(node):                  # 솔버 노드 -> legs 행렬 인덱스
        if node == START:
            return 0
        if node == END:
            return n + 1
        return visit_nodes[node][0] + 1

    def stay(node):
        if node >= V:
            return 0
        pi = visit_nodes[node][0]
        return (stays or {}).get(pi, places[pi].stay)

    def fee(node):
        return places[visit_nodes[node][0]].fee if node < V else 0

    def leg(a, b):
        la, lb = loc(a), loc(b)
        if la == lb:
            return None
        return legs[la][lb]

    manager = pywrapcp.RoutingIndexManager(num_nodes, 1, [START], [END])
    routing = pywrapcp.RoutingModel(manager)
    BIG = 10 ** 6

    # 같은 식당의 점심/저녁 복제본끼리, 불가능한 구간은 큰 값으로 막음
    def time_cb(fi, ti):
        a, b = manager.IndexToNode(fi), manager.IndexToNode(ti)
        l = leg(a, b)
        return stay(a) + (l.minutes if l else BIG)

    def cost_cb(fi, ti):
        a, b = manager.IndexToNode(fi), manager.IndexToNode(ti)
        l = leg(a, b)
        return int(round(scale * l.non_time_cost(w))) if l else BIG

    def money_cb(fi, ti):
        a, b = manager.IndexToNode(fi), manager.IndexToNode(ti)
        l = leg(a, b)
        return fee(a) + (l.won if l else BIG)

    t_idx = routing.RegisterTransitCallback(time_cb)
    c_idx = routing.RegisterTransitCallback(cost_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(c_idx)

    # --- 시간 차원: 누적값 = 자정 기준 분 ---
    horizon = 24 * 60 * 2
    routing.AddDimension(t_idx, horizon, horizon, False, "Time")   # slack = 대기 허용
    tdim = routing.GetDimensionOrDie("Time")
    tdim.SetSpanCostCoefficientForAllVehicles(int(round(scale * w["w_time"])))
    day_lo, day_hi = hm(req.start_time), hm(req.end_time)
    tdim.CumulVar(routing.Start(0)).SetRange(day_lo, day_lo)
    tdim.CumulVar(routing.End(0)).SetRange(day_lo, day_hi)

    for node, (pi, s) in enumerate(visit_nodes):
        idx = manager.NodeToIndex(node)
        dom = start_domain(places[pi], req, s, stay(node))
        if not dom:
            routing.solver().Add(routing.ActiveVar(idx) == 0)  # 애초에 불가능한 복제본
            continue
        cv = tdim.CumulVar(idx)
        cv.SetRange(dom[0][0], dom[-1][1])
        for (_, hi), (lo, _) in zip(dom, dom[1:]):   # 구간 사이 빈틈 제거
            if hi + 1 <= lo - 1:
                cv.RemoveInterval(hi + 1, lo - 1)

    # --- 방문 필수(못 맞추면 큰 패널티 내고 제외) ---
    for i, p in enumerate(places):
        copies = [manager.NodeToIndex(k) for k, (pi, _) in enumerate(visit_nodes) if pi == i]
        routing.AddDisjunction(copies, DROP_PENALTY, 1)

    # --- 식사 슬롯마다 식당은 최대 1곳 ---
    for s in slots:
        def demand_cb(fi, s=s):
            node = manager.IndexToNode(fi)
            return 1 if node < V and visit_nodes[node][1] == s else 0
        d_idx = routing.RegisterUnaryTransitCallback(demand_cb)
        routing.AddDimension(d_idx, 0, 1, True, f"meal_{s}")

    # --- 예산 ---
    if req.budget is not None:
        m_idx = routing.RegisterTransitCallback(money_cb)
        routing.AddDimension(m_idx, 0, req.budget, True, "Money")

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = time_limit_s

    sol = None
    init = solve_nearest_neighbor(req, legs, w, stays=stays) if warm_start else None
    if init is not None:
        route = []
        for st in init.stops:
            k = next(k for k, (pi, sl) in enumerate(visit_nodes)
                     if pi == st.place_idx and sl == st.slot)
            route.append(manager.NodeToIndex(k))
        routing.CloseModelWithParameters(params)
        assignment = routing.ReadAssignmentFromRoutes([route], True)
        if assignment is not None:
            sol = routing.SolveFromAssignmentWithParameters(assignment, params)
    if sol is None:
        sol = routing.SolveWithParameters(params)
    if sol is None:
        return None, list(range(n))

    order, chosen_slots = [], {}
    idx = routing.Start(0)
    while not routing.IsEnd(idx):
        node = manager.IndexToNode(idx)
        if node < V:
            pi, s = visit_nodes[node]
            order.append(pi)
            if s:
                chosen_slots[pi] = s
        idx = sol.Value(routing.NextVar(idx))
    dropped = [i for i in range(n) if i not in order]
    sch = simulate(order, chosen_slots, legs, req, w, stays=stays)
    return sch, dropped
