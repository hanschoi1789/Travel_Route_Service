"""최상위 함수: plan(req) -> PlanResult

흐름
  1. 모든 구간 × 수단의 이동 후보 수집 (provider)
  2. 솔버로 방문 순서 + 식당 식사 슬롯 + 구간별 이동수단 결정
     - exact  : 셋을 동시에 최적화하는 정확해 (장소 8개 이하 기본)
     - ortools: 구간별 수단을 가중치로 먼저 고르고 순서를 최적화 (장소가 많을 때).
                고른 수단으로 시간/예산을 못 맞추면 가중치를 조정해 다시 풂
  3. 해가 없으면 원인(시간 부족 / 최소 필요 금액)을 알려줌
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from . import config
from .matrix import build_candidates, choose_legs
from .models import TripRequest, fmt, hm
from .schedule import Schedule, objective, simulate
from .solvers import solve_exact, solve_given_order, solve_nearest_neighbor, solve_ortools

SLOT_KO = {"lunch": "점심", "dinner": "저녁"}
MODE_KO = {"walk": "도보", "transit": "대중교통", "taxi": "택시", "car": "자차"}


@dataclass
class PlanResult:
    req: TripRequest
    schedule: Optional[Schedule]
    solver: str
    weights: dict
    dropped: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.schedule is not None

    # 시각화 파트로 넘길 JSON 형태
    def to_dict(self) -> dict:
        req, sch = self.req, self.schedule
        if sch is None:
            return {"ok": False, "warnings": self.warnings,
                    "dropped": [req.places[i].name for i in self.dropped]}
        stops = []
        for s in sch.stops:
            p = req.places[s.place_idx]
            stops.append({
                "name": p.name, "lat": p.lat, "lng": p.lng, "category": p.category,
                "meal": s.slot, "arrive": fmt(s.arrive), "start": fmt(s.start),
                "depart": fmt(s.depart), "wait_min": s.wait, "stay_min": s.stay,
                "stay_range": list(p.stay_range),
                "leg_in": _leg_dict(s.leg_in),
            })
        return {
            "ok": True, "solver": self.solver, "weights": self.weights,
            "origin": {"name": req.origin.name, "lat": req.origin.lat, "lng": req.origin.lng},
            "destination": {"name": req.destination.name, "lat": req.destination.lat,
                            "lng": req.destination.lng},
            "depart": fmt(sch.depart_origin), "latest_departure": fmt(sch.latest_departure),
            "arrive": fmt(sch.arrive_dest),
            "stops": stops, "leg_to_destination": _leg_dict(sch.leg_out),
            "totals": {
                "duration_min": sch.arrive_dest - sch.depart_origin,
                "travel_min": sch.total_travel_min, "wait_min": sch.total_wait_min,
                "cost_won": sch.total_won, "walk_km": sch.total_walk_km,
                "transfers": sch.total_transfers,
                "stay_min": sum(x.stay for x in sch.stops),
                "stay_cut_min": stay_cut(sch, req),
                "score": round(sch.objective - config.stay_cut_weight(self.weights) * stay_cut(sch, req), 2),
                "objective": round(sch.objective, 2),
            },
            "dropped": [req.places[i].name for i in self.dropped],
            "warnings": self.warnings,
        }

    def pretty(self) -> str:
        d = self.to_dict()
        if not d["ok"]:
            return "경로를 찾지 못했습니다.\n" + "\n".join("  ! " + w for w in d["warnings"])
        L = [f"[{self.solver}] 출발 {d['depart']} (늦어도 {d['latest_departure']}까지 출발 가능) "
             f"-> 도착 {d['arrive']}"]
        L.append(f"  {self.req.origin.name}")
        for s in d["stops"]:
            L.append("   " + _leg_str(s["leg_in"]))
            meal = f" [{SLOT_KO[s['meal']]}]" if s["meal"] else ""
            wait = f" (대기 {s['wait_min']}분)" if s["wait_min"] else ""
            cut = s["stay_range"][1] - s["stay_min"]
            short = f" (체류 {s['stay_min']}분, {cut}분 단축)" if cut else ""
            L.append(f"  {s['start']}~{s['depart']}  {s['name']}{meal}{wait}{short}")
        L.append("   " + _leg_str(d["leg_to_destination"]))
        L.append(f"  {self.req.destination.name}")
        t = d["totals"]
        L.append(f"  총 {t['duration_min']}분 (이동 {t['travel_min']}분, 대기 {t['wait_min']}분) | "
                 f"{t['cost_won']:,}원 | 도보 {t['walk_km']}km | 환승 {t['transfers']}회")
        if d["dropped"]:
            L.append(f"  제외된 장소: {', '.join(d['dropped'])}")
        for w in d["warnings"]:
            L.append(f"  ! {w}")
        return "\n".join(L)


def _leg_dict(l):
    return {"mode": l.mode, "minutes": l.minutes, "won": l.won, "walk_km": round(l.walk_km, 2),
            "transfers": l.transfers, "distance_km": round(l.distance_km, 2), "source": l.source,
            "detail": l.detail}


KIND_KO = {"SUBWAY": "지하철", "BUS": "버스"}


def transit_summary(detail: list) -> str:
    """예: '지하철 4호선 서울역→혜화(5정거장) → 버스 7022 혜화→창경궁(3정거장)'"""
    rides = []
    for d in detail:
        if d.get("type") == "WALKING":
            continue
        kind = KIND_KO.get(d.get("type"), d.get("type", ""))
        lines = "/".join(d.get("lines", [])[:3]) + ("…" if len(d.get("lines", [])) > 3 else "")
        span = f" {d['from']}→{d['to']}" if d.get("from") else ""
        n = f"({d['stops']}정거장)" if d.get("stops") else ""
        rides.append(f"{kind} {lines}{span}{n}".replace("  ", " ").strip())
    return " → ".join(rides)


def _leg_str(l):
    head = f"↓ {MODE_KO[l['mode']]} {l['minutes']}분, {l['won']:,}원"
    if l["mode"] != "transit":
        return head
    if l.get("detail"):
        tr = f"환승 {l['transfers']}회" if l["transfers"] else "환승 없음"
        return f"{head} [{tr}] {transit_summary(l['detail'])}"
    # 세부 노선 정보가 없는 대중교통 구간
    tr = f"환승 {l['transfers']}회" if l["transfers"] else "환승 없음"
    return f"{head} [{tr}, 노선 정보 없음]"


# ---------------------------------------------------------------------------
def _refine_without(req: TripRequest, cands, dropped: list[int], w: dict):
    """dropped 장소를 뺀 나머지로 정확해를 풀고, 원래 req 의 장소 번호로 되돌려 일정을 만든다."""
    n = len(req.places)
    kept = [i for i in range(n) if i not in dropped]
    sub = replace(req, places=[req.places[i] for i in kept])
    nodes = [0] + [i + 1 for i in kept] + [n + 1]          # 원래 행렬에서 쓸 노드들
    sub_cands = [[cands[a][b] for b in nodes] for a in nodes]
    ex = solve_exact(sub, sub_cands, w)
    if ex is None:
        return None
    order = [kept[s.place_idx] for s in ex.stops]
    slots = {kept[s.place_idx]: s.slot for s in ex.stops if s.slot}
    stays = {kept[s.place_idx]: s.stay for s in ex.stops}
    chosen = [s.leg_in for s in ex.stops] + [ex.leg_out]
    return simulate(order, slots, None, req, w, chosen=chosen, stays=stays)


def stay_cut(sch, req: TripRequest) -> int:
    """체류시간을 최대값에서 줄인 분의 합."""
    return sum(req.places[s.place_idx].stay - s.stay for s in sch.stops)


def _stay_warning(req: TripRequest, sch, warnings: list) -> None:
    if sch is None:
        return
    cuts = [(req.places[s.place_idx].name, req.places[s.place_idx].stay - s.stay)
            for s in sch.stops if s.stay < req.places[s.place_idx].stay]
    if cuts:
        warnings.append("체류시간을 줄인 장소: "
                        + ", ".join(f"{n} -{c}분" for n, c in cuts))


def _exact_diagnosis(req: TripRequest, cands) -> list[str]:
    """정확해가 없을 때 원인 진단. 완전탐색 기반이라 결론이 확실하다."""
    free = replace(req, budget=None)
    any_w = {"w_time": 1.0, "w_money": 0.0, "w_walk": 0.0, "w_transfer": 0.0}
    if solve_exact(free, cands, any_w) is None:
        total_stay = sum(p.stay_range[0] for p in req.places)
        return [f"체류시간을 최소로 줄이고 어떤 순서·이동수단을 써도 {req.start_time}~{req.end_time} 안에 "
                f"모든 장소를 돌 수 없습니다 (최소 체류시간 합계만 {total_stay // 60}시간 {total_stay % 60}분, "
                "식당은 식사시간에만 가능). 장소를 줄이거나 종료시각을 늦춰 보세요."]
    cheap_w = {"w_time": 0.0, "w_money": 1.0, "w_walk": 0.0, "w_transfer": 0.0}
    cheapest = solve_exact(free, cands, cheap_w)
    fees = sum(p.fee for p in req.places)
    return [f"시간 안에 모든 장소를 돌려면 최소 {cheapest.total_won:,}원이 필요합니다 "
            f"(입장료 {fees:,}원 포함, 예산 {req.budget:,}원). 예산을 늘리거나 장소를 줄여 보세요."]


def plan(req: TripRequest, provider=None, solver: str = "auto", cands=None,
         time_limit_s: int = 3) -> PlanResult:
    """solver: 'auto' | 'exact' | 'ortools' | 'nearest' | 'given'(입력 순서 그대로, 비교용)
    auto = 장소 8개 이하면 exact, 많으면 ortools
    cands 를 넘기면 API 재호출 없이 재사용 (프리셋 비교할 때 유용)

    exact   : 순서 + 식사 슬롯 + 구간별 이동수단을 동시에 최적화 (정확해)
    그 외   : 구간별 이동수단을 가중치로 미리 고른 뒤 순서를 정함. 그렇게 고른 수단으로
              시간/예산을 못 맞추면 가중치를 조정해 다시 풂 (근사)
    """
    if cands is None:
        if provider is None:
            raise ValueError("provider(KakaoProvider)가 필요합니다. 추정 모드는 지원하지 않습니다. "
                             "plan(req, provider=KakaoProvider()) 처럼 넘겨 주세요.")
        cands = build_candidates(req, provider)
    if solver == "auto":
        solver = "exact" if len(req.places) <= 8 else "ortools"

    base_w = req.resolved_weights()
    warnings = []
    defaults = [p.name for p in req.places if p.hours_is_default]
    if defaults:
        warnings.append("운영시간 기본값(임의) 사용: " + ", ".join(defaults))

    # ---- 정확해 -------------------------------------------------------------
    if solver == "exact":
        sch = solve_exact(req, cands, base_w)
        if sch is None:
            warnings.extend(_exact_diagnosis(req, cands))
        _stay_warning(req, sch, warnings)
        return PlanResult(req, sch, solver, base_w, [], warnings)

    # ---- 근사 솔버 (ortools / nearest / given) --------------------------------
    state = {"stays": None}   # 근사 솔버용 체류시간 (None = 모두 최대값)

    def run(w, use_budget=True, fast=False):
        r = req if use_budget else replace(req, budget=None)
        legs = choose_legs(cands, w)
        st = state["stays"]
        if solver == "nearest":
            return solve_nearest_neighbor(r, legs, w, stays=st), []
        if solver == "given":
            return solve_given_order(r, legs, w, stays=st), []
        if solver == "ortools":
            return solve_ortools(r, legs, w, time_limit_s=1 if fast else time_limit_s, stays=st)
        raise ValueError(solver)

    def stays_at(k):
        """모든 장소의 체류시간을 최대값에서 k 단계(STAY_STEP 분씩) 줄인 값 (최소값 아래로는 안 감)."""
        return {i: max(p.stay_range[0], p.stay - k * config.STAY_STEP) for i, p in enumerate(req.places)}

    def good(res):
        sch, dropped = res
        return sch is not None and not dropped

    def adjusted(tf, mf):
        # 원래 가중치로 고른 수단 조합이 시간/예산을 못 맞출 때 가중치에 배수를 곱해 다시 고름
        #   tf : 시간 가중치 배수 -> 더 빠른 수단(택시 등) 쪽으로
        #   mf : 비용 가중치 배수 -> 더 싼 수단(대중교통/도보) 쪽으로
        return dict(base_w, w_time=max(base_w["w_time"], 0.05) * tf,
                    w_money=max(base_w["w_money"], 0.05) * mf)

    w = dict(base_w)
    res = run(w)
    # 1) 시간이 모자라면 체류시간을 모든 장소에서 같이 조금씩 줄여 봄
    max_k = max((-(-(p.stay - p.stay_range[0]) // config.STAY_STEP) for p in req.places), default=0)
    k = 0
    tried = [(len(res[1]) if res[0] is not None else 10 ** 6, 0, res)]
    while not good(res) and k < max_k:
        k += 1
        state["stays"] = stays_at(k)
        res = run(w)
        tried.append((len(res[1]) if res[0] is not None else 10 ** 6, k, res))
    if not good(res):
        # 끝까지 못 맞추면: 빠지는 장소가 가장 적은 단계 중 체류시간을 가장 덜 줄인 단계로
        _, k, res = min(tried, key=lambda x: (x[0], x[1]))
        state["stays"] = stays_at(k) if k else None
    # 2) 그래도 안 되면 (체류는 최소로 둔 채) 이동수단 가중치를 조정
    if not good(res):
        quick = solver == "ortools"
        tfs = [1, 3, 10, 40] if quick else [1, 1.5, 2, 3, 5, 8, 13, 20, 40, 80]
        mfs = ([1] if req.budget is None else
               ([1, 4, 20] if quick else [1, 1.5, 2, 3, 5, 8, 13, 20, 40, 80, 160]))
        feasible = []
        for tf in tfs:
            for mf in mfs:
                if tf == 1 and mf == 1:
                    continue
                r = run(adjusted(tf, mf), fast=quick)
                if good(r):
                    feasible.append((objective(r[0], req, base_w), tf, mf, r))
        if feasible:
            best_obj, tf, mf, res = min(feasible, key=lambda x: x[0])
            w = adjusted(tf, mf)
            if quick:
                r = run(w)
                if good(r) and objective(r[0], req, base_w) <= best_obj:
                    res, best_obj = r, objective(r[0], req, base_w)
            res[0].objective = best_obj
            if req.budget is not None and good(run(base_w, use_budget=False, fast=quick)):
                warnings.append(f"원래 기준으로 고른 이동수단은 예산 {req.budget:,}원을 넘어서, "
                                "일부 구간을 더 싼 수단으로 바꿈")
            else:
                warnings.append(f"원래 기준으로 고른 이동수단으로는 {req.end_time} 안에 못 끝나서, "
                                "일부 구간을 더 빠른 수단으로 바꿈")
        elif len(req.places) <= 8:
            # 근사 솔버가 못 찾았지만 장소가 적으면 정확해로 확인
            ex = solve_exact(req, cands, base_w)
            if ex is not None:
                res, w = (ex, []), base_w
                warnings.append(f"{solver} 로는 제약을 모두 만족하는 해를 못 찾아서 정확해(exact)로 대체함")
            else:
                warnings.extend(_exact_diagnosis(req, cands))

    sch, dropped = res
    if sch is not None and dropped and len(req.places) - len(dropped) <= 8:
        # 뺄 장소가 정해졌으면, 남은 장소만으로 정확해를 다시 풀어 순서·수단·체류시간을 최적화
        refined = _refine_without(req, cands, dropped, base_w)
        if refined is not None:
            sch = refined
    _stay_warning(req, sch, warnings)
    if sch is None:
        warnings.append("시간/예산 제약을 모두 만족하는 경로가 없습니다.")
    if dropped:
        warnings.append("제약을 맞추려고 일부 장소를 뺐습니다.")
    return PlanResult(req, sch, solver, w, dropped, warnings)
