"""모든 장소 쌍의 이동 후보를 모으고, 가중치에 맞는 수단을 구간별로 고른다.

노드 번호: 0 = 출발지, 1..n = 장소, n+1 = 도착지
"""
from __future__ import annotations

from .models import Leg, TripRequest
from . import config
from .providers import haversine_km

SAME_PLACE_KM = 0.005   # 5 m 이내면 같은 지점으로 봄 (카카오는 이런 구간의 경로를 거절함)


def nodes_of(req: TripRequest):
    return [req.origin, *req.places, req.destination]


def validate_modes(modes) -> None:
    modes = tuple(modes)
    if config.MODE_CAR in modes and len(modes) > 1:
        raise ValueError("자차(car)는 다른 수단과 섞을 수 없습니다. modes=('car',) 로 지정하세요.")
    for m in modes:
        if m not in config.MODES_PUBLIC + (config.MODE_CAR,):
            raise ValueError(f"unknown mode: {m}")


def build_candidates(req: TripRequest, provider) -> list[list[list[Leg]]]:
    """cands[i][j] = i->j 로 갈 수 있는 모든 (수단, 경로) 후보."""
    validate_modes(req.modes)
    nodes = nodes_of(req)
    N = len(nodes)
    cands = [[[] for _ in range(N)] for _ in range(N)]
    for i in range(N):
        for j in range(N):
            if i == j or j == 0 or i == N - 1:   # 출발지로 돌아가거나 도착지에서 나가는 구간은 불필요
                continue
            if haversine_km(nodes[i], nodes[j]) < SAME_PLACE_KM:
                # 같은 지점(예: 출발지 = 도착지, 같은 건물의 두 식당): 이동 없음. API 호출하지 않음
                cands[i][j] = [Leg("walk", 0, 0, 0.0, 0, 0.0, "same_place")]
                continue
            legs = []
            for m in req.modes:
                for leg in provider.legs(nodes[i], nodes[j], m):
                    if leg.mode == "walk" and leg.distance_km > req.max_walk_leg_km:
                        continue
                    legs.append(leg)
            if not legs:  # 걸어가기엔 멀고 대중교통도 없으면 -> 가능한 수단 중 아무거나라도 넣음
                for m in req.modes:
                    legs += provider.legs(nodes[i], nodes[j], m)
            cands[i][j] = legs
    if hasattr(provider, "save_cache"):
        provider.save_cache()
    return cands


def choose_legs(cands, w: dict) -> list[list[Leg | None]]:
    """구간마다 가중 비용이 가장 작은 후보를 선택 -> 하나의 비용 행렬로 만든다."""
    N = len(cands)
    best = [[None] * N for _ in range(N)]
    for i in range(N):
        for j in range(N):
            if cands[i][j]:
                best[i][j] = min(cands[i][j], key=lambda l: l.weighted(w))
    return best
