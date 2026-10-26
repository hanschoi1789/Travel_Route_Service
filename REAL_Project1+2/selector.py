"""후보 장소를 Google Places 정보로 확인하고 경로 계산에 넘길 집합을 정한다.

CLI: python selector.py --candidates data/candidate_places.csv --context data/trip_context.json
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

import requests

KST = timezone(timedelta(hours=9))
PRIOR_COUNT = 20
MAX_CANDIDATES = 30
MEAL_WINDOWS = {"lunch": (660, 870), "dinner": (1020, 1170)}
GOOGLE_FIELDS = (
    "id,displayName,formattedAddress,location,primaryType,types,businessStatus,"
    "googleMapsUri,rating,userRatingCount,regularOpeningHours,currentOpeningHours,"
    "utcOffsetMinutes,attributions"
)


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return clean(value.item())
    return value


def is_restaurant(row):
    category = str(row.get("카테고리") or "")
    if any(w in category for w in ("카페", "커피", "디저트", "베이커리", "제과", "술집", "주점", "호프", "칵테일", "와인바")):
        return False
    return any(w in category for w in ("음식점", "한식", "양식", "일식", "중식", "분식", "식당", "뷔페"))


def distance_m(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(lon2-lon1)/2)**2
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1, max(0, h))))


def _norm(text):
    return re.sub(r"[^가-힣a-z0-9]", "", str(text).lower())


def match_place(row, places):
    """이름·좌표·도로명 주소를 대조한다. 동점에 가까운 두 지점은 자동 연결하지 않는다."""
    matches = []
    original = _norm(row["장소명"])
    for place in places:
        loc = place.get("location", {})
        if not {"latitude", "longitude"} <= loc.keys():
            continue
        meters = distance_m(float(row["위도"]), float(row["경도"]), loc["latitude"], loc["longitude"])
        name = _norm(place.get("displayName", {}).get("text", ""))
        # 건물 내부 시설의 이름을 건물 전체 이름으로 줄여 연결하지 않는다.
        facility_terms = ("도서관", "별관", "본관", "배움터", "주차장", "매표소", "체험관", "교육관", "기념품", "안내소")
        if any((term in original) != (term in name) for term in facility_terms):
            continue
        primary = place.get("primaryType", "")
        types = set(place.get("types", []))
        category = str(row.get("카테고리", ""))
        if any(w in category for w in ("카페", "커피")) and types and not types.intersection({"cafe", "coffee_shop", "dessert_shop", "tea_house"}):
            continue
        google_food = primary in {"restaurant", "cafe", "coffee_shop", "bakery", "bar", "meal_takeaway"} or primary.endswith("_restaurant")
        if "음식점" not in category and google_food:
            continue
        similarity = SequenceMatcher(None, original, name).ratio()
        exact = original == name and bool(name)
        # 시/구 명칭 차이를 제외한 도로명+건물번호 일치도만 보조 근거로 쓴다.
        road = re.search(r"(\S+(?:로|길))\s+(\d+(?:-\d+)?)", str(row.get("주소", "")))
        address_match = bool(road and _norm(road.group(0)) in _norm(place.get("formattedAddress", "")))
        if is_restaurant(row) and place.get("primaryType") in {"cafe", "coffee_shop", "bakery", "bar"}:
            continue
        accepted = (exact and meters <= 450) or (similarity >= .82 and meters <= 180) or (similarity >= .68 and address_match and meters <= 250)
        if not accepted:
            continue
        confidence = similarity + (.15 if address_match else 0) - min(meters/1000, .45)
        matches.append((confidence, place, round(meters, 1), round(similarity, 3)))
    matches.sort(key=lambda x: (-x[0], x[1].get("id", "")))
    if not matches:
        return None, {"status": "unmatched", "reason": "이름과 위치가 일치하는 Google 장소를 찾지 못함"}
    if len(matches) > 1 and matches[0][0] - matches[1][0] < .08:
        return None, {"status": "ambiguous", "reason": "비슷한 지점이 여러 개여서 자동 연결을 보류함"}
    _, place, meters, similarity = matches[0]
    return place, {"status": "matched", "distance_m": meters, "name_similarity": similarity}


class GooglePlaces:
    """응답은 이번 실행의 메모리에서만 재사용한다. 디스크 API 캐시는 만들지 않는다."""
    def __init__(self, key):
        if not key:
            raise ValueError(".streamlit/secrets.toml에 GOOGLE_PLACES_API_KEY를 설정해 주세요.")
        self.key = key
        self.calls = 0
        self.memory = {}

    def request(self, method, path, mask, **kwargs):
        self.calls += 1
        try:
            response = requests.request(method, "https://places.googleapis.com/v1/" + path,
                headers={"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": mask}, timeout=20, **kwargs)
        except requests.RequestException as error:
            raise RuntimeError("Google Places 연결에 실패했습니다. 네트워크를 확인한 뒤 다시 실행해 주세요.") from error
        if response.status_code != 200:
            hints = {400: "API 키 또는 요청 형식을 확인해 주세요.", 401: "API 키를 확인해 주세요.",
                     403: "Places API (New) 활성화, 결제 연결, 키의 API/IP 제한을 확인해 주세요.",
                     429: "사용 한도에 도달했습니다. Google Cloud 할당량을 확인해 주세요."}
            raise RuntimeError(f"Google Places HTTP {response.status_code}: " + hints.get(response.status_code, "잠시 후 다시 실행해 주세요."))
        return response.json()

    def find(self, row):
        token = (str(row["place_id"]), str(row["장소명"]), float(row["위도"]), float(row["경도"]))
        if token not in self.memory:
            payload = {"textQuery": f"{row['장소명']} {row.get('주소', '')}", "languageCode": "ko", "regionCode": "KR", "pageSize": 3,
                       "locationBias": {"circle": {"center": {"latitude": float(row["위도"]), "longitude": float(row["경도"])}, "radius": 500.0}}}
            data = self.request("POST", "places:searchText", ",".join("places." + f for f in GOOGLE_FIELDS.split(",")), json=payload)
            self.memory[token] = match_place(row, data.get("places", []))
        return self.memory[token]

    def reviews(self, google_id):
        return self.request("GET", "places/" + quote(google_id, safe=""), "reviews,attributions", params={"languageCode": "ko"})


def _point_date(point):
    d = point.get("date")
    return date(d["year"], d["month"], d["day"]) if d else None


def _minute(point):
    return int(point.get("hour", 0))*60 + int(point.get("minute", 0))


def merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def opening_windows(place, trip_day, fetched_day):
    """한국 현지 날짜의 구간(분). None=미제공, []=해당 날짜 닫힘. 자정·분할 영업 처리."""
    trip_day = date.fromisoformat(str(trip_day))
    fetched_day = date.fromisoformat(str(fetched_day))
    if place.get("utcOffsetMinutes", 540) != 540:
        return None, "unsupported_timezone"
    current = 0 <= (trip_day-fetched_day).days <= 6 and "currentOpeningHours" in place
    field = "currentOpeningHours" if current else "regularOpeningHours"
    hours = place.get(field)
    if hours is None:
        return None, "unknown"
    periods = hours.get("periods")
    if periods is None:
        # protobuf JSON은 빈 반복 필드를 생략할 수 있다. 빈 hours는 공식적으로 닫힘을 뜻한다.
        if not hours or hours.get("weekdayDescriptions"):
            return [], field
        return None, "unknown"
    if not periods:
        return [], field
    intervals = []
    for period in periods:
        opened, closed = period.get("open", {}), period.get("close")
        if not closed:
            if opened.get("day", 0) == 0 and _minute(opened) == 0:
                return [[0, 1440]], field
            return None, "unknown"
        if current and _point_date(opened) and _point_date(closed):
            start = (_point_date(opened)-trip_day).days*1440 + _minute(opened)
            end = (_point_date(closed)-trip_day).days*1440 + _minute(closed)
            if closed.get("truncated") and _minute(closed) == 1439:
                end += 1
            intervals.append([max(0, start), min(1440, end)])
        else:
            weekday = (trip_day.weekday()+1) % 7
            opened_week = int(opened.get("day", 0))*1440 + _minute(opened)
            closed_week = int(closed.get("day", 0))*1440 + _minute(closed)
            if closed_week <= opened_week:
                closed_week += 10080
            for shift in (-10080, 0, 10080):
                start, end = opened_week+shift-weekday*1440, closed_week+shift-weekday*1440
                intervals.append([max(0, start), min(1440, end)])
    return merge_intervals(intervals), field


def parse_time(value):
    text = str(value)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", text):
        raise ValueError("시각은 HH:MM 형식이어야 합니다.")
    parts = text.split(":")
    return int(parts[0])*60 + int(parts[1])


def feasible_starts(windows, start, end, duration):
    """[최소 도착분, 최대 도착분]. 도착+체류가 같은 영업 구간 안에 있어야 한다."""
    if windows is None:
        return None
    return [[max(a, start), min(b, end)-duration] for a, b in windows if max(a, start)+duration <= min(b, end)]


def validate_context(rows, context):
    if not rows or len(rows) > MAX_CANDIDATES:
        raise ValueError(f"후보는 1~{MAX_CANDIDATES}곳이어야 합니다.")
    if len({str(r['place_id']) for r in rows}) != len(rows):
        raise ValueError("후보 place_id가 중복됩니다.")
    target = context.get("target_count")
    if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= 30:
        raise ValueError("target_count는 1~30 사이 정수여야 합니다.")
    if str(context.get("main_place_id")) not in {str(r["place_id"]) for r in rows}:
        raise ValueError("여행조건의 메인 장소가 후보에 없습니다.")
    date.fromisoformat(str(context["trip_date"]))
    start, end = parse_time(context["start_time"]), parse_time(context["end_time"])
    if not 0 <= start < end <= 1440:
        raise ValueError("같은 날의 시작·종료 시간을 확인해 주세요.")
    stay = context.get("average_stay_minutes", 90)
    meal = context.get("meal_stay_minutes", 60)
    if not isinstance(stay, int) or not 1 <= stay <= 720 or not isinstance(meal, int) or not 1 <= meal <= 240:
        raise ValueError("체류시간을 확인해 주세요.")
    if any(max(start, a)+meal > min(end, b) for a, b in MEAL_WINDOWS.values()):
        raise ValueError("점심·저녁 식당 2곳을 포함하려면 두 식사 시간대가 일정 안에 있어야 합니다. 점심 11:00~14:30, 저녁 17:00~19:30, 식사 60분 기준입니다.")
    for row in rows:
        lat, lon = float(row["위도"]), float(row["경도"])
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("후보 좌표를 확인해 주세요.")


def enrich_candidates(rows, context, client, fetched_day=None):
    validate_context(rows, context)
    today = fetched_day or datetime.now(KST).date().isoformat()
    start, end = parse_time(context["start_time"]), parse_time(context["end_time"])
    enriched = []
    for original in rows:
        row = clean(dict(original))
        row["place_id"] = str(row["place_id"])
        row["위도"], row["경도"] = float(row["위도"]), float(row["경도"])
        row["input_mandatory"] = row.get("필수여부") == "필수" or row.get("사용자선택") == "필수" or row["place_id"] == str(context["main_place_id"])
        row["is_restaurant"] = is_restaurant(row)
        row["meal_supplement"] = row["place_id"] in {str(i) for i in context.get("restaurant_candidates_added", [])}
        row["stay_minutes"] = context.get("meal_stay_minutes", 60) if row["is_restaurant"] else context.get("average_stay_minutes", 90)
        row.update(google_place_id=None, google_rating=None, google_rating_count=0, adjusted_rating=None,
                   opening_intervals=None, visit_start_windows=None, meal_start_windows={}, hours_source="unknown",
                   match_status="not_requested", exclusion_reason="", fetched_at=datetime.now(KST).isoformat())
        if row.get("사용자선택") == "제외":
            row["exclusion_reason"] = "사용자가 제외한 장소"
            enriched.append(row)
            continue
        place, evidence = client.find(row)
        row["match_status"] = evidence["status"]
        row["match_evidence"] = evidence
        if place is None:
            row["exclusion_reason"] = evidence["reason"]
            enriched.append(row)
            continue
        row.update(google_place_id=place["id"], google_name=place.get("displayName", {}).get("text"),
                   google_maps_url=place.get("googleMapsUri", ""), google_rating=place.get("rating"),
                   google_rating_count=int(place.get("userRatingCount", 0)), business_status=place.get("businessStatus", "UNKNOWN"),
                   attributions=place.get("attributions", []))
        if row["business_status"] in {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY", "FUTURE_OPENING"}:
            row["exclusion_reason"] = "폐업·일시 휴업 또는 개업 전으로 표시된 장소"
        windows, source = opening_windows(place, context["trip_date"], today)
        row["opening_intervals"], row["hours_source"] = windows, source
        row["visit_start_windows"] = feasible_starts(windows, start, end, row["stay_minutes"])
        if row["visit_start_windows"] == []:
            row["exclusion_reason"] = row["exclusion_reason"] or "여행 시간 안에 체류 가능한 영업 구간이 없음"
        if row["is_restaurant"]:
            row["meal_start_windows"] = {slot: feasible_starts(windows, max(start, a), min(end, b), row["stay_minutes"]) or [] for slot, (a, b) in MEAL_WINDOWS.items()}
        enriched.append(row)
    # 동일 Google 장소로 연결된 후보는 한 번만 사용한다. 필수 후보를 우선 보존한다.
    seen = set()
    for row in sorted(enriched, key=lambda r: (not r["input_mandatory"], r.get("match_evidence", {}).get("distance_m", float("inf")), r["place_id"])):
        gid = row.get("google_place_id")
        if gid and not row["exclusion_reason"]:
            if gid in seen:
                row["exclusion_reason"] = "다른 후보와 같은 Google 장소로 연결됨"
            seen.add(gid)
    return enriched


def choose_places(enriched, context):
    """필수 장소와 점심/저녁 식당 쌍을 고정하고 나머지는 동적 계획법으로 고른다.

    우선순위: 시간 확인 장소 수 → 평점 있는 장소 수 → 보정 평점 합계.
    식당 쌍을 모두 비교하고 (개수, 체류시간)별 최선 집합을 보존한다.
    이동 순서는 계산하지 않는다.
    """
    rows = [dict(r) for r in enriched]
    mandatory = [r for r in rows if r["input_mandatory"]]
    invalid = [r for r in mandatory if r["exclusion_reason"]]
    if invalid:
        raise ValueError("필수 장소 확인이 필요합니다: " + "; ".join(f"{r['장소명']} — {r['exclusion_reason']}" for r in invalid))
    eligible = [r for r in rows if not r["exclusion_reason"]]
    rated = [r for r in eligible if isinstance(r["google_rating"], (int, float)) and r["google_rating_count"] > 0]
    prior = sum(r["google_rating"] for r in rated)/len(rated) if rated else None
    for row in eligible:
        if row in rated:
            n = row["google_rating_count"]
            row["adjusted_rating"] = (n*row["google_rating"] + PRIOR_COUNT*prior)/(n+PRIOR_COUNT)
    def quality(row):
        return (int(row["opening_intervals"] is not None), int(row["adjusted_rating"] is not None), row["adjusted_rating"] or 0, row["google_rating_count"])
    ranked = sorted(eligible, key=lambda r: (tuple(-v for v in quality(r)), r["place_id"]))
    lunch = [r for r in eligible if r["meal_start_windows"].get("lunch")]
    dinner = [r for r in eligible if r["meal_start_windows"].get("dinner")]
    best, best_key, pair_count = None, None, 0
    for a in lunch:
        for b in dinner:
            if a["place_id"] == b["place_id"]:
                continue
            fixed = {r["place_id"]: r for r in mandatory + [a, b]}
            if len(fixed) > context["target_count"]:
                continue
            capacity = parse_time(context["end_time"])-parse_time(context["start_time"])-sum(r["stay_minutes"] for r in fixed.values())
            needed = context["target_count"]-len(fixed)
            if capacity < 0:
                continue
            states = {(0, 0): ((0, 0, 0, 0), [])}
            for row in ranked:
                if row["place_id"] in fixed or row.get("meal_supplement", False):
                    continue
                for (count, spent), (score, members) in list(states.items()):
                    new_spent = spent + row["stay_minutes"]
                    if count >= needed or new_spent > capacity:
                        continue
                    new_score = tuple(score[i]+quality(row)[i] for i in range(4))
                    state_key = (count+1, new_spent)
                    same_score_better_ids = state_key in states and new_score == states[state_key][0] and tuple(sorted(r["place_id"] for r in members+[row])) < tuple(sorted(r["place_id"] for r in states[state_key][1]))
                    if state_key not in states or new_score > states[state_key][0] or same_score_better_ids:
                        states[state_key] = (new_score, members+[row])
            completed = [state for (count, spent), state in states.items() if count == needed]
            if not completed:
                continue
            _, extras = min(completed, key=lambda state: (tuple(-v for v in state[0]), tuple(sorted(r["place_id"] for r in state[1]))))
            chosen = list(fixed.values()) + extras
            pair_count += 1
            key = tuple(sum(quality(r)[i] for r in chosen) for i in range(4))
            tie = tuple(sorted(r["place_id"] for r in chosen)) + (a["place_id"], b["place_id"])
            if best is None or key > best_key or (key == best_key and tie < best[2]):
                best, best_key = (chosen, (a["place_id"], b["place_id"]), tie), key
    if best is None:
        raise ValueError("조건에 맞는 최종 장소를 만들지 못했습니다. 서로 다른 점심·저녁 식당의 영업시간, 총 장소 수, 체류시간을 확인해 주세요. 반경을 넓히거나 날짜·시간을 바꾸면 후보를 다시 찾습니다.")
    selected, pair, _ = best
    selected_ids = {r["place_id"] for r in selected}
    for row in rows:
        row["selected"] = row["place_id"] in selected_ids
        row["meal_required"] = row["place_id"] in pair
        row["mandatory"] = row["input_mandatory"] or row["meal_required"]
        row["part2_mandatory"] = row["mandatory"]
        row["part2_meal_required"] = row["meal_required"]
        row["meal_options"] = [s for s, w in row["meal_start_windows"].items() if w]
        if row["selected"]:
            reason = "메인·사용자 필수 장소" if row["input_mandatory"] else "평가 근거 우선순위에 따라 선정"
            if row["meal_required"]:
                reason += "; 점심·저녁을 각각 배치할 수 있는 서로 다른 식당 2곳 조건 충족"
            reason += "; " + ("영업시간 미제공: 방문 전 확인 필요" if row["opening_intervals"] is None else "여행 시간 내 체류 가능한 영업 구간 확인")
            row["필수여부"] = "필수" if row["mandatory"] else "선정"
            row["사용자선택"] = "필수" if row["mandatory"] else "포함"
        else:
            reason = row["exclusion_reason"] or "필수·식사 조건을 만족하는 집합에서 평가 근거 우선순위가 낮음"
        row["selection_reason"] = reason
    selected = sorted([r for r in rows if r["selected"]], key=lambda r: (r["place_id"] != str(context["main_place_id"]), not r["mandatory"], -float(r["adjusted_rating"] or 0), r["place_id"]))
    return {"schema_version": "2.0", "created_at": datetime.now(KST).isoformat(), "context": context,
            "selected": selected, "not_selected": [r for r in rows if not r["selected"]],
            "summary": {"input_count": len(rows), "matched_count": sum(r["match_status"] == "matched" for r in rows),
                        "selected_count": len(selected), "pairs_evaluated": pair_count, "prior_mean": prior, "prior_count": PRIOR_COUNT,
                        "unknown_hours_count": sum(r["opening_intervals"] is None for r in selected)},
            "meal_assignment_example": {"lunch": pair[0], "dinner": pair[1]},
            "route_verified": False, "order_meaning": "표시 순서이며 방문 순서가 아님"}


def result_csv_bytes(rows):
    out = io.StringIO(newline="")
    columns = list(dict.fromkeys(k for row in rows for k in row)) or ["place_id", "장소명", "selection_reason"]
    writer = csv.DictWriter(out, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        data = {}
        for k, v in clean(row).items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")):
                v = "'" + v
            data[k] = v
        writer.writerow(data)
    return out.getvalue().encode("utf-8-sig")


def route_payload(result):
    return {"schema_version": "2.0", "created_at": result["created_at"], "timezone": "Asia/Seoul",
            "trip": result["context"], "route_verified": False,
            "instructions": "장소 집합만 확정. 영업 구간에서 체류시간을 빼 도착 가능 구간을 사용하고 이동시간·식사 배치·전체 일정 가능성을 검증한다. unknown을 24시간 영업으로 바꾸지 않는다.",
            "meal_assignment_example": result["meal_assignment_example"],
            "places": [{"place_id": r["place_id"], "google_place_id": r["google_place_id"], "name": r["장소명"],
                        "lat": float(r["위도"]), "lng": float(r["경도"]), "coordinate_source": "kakao_local", "mandatory": r["mandatory"],
                        "meal_required": r["meal_required"], "meal_options": r["meal_options"], "stay_minutes": r["stay_minutes"],
                        "opening_intervals": r["opening_intervals"], "visit_start_windows": r["visit_start_windows"],
                        "meal_start_windows": r["meal_start_windows"], "hours_source": r["hours_source"],
                        "requires_hours_check": r["opening_intervals"] is None or r["hours_source"] != "currentOpeningHours", "selection_reason": r["selection_reason"]}
                       for r in result["selected"]]}


def save_result(result, output_root):
    """실행별 산출물을 분리해 실패한 실행을 이전 성공 결과로 오해하지 않도록 한다."""
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    folder = root / datetime.now(KST).strftime("%Y%m%d_%H%M%S_%f")
    folder.mkdir()
    (folder / "final_places.csv").write_bytes(result_csv_bytes(result["selected"]))
    (folder / "not_selected_places.csv").write_bytes(result_csv_bytes(result["not_selected"]))
    for filename, payload in (("route_input.json", route_payload(result)), ("selection_result.json", result)):
        (folder / filename).write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (root / "latest.json").write_text(json.dumps({"status": "success", "run_folder": folder.name, "created_at": result["created_at"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    return folder


def mark_pending(root, status="running", message=""):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "latest.json").write_text(json.dumps({"status": status, "message": message, "updated_at": datetime.now(KST).isoformat()}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_keys(root):
    import tomllib
    path = Path(root) / ".streamlit" / "secrets.toml"
    keys = tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    for key in ("KAKAO_REST_API_KEY", "GOOGLE_PLACES_API_KEY"):
        if os.environ.get(key):
            keys[key] = os.environ[key]
    return keys


# COMMAND LINE
if __name__ == "__main__":
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="후보 CSV의 Google 정보 조회 및 최종 장소 선정")
    parser.add_argument("--candidates", default="data/candidate_places.csv")
    parser.add_argument("--context", default="data/trip_context.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    mark_pending(root / "outputs")
    try:
        with (root / args.candidates).open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        context = json.loads((root / args.context).read_text(encoding="utf-8"))
        client = GooglePlaces(load_keys(root).get("GOOGLE_PLACES_API_KEY"))
        result = choose_places(enrich_candidates(rows, context, client), context)
        folder = save_result(result, root / "outputs")
        for row in result["selected"]:
            print(f"{row['장소명']} | {row['위도']}, {row['경도']} | {row['selection_reason']}")
        print(f"결과: {folder}\nGoogle 요청: {client.calls}회 · 방문 순서는 다음 단계에서 계산합니다.")
    except (ValueError, RuntimeError, OSError) as error:
        mark_pending(root / "outputs", "failed", str(error))
        print(f"선정 실패: {error}")
        raise SystemExit(1)
