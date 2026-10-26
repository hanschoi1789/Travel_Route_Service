"""수원 데모: 구글 Places로 뽑은 수원 식당 2곳 + 수원화성 관광지 3곳 (demo.py 와 같은 구성)

python suwon_demo.py   # 카카오 API로 실제 경로 조회 (키가 없으면 안내 문구를 출력하고 종료)
                       # 처음 보는 구간만 호출하고 kakao_cache.json 에 저장해 두므로 다시 돌리면 한도를 쓰지 않는다.

출력 구성 (demo.py 와 동일)
  1) 기준(프리셋)별 최적 경로 (정확해) - config.PRESETS 의 모든 프리셋, 가중치는 config.py 값 그대로
  2) 방문 순서를 정하는 방법 비교 (given / nearest / ortools / exact)
  3) 종료시각을 당겨 장소가 너무 많을 때 OR-Tools가 일부 장소를 빼는 경우
  결과 JSON -> suwon_output.json  (demo.py 의 sample_output.json 은 건드리지 않음)

식당의 영업시간은 suwon_cache.json(구글 응답 원본)에서 읽어 Place.open_time/close_time 에 넣는다.
관광지 좌표는 대략값이다. 정확한 좌표는 카카오 장소 검색 결과로 바꿔 쓰는 걸 권장.
"""
import json
import os
import sys
import time
from dataclasses import replace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)   # kakao_cache.json / suwon_cache.json 을 프로젝트 폴더에서 읽고 쓰도록

from route_planner import (KakaoProvider, Location, Place, TripRequest,  # noqa: E402
                           NO_KEY_MESSAGE, build_candidates, config, kakao_key_available, plan)

DAY = "토"   # 여행 요일 (구글 영업시간에서 이 요일 값을 읽는다)
DAY_KO = ["일", "월", "화", "수", "목", "금", "토"]
LUNCH_NAME, DINNER_NAME = "얼티튜드", "무월 수원행궁점"
SHORT_END = "18:00"   # 3)번에서 쓸 이른 종료시각


# ---------------------------------------------------------------------------
# 구글 영업시간 -> Place
# ---------------------------------------------------------------------------
def fmt_min(m):
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def day_hours(opening_hours, day_ko):
    """구글 regularOpeningHours.periods 에서 한 요일의 (open, close) 를 'HH:MM' 으로. 휴무/정보없음이면 None.
    하루에 영업 구간이 여러 개(브레이크타임)면 첫 오픈~마지막 마감으로 합친다.
    자정을 넘겨 끝나면 23:59 로 자른다 (모델이 하루 단위라서)."""
    if not opening_hours or "periods" not in opening_hours:
        return None
    spans = []
    for p in opening_hours["periods"]:
        o, c = p.get("open"), p.get("close")
        if not o or not c or DAY_KO[o["day"]] != day_ko:
            continue
        start = o.get("hour", 0) * 60 + o.get("minute", 0)
        end = c.get("hour", 0) * 60 + c.get("minute", 0) + ((c["day"] - o["day"]) % 7) * 1440
        spans.append((start, end))
    if not spans:
        return None
    spans.sort()
    return fmt_min(spans[0][0]), fmt_min(min(spans[-1][1], 24 * 60 - 1))


def load_restaurant(name):
    with open("suwon_cache.json", encoding="utf-8") as f:
        cache = json.load(f)
    for places in cache.values():
        for p in places:
            if p.get("displayName", {}).get("text") == name:
                h = day_hours(p.get("regularOpeningHours"), DAY)
                if h is None:
                    sys.exit(f"{name}: {DAY}요일 영업시간 정보가 없습니다.")
                return Place(name, p["location"]["latitude"], p["location"]["longitude"],
                             "restaurant", open_time=h[0], close_time=h[1])
    sys.exit(f"{name} 을(를) suwon_cache.json 에서 찾지 못했습니다.")


places = [
    load_restaurant(LUNCH_NAME),
    load_restaurant(DINNER_NAME),
    Place("화성행궁", 37.2819, 127.0142, "attraction"),
    Place("장안문", 37.2899, 127.0100, "attraction"),
    Place("팔달문", 37.2775, 127.0170, "attraction"),
]

