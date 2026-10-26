"""구간 이동 정보(시간/요금/도보거리/환승) 제공자.

- KakaoProvider    : 카카오모빌리티 자동차 길찾기 + 카카오맵 대중교통/도보 경로 API.
                     결과는 JSON 파일에 캐시해서 같은 구간은 다시 호출하지 않음.

모든 provider 는 legs(a, b, mode) -> list[Leg] 를 제공한다.
(대중교통처럼 후보가 여러 개면 여러 개를 반환하고, 가중치에 맞는 것을 matrix 단계에서 고른다.)
"""
from __future__ import annotations

import json
import math
import os
from typing import Optional

from . import config
from .models import Leg, Location


def haversine_km(a: Location, b: Location) -> float:
    return _hav(a.lat, a.lng, b.lat, b.lng)


def _hav(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _thin(pts: list, max_pts: int = 300) -> list:
    """지도에 그릴 좌표가 너무 많으면 고르게 줄임 (캐시 파일 크기 절약)."""
    if len(pts) <= max_pts:
        return pts
    step = len(pts) / (max_pts - 1)
    return [pts[int(k * step)] for k in range(max_pts - 1)] + [pts[-1]]


# ---------------------------------------------------------------------------
class KakaoProvider:
    """카카오 API provider.

    필요한 키: 카카오 디벨로퍼스 앱의 REST API 키 (자동차 길찾기는 카카오모빌리티 쪽 권한도 필요).
    무료 한도(2026-09 기준 문서): 자동차 길찾기 10,000건/일, 대중교통·도보 경로 1,000건/일.
    추정값으로 대체하지 않는다. 인증 실패·한도 초과·네트워크 오류는 바로 오류로 멈추고,
    경로가 없는 구간(404 등)은 그 수단의 후보에서만 제외한다.
    """

    CAR_URL = "https://apis-navi.kakaomobility.com/v1/directions"
    TRANSIT_URL = "https://dapi.kakao.com/v2/routing/publictraffic"
    WALK_URL = "https://dapi.kakao.com/v2/routing/walk"

    def __init__(self, rest_api_key: Optional[str] = None, cache_path: str = "kakao_cache.json",
                 timeout: float = 5.0):
        import requests  # 선택 의존성

        self._requests = requests
        # 우선순위: 직접 넘긴 키 > config.py 의 키 > 환경변수
        self.key = (rest_api_key or config.KAKAO_REST_API_KEY.strip()
                    or os.environ.get("KAKAO_REST_API_KEY"))
        if not self.key:
            raise ValueError("카카오 REST API 키가 없습니다. "
                             "route_planner/config.py 의 KAKAO_REST_API_KEY 에 넣어주세요.")
        self.cache_path = cache_path
        self.timeout = timeout
        self.cache = {}
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                self.cache = json.load(f)
        self.calls = {"car": 0, "transit": 0, "walk": 0}
        self._failed = set()

    # -- public -----------------------------------------------------------
    def legs(self, a: Location, b: Location, mode: str) -> list[Leg]:
        # API 호출 전에 걸러내기 (한도 절약)
        d_est = haversine_km(a, b) * config.EST["detour"]
        if mode == "walk" and d_est > config.MAX_WALK_LEG_KM * 1.5:
            return []
        if mode == "transit" and d_est < 1.0:
            return []

        key = f"{mode}:{a.lat:.5f},{a.lng:.5f}>{b.lat:.5f},{b.lng:.5f}"
        if key in self.cache:
            cached = self.cache[key]
            # 예전 버전 캐시(노선·경로 좌표 없음)면 한 번 다시 받아옴
            stale = any(x.get("source") == "kakao" and not x.get("detail") for x in cached)
            if not stale:
                return [Leg(**x) for x in cached]
        if key in self._failed:          # 이번 실행에서 이미 실패한 구간 -> 재호출 안 함
            return []
        try:
            if mode in ("taxi", "car"):
                legs = self._car(a, b, mode)
            elif mode == "transit":
                legs = self._transit(a, b)
            elif mode == "walk":
                legs = self._walk(a, b)
            else:
                raise ValueError(mode)
        except self._requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code in (401, 403):
                # 키/권한 문제는 조용히 넘어가면 안 되므로 바로 중단
                raise RuntimeError(
                    f"카카오 API 인증 실패 ({code}, {mode}). "
                    "401: REST API 키가 틀림 / 403: 카카오맵 사용 설정 OFF 또는 해당 API 권한 없음. "
                    f"응답: {e.response.text[:200]}") from e
            if code == 429:
                self.save_cache()   # 지금까지 받은 구간은 보존
                raise RuntimeError(
                    f"카카오 API 호출 한도 초과 (429, {mode}). 받아 둔 구간은 {self.cache_path}에 저장했습니다. "
                    "한도가 풀린 뒤 다시 실행하면 이어서 진행합니다.") from e
            print(f"[KakaoProvider] {mode} {a.name}->{b.name} 경로 없음/조회 실패({code}) - 이 구간의 {mode} 후보 제외")
            self._failed.add(key)
            return []
        except self._requests.RequestException as e:  # 네트워크 오류, 시간 초과 등
            self.save_cache()
            raise RuntimeError(
                f"카카오 API 호출 실패 ({mode} {a.name}->{b.name}): {type(e).__name__} {e}. "
                f"받아 둔 구간은 {self.cache_path}에 저장했습니다.") from e
        except Exception as e:  # 응답 해석 실패 등: 추정하지 않고 이 수단만 제외
            print(f"[KakaoProvider] {mode} {a.name}->{b.name} 응답 해석 실패 - 이 구간의 {mode} 후보 제외: "
                  f"{type(e).__name__} {e}")
            self._failed.add(key)
            return []
        self.cache[key] = [x.__dict__ for x in legs]
        return legs

    def report(self) -> str:
        return (f"[KakaoProvider] 이번 실행 API 호출: 자동차 {self.calls['car']}, "
                f"대중교통 {self.calls['transit']}, 도보 {self.calls['walk']} | "
                f"경로 없음·해석 실패로 제외한 구간 {len(self._failed)}개 | 캐시 {len(self.cache)}개 구간")

    def save_cache(self):
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=1)

    # -- 개별 API ---------------------------------------------------------
    def _get(self, url, params):
        r = self._requests.get(url, params=params, timeout=self.timeout,
                               headers={"Authorization": f"KakaoAK {self.key}"})
        r.raise_for_status()
        return r.json()

    def _car(self, a, b, mode) -> list[Leg]:
        self.calls["car"] += 1
        # 택시와 자차는 같은 호출 결과를 공유하므로 둘 다 캐시에 넣음
        data = self._get(self.CAR_URL, {
            "origin": f"{a.lng},{a.lat}", "destination": f"{b.lng},{b.lat}",
            "priority": "RECOMMEND",
        })
        route = data["routes"][0]
        if route.get("result_code", 0) != 0:
            raise RuntimeError(route.get("result_msg"))
        s = route["summary"]
        dist_km = s["distance"] / 1000
        drive_min = s["duration"] / 60
        toll = s.get("fare", {}).get("toll", 0)
        taxi_fare = s.get("fare", {}).get("taxi", 0)
        p = config.EST
        pts = []
        for sec in route.get("sections", []) or []:
            for road in sec.get("roads", []) or []:
                v = road.get("vertexes") or []
                pts += [[round(v[k], 5), round(v[k + 1], 5)] for k in range(0, len(v) - 1, 2)]
        path_detail = [{"type": "CAR", "path": _thin(pts)}] if pts else []
        taxi = Leg("taxi", round(drive_min + p["taxi_overhead_min"]), taxi_fare + toll,
                   0.0, 0, dist_km, "kakao", path_detail)
        fuel = dist_km / p["car_km_per_l"] * p["fuel_won_per_l"]
        car = Leg("car", round(drive_min + p["car_overhead_min"]), int(round(fuel + toll, -1)),
                  0.0, 0, dist_km, "kakao", path_detail)
        base = f"{a.lat:.5f},{a.lng:.5f}>{b.lat:.5f},{b.lng:.5f}"
        self.cache[f"taxi:{base}"] = [taxi.__dict__]
        self.cache[f"car:{base}"] = [car.__dict__]
        return [taxi] if mode == "taxi" else [car]

    def _transit(self, a, b) -> list[Leg]:
        self.calls["transit"] += 1
        data = self._get(self.TRANSIT_URL, {
            "start_x": a.lng, "start_y": a.lat, "end_x": b.lng, "end_y": b.lat,
            "input_coord": "WGS84", "output_coord": "WGS84",
        })
        if data.get("status") != "OK":
            return []
        out, bad = [], []
        for r in data.get("routes", [])[:5]:
            try:
                out.append(self._parse_transit_route(r, a, b))
            except Exception as e:   # 경로 하나가 이상해도 나머지 후보는 살림
                bad.append(f"{type(e).__name__}: {e}")
        if bad:
            self._dump_debug("transit", a, b, data, bad)
        if not out and bad:
            raise RuntimeError(f"대중교통 응답 해석 실패 ({bad[0]})")
        return out

    def _parse_transit_route(self, r, a, b) -> Leg:
        pr = r.get("properties", r)
        steps = r.get("steps", []) or []
        sp = lambda s: s.get("properties", s)
        total_min = pr["totalTime"] / 60
        dist_km = (pr.get("totalDistance") or 0) / 1000
        walk_m = sum(sp(s).get("distance", 0) or 0 for s in steps if sp(s).get("type") == "WALKING")

        # 카카오 대중교통 API는 첫 승차 전/마지막 하차 후 도보 구간을 주지 않음
        # -> 첫 탑승 정류장, 마지막 하차 정류장 좌표로 직선거리 추정해서 더함
        ride = [s for s in steps if sp(s).get("type") != "WALKING"
                and (s.get("path") or {}).get("points")]
        access_km = 0.0
        if ride:
            x0, y0 = ride[0]["path"]["points"][0]
            x1, y1 = ride[-1]["path"]["points"][-1]
            access_km = (_hav(a.lat, a.lng, y0, x0) + _hav(y1, x1, b.lat, b.lng)) * config.EST["detour"]
        access_min = access_km / config.EST["walk_kmh"] * 60

        detail = []
        for st in steps:
            q = sp(st)
            kind = q.get("type", "")
            stops = [x.get("name", "") for x in (q.get("stops") or []) if isinstance(x, dict)]
            lines = [v.get("name", "") for v in (q.get("vehicles") or []) if isinstance(v, dict)]
            pts = (st.get("path") or {}).get("points") or []
            detail.append({
                "type": kind,
                "lines": [x for x in lines if x],          # 탈 수 있는 노선들 (예: 버스 여러 대)
                "from": stops[0] if stops else "",
                "to": stops[-1] if stops else "",
                "stops": max(len(stops) - 1, 0),           # 몇 정거장 가는지
                "minutes": round((q.get("time") or 0) / 60),
                "guidance": q.get("guidance", ""),
                "path": [[round(x, 5), round(y, 5)] for x, y in pts],   # 지도에 그릴 좌표
            })
        return Leg("transit", round(total_min + access_min), self._fare(pr.get("fare"), dist_km),
                   walk_m / 1000 + access_km, int(pr.get("transfers") or 0), dist_km, "kakao",
                   detail)

    @staticmethod
    def _fare(f, dist_km) -> int:
        """요금 필드가 경로마다 다르게 올 수 있어서 방어적으로 읽음.
        value -> min/max -> 숫자 그대로 -> (없으면) 거리 기반 추정 요금 순서."""
        if isinstance(f, (int, float)):
            return int(f)
        if isinstance(f, dict):
            for k in ("value", "card", "total", "min", "max"):
                v = f.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    return int(v)
        p = config.EST
        extra = max(0.0, dist_km - p["transit_base_km"])
        return int(p["transit_base_fare"] + math.ceil(extra / 5) * p["transit_extra_per_5km"])

    def _dump_debug(self, mode, a, b, data, errors):
        """해석 못 한 응답 원본을 저장 (처음 한 번만). 이 파일을 보면 실제 응답 구조를 알 수 있음."""
        path = f"kakao_debug_{mode}.json"
        if os.path.exists(path):
            return
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"from": a.name, "to": b.name, "errors": errors, "response": data},
                      fp, ensure_ascii=False, indent=1)
        print(f"[KakaoProvider] 해석 못 한 {mode} 응답 원본을 {path} 에 저장했습니다.")

    def _walk(self, a, b) -> list[Leg]:
        self.calls["walk"] += 1
        data = self._get(self.WALK_URL, {
            "start_x": a.lng, "start_y": a.lat, "end_x": b.lng, "end_y": b.lat,
            "input_coord": "WGS84", "output_coord": "WGS84",
        })
        if data.get("status") != "OK":
            return []
        pr = data["route"]["properties"]
        km = pr["totalDistance"] / 1000
        pts = []
        for lg in data["route"].get("legs", []) or []:
            for st in lg.get("steps", []) or []:
                pts += [[round(x, 5), round(y, 5)] for x, y in ((st.get("path") or {}).get("points") or [])]
        detail = [{"type": "WALKING", "path": _thin(pts)}] if pts else []
        return [Leg("walk", round(pr["totalTime"] / 60), 0, km, 0, km, "kakao", detail)]


def kakao_key_available() -> bool:
    """카카오 REST API 키가 config.py 또는 환경변수에 있는지."""
    return bool(config.KAKAO_REST_API_KEY.strip() or os.environ.get("KAKAO_REST_API_KEY"))


NO_KEY_MESSAGE = ("카카오 REST API 키가 없습니다. route_planner/config.py 의 KAKAO_REST_API_KEY 에 키를 넣거나 "
                  "환경변수 KAKAO_REST_API_KEY 를 설정하세요. API 없이는 실행하지 않습니다.")
