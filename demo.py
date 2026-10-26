"""서울 샘플 데모.

python demo.py              # 카카오 API로 실제 경로 조회 (키가 없으면 안내 문구를 출력하고 종료)
"""
import json
import sys
import time
from dataclasses import replace

from route_planner import (KakaoProvider, Location, Place, TripRequest, NO_KEY_MESSAGE,
                           build_candidates, config, kakao_key_available, plan)

places = [
    Place("경복궁", 37.5796, 126.9770, "attraction"),
    Place("북촌한옥마을", 37.5826, 126.9831, "attraction"),
    Place("광장시장", 37.5701, 126.9996, "restaurant"),
    Place("익선동 카페", 37.5740, 126.9895, "cafe"),
    Place("명동교자", 37.5626, 126.9856, "restaurant"),
    Place("N서울타워", 37.5512, 126.9882, "attraction", open_time="10:00", close_time="23:00", fee=21000),
]

req = TripRequest(
    places=places,
    origin=Location("서울역", 37.5547, 126.9707),
    destination=Location("홍대입구역", 37.5572, 126.9245),
    start_time="09:00",
    end_time="22:00",
    budget=60000,
)

if not kakao_key_available():
    sys.exit(NO_KEY_MESSAGE)
provider = KakaoProvider()
print("카카오 API로 실제 경로를 조회합니다.")

cands = build_candidates(req, provider)   # API 호출은 여기서 한 번만

print("=" * 70)
print("1) 기준(프리셋)별 최적 경로 (정확해)")
print("=" * 70)
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
print("3) 18시에 끝나야 하는데 장소가 너무 많을 때 -> OR-Tools가 일부 장소를 빼서 가능한 일정 제시")
print("=" * 70)
short = replace(req, end_time="18:00")
print(plan(short, cands=build_candidates(short, provider), solver="ortools", time_limit_s=2).pretty())

if hasattr(provider, "report"):
    print("\n" + provider.report())

with open("sample_output.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\n프리셋별 결과 JSON -> sample_output.json")