req = TripRequest(
    places=places,
    origin=Location("수원역", 37.2661, 127.0001),
    destination=Location("수원역", 37.2661, 127.0001),
    start_time="10:00",
    end_time="21:00",
    budget=40000,
)

if not kakao_key_available():
    sys.exit(NO_KEY_MESSAGE)
provider = KakaoProvider()
print("카카오 API로 실제 경로를 조회합니다.")

print(f"\n[입력] {DAY}요일 {req.start_time}~{req.end_time}, {req.origin.name} 출발 -> "
      f"{req.destination.name} 도착, 예산 {req.budget:,}원")
for p in places:
    tag = "기본값" if p.hours_is_default else "구글"
    o, c = p.hours
    print(f"  {p.name:12s} {p.category:10s} 운영 {fmt_min(o)}~{fmt_min(c)}  ({tag})")

cands = build_candidates(req, provider)   # API 호출은 여기서 한 번만

print("\n" + "=" * 70)
print("1) 기준(프리셋)별 최적 경로 (정확해)")
print("=" * 70)
print("  가중치 (config.PRESETS 그대로)")
for name, w in config.PRESETS.items():
    print(f"    {name:9s} " + ", ".join(f"{k}={v}" for k, v in w.items()))
results = {}
for preset in config.PRESETS:
    r = plan(replace(req, preset=preset), cands=cands, solver="exact")
    results[preset] = r.to_dict()
    print(f"\n### {preset}")
    print(r.pretty())

print("\n" + "=" * 70)
print("2) 방문 순서를 정하는 방법 비교 (같은 장소·조건, balanced 기준)")
print("=" * 70)
print("""  given   : 입력한 순서 그대로 방문 (비교 기준)
  nearest : 매번 가장 빨리·싸게 갈 수 있는 곳부터 방문하는 간단한 방법
  ortools : Google OR-Tools로 순서를 최적화 (근사해, 장소가 9곳 이상일 때 기본)
  exact   : 방문 순서·이동수단·체류시간을 동시에 따지는 정확해 (장소 8곳 이하일 때 기본)

  점수     = 총 소요시간 + 비용·도보·환승·체류 단축을 분으로 환산한 값. 낮을수록 좋음.
  격차     = exact보다 점수가 몇 % 나쁜지.
  체류단축 = 시간 안에 넣으려고 체류시간을 줄인 합계.
""")
rows = []
for s in ("given", "nearest", "ortools", "exact"):
    t0 = time.time()
    r = plan(req, cands=cands, solver=s, time_limit_s=2)
    dt = time.time() - t0
    rows.append((s, r, dt))
best = next(r for s, r, _ in rows if s == "exact")
print(f"  {'방법':8s} {'점수':>7s} {'격차':>7s} {'체류단축':>6s} {'총소요':>6s} {'비용':>8s} {'도보':>6s} {'환승':>4s} {'계산':>6s}  방문 순서")
for s, r, dt in rows:
    if not r.ok or r.dropped:
        print(f"  {s:8s} {'해 없음':>7s}   (제약을 모두 지키는 일정을 못 찾음)")
        continue
    t = r.to_dict()["totals"]
    obj = r.schedule.objective
    gap = f"{(obj - best.schedule.objective) / best.schedule.objective * 100:.1f}%" if best.ok else "-"
    names = " → ".join(req.places[i].name for i in r.schedule.order)
    print(f"  {s:8s} {obj:7.1f} {gap:>7s} {t['stay_cut_min']:5d}분 {t['duration_min']:5d}분 "
          f"{t['cost_won']:7,}원 {t['walk_km']:4.1f}km {t['transfers']:3d}회 {dt:5.2f}s  {names}")

print("\n" + "=" * 70)
print(f"3) {SHORT_END}에 끝나야 하는데 장소가 너무 많을 때 -> OR-Tools가 일부 장소를 빼서 가능한 일정 제시")
print("=" * 70)
short = replace(req, end_time=SHORT_END)
print(plan(short, cands=build_candidates(short, provider), solver="ortools", time_limit_s=2).pretty())

print("\n" + provider.report())

with open("suwon_output.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\n프리셋별 결과 JSON -> suwon_output.json")
