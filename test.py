"""
suwon_restaurants.py : 수원 식당 중 별점 N점 이상인 곳을 구글 Places API (New)로 뽑는 스크립트

사용법
  1) API_KEY에 키를 붙여 넣는다 (또는 환경변수 GOOGLE_PLACES_API_KEY).
  2) pip install requests
  3) python suwon_restaurants.py
     python suwon_restaurants.py --min-rating 4.3 --min-reviews 100
     python suwon_restaurants.py --dry-run        # 호출 없이 예상 요청 수만 출력
     python suwon_restaurants.py --refresh        # 캐시 무시하고 다시 호출

결과
  suwon_restaurants.csv   : 별점 높은 순 목록 (엑셀용)
  suwon_restaurants.json  : 영업시간 등 전체 정보
  suwon_cache.json        : 호출 캐시 (같은 검색은 다시 호출하지 않음)

동작 방식
  - 구글 검색은 질의 1개당 최대 60곳(20곳 x 3페이지)까지만 준다.
    그래서 "구/동네 x 음식 종류" 조합으로 여러 번 검색해서 합치고 place_id로 중복을 제거한다.
  - 리뷰(reviews)는 가장 비싼 등급이라 요청하지 않는다. 평점/리뷰 수/영업시간은 Enterprise 등급.
  - 요청 수 = 질의 수 x 페이지 수. 월 무료 사용량(Enterprise 약 1,000건)을 넘지 않게 --dry-run으로 먼저 확인.
"""

import csv
import itertools
import json
import os
import sys
import time

import requests

# ───────────────────────── 설정 ─────────────────────────
API_KEY = "AIzaSyCmLHNhVBBk9HPxlKniRlNSI-waVDifY-I"  # ← 여기에 Google API 키를 붙여 넣으세요

MIN_RATING = 4.0       # 이 별점 이상만
MIN_REVIEWS = 30       # 리뷰 수가 너무 적은 곳(5.0점인데 리뷰 2개 등) 제외. 0이면 제한 없음
MAX_PAGES = 3          # 질의당 페이지 수 (페이지당 20곳, 구글 한도상 최대 3)

AREAS = ["장안구", "권선구", "팔달구", "영통구"]
KINDS = ["맛집", "한식", "중식", "일식", "양식", "고기집", "분식"]

# 수원시 대략적인 경계 (이 안쪽 결과만 받음). 용인·화성 쪽 결과가 섞이는 걸 줄여 준다.
SUWON_BOX = {"low": (37.20, 126.92), "high": (37.33, 127.10)}
ADDRESS_MUST_CONTAIN = "수원"

LANGUAGE = "ko"
REGION = "KR"
CACHE_FILE = "suwon_cache.json"
CSV_FILE = "suwon_restaurants.csv"
JSON_FILE = "suwon_restaurants.json"
# ────────────────────────────────────────────────────────

URL = "https://places.googleapis.com/v1/places:searchText"
DAY_KO = ["일", "월", "화", "수", "목", "금", "토"]

FIELDS = [
    "places.id", "places.displayName", "places.formattedAddress", "places.location",
    "places.primaryTypeDisplayName", "places.businessStatus", "places.googleMapsUri",
    "places.rating", "places.userRatingCount", "places.priceLevel",
    "places.regularOpeningHours", "places.websiteUri", "places.nationalPhoneNumber",
    "nextPageToken",
]
FIELD_MASK = ",".join(FIELDS)


def search_page(query, api_key, page_token=None):
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {
        "textQuery": query,
        "includedType": "restaurant",
        "strictTypeFiltering": True,
        "languageCode": LANGUAGE,
        "regionCode": REGION,
        "pageSize": 20,
        "locationRestriction": {"rectangle": {
            "low": {"latitude": SUWON_BOX["low"][0], "longitude": SUWON_BOX["low"][1]},
            "high": {"latitude": SUWON_BOX["high"][0], "longitude": SUWON_BOX["high"][1]},
        }},
    }
    if page_token:
        body["pageToken"] = page_token
    r = requests.post(URL, headers=headers, json=body, timeout=20)
    if r.status_code != 200:
        error_exit(r)
    return r.json()


def error_exit(r):
    try:
        msg = r.json().get("error", {}).get("message", r.text)
    except ValueError:
        msg = r.text
    hint = ""
    if r.status_code == 400 and "API key" in msg:
        hint = "API 키가 잘못됐습니다."
    elif r.status_code == 403:
        hint = ("Places API (New)가 이 키의 프로젝트에서 켜져 있는지, 결제 계정이 연결됐는지, "
                "키의 API 제한에 Places API (New)가 포함됐는지 확인하세요.")
    elif r.status_code == 429:
        hint = "호출 한도 초과입니다. 잠시 후 다시 시도하세요."
    sys.exit(f"[오류 {r.status_code}] {msg}\n{hint}")


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def fetch_query(query, api_key, cache, refresh):
    """한 질의를 최대 MAX_PAGES 페이지까지 받아 장소 리스트로 반환. (places, 실제 호출 수)"""
    key = f"{query}|pages={MAX_PAGES}"
    if key in cache and not refresh:
        return cache[key], 0
    places, token, calls = [], None, 0
    for _ in range(MAX_PAGES):
        data = search_page(query, api_key, token)
        calls += 1
        places += data.get("places", [])
        token = data.get("nextPageToken")
        if not token:
            break
        time.sleep(0.3)
    cache[key] = places
    save_cache(cache)
    return places, calls


def hours_text(p):
    lines = p.get("regularOpeningHours", {}).get("weekdayDescriptions", [])
    return " / ".join(lines)


def to_row(p):
    return {
        "place_id": p.get("id"),
        "name": p.get("displayName", {}).get("text"),
        "type": p.get("primaryTypeDisplayName", {}).get("text"),
        "rating": p.get("rating"),
        "rating_count": p.get("userRatingCount"),
        "price_level": p.get("priceLevel"),
        "address": p.get("formattedAddress"),
        "lat": p.get("location", {}).get("latitude"),
        "lng": p.get("location", {}).get("longitude"),
        "status": p.get("businessStatus"),
        "hours": hours_text(p),
        "has_hours": bool(p.get("regularOpeningHours")),
        "phone": p.get("nationalPhoneNumber"),
        "website": p.get("websiteUri"),
        "maps_url": p.get("googleMapsUri"),
    }


def keep(p, min_rating, min_reviews):
    """필터: 수원 주소, 영업 중, 별점·리뷰 수 기준."""
    if ADDRESS_MUST_CONTAIN not in (p.get("formattedAddress") or ""):
        return False
    if p.get("businessStatus") not in (None, "OPERATIONAL"):
        return False
    rating = p.get("rating")
    if rating is None or rating < min_rating:
        return False
    return (p.get("userRatingCount") or 0) >= min_reviews


def collect(raw_lists, min_rating, min_reviews):
    """여러 질의 결과를 합쳐 중복 제거 + 필터 + 정렬. (결과 행, 중복 제거 후 전체 수)"""
    unique = {}
    for places in raw_lists:
        for p in places:
            unique.setdefault(p.get("id"), p)
    rows = [to_row(p) for p in unique.values() if keep(p, min_rating, min_reviews)]
    rows.sort(key=lambda r: (-(r["rating"] or 0), -(r["rating_count"] or 0)))
    return rows, len(unique)


def write_outputs(rows):
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    cols = ["name", "type", "rating", "rating_count", "price_level", "address",
            "hours", "phone", "maps_url", "lat", "lng", "place_id"]
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:  # 엑셀 한글 깨짐 방지
        w = csv.writer(f)
        w.writerow(["식당명", "종류", "별점", "리뷰수", "가격대", "주소",
                    "영업시간", "전화", "구글지도", "위도", "경도", "place_id"])
        for r in rows:
            w.writerow([r[c] for c in cols])


def parse_args(argv):
    opts = {"min_rating": MIN_RATING, "min_reviews": MIN_REVIEWS,
            "dry_run": "--dry-run" in argv, "refresh": "--refresh" in argv}
    for i, a in enumerate(argv):
        if a == "--min-rating" and i + 1 < len(argv):
            opts["min_rating"] = float(argv[i + 1])
        if a == "--min-reviews" and i + 1 < len(argv):
            opts["min_reviews"] = int(argv[i + 1])
    return opts


def main():
    opts = parse_args(sys.argv[1:])
    queries = [f"수원 {a} {k}" for a, k in itertools.product(AREAS, KINDS)]
    max_requests = len(queries) * MAX_PAGES
    print(f"질의 {len(queries)}개 x 최대 {MAX_PAGES}페이지 = 최대 {max_requests}회 요청 "
          f"(조건: 별점 {opts['min_rating']}+, 리뷰 {opts['min_reviews']}+)")
    if opts["dry_run"]:
        print("--dry-run: 호출하지 않고 종료합니다.")
        return

    api_key = API_KEY or os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        sys.exit("API 키가 없습니다. 파일 위쪽 API_KEY = \"\" 에 키를 넣어 주세요.")

    cache, raw_lists, total_calls = load_cache(), [], 0
    for i, q in enumerate(queries, 1):
        places, calls = fetch_query(q, api_key, cache, opts["refresh"])
        total_calls += calls
        raw_lists.append(places)
        print(f"[{i}/{len(queries)}] {q}: {len(places)}곳 (호출 {calls}회)")

    rows, unique_n = collect(raw_lists, opts["min_rating"], opts["min_reviews"])
    write_outputs(rows)

    print(f"\n완료: 중복 제거 후 {unique_n}곳 중 조건 통과 {len(rows)}곳, 이번 API 호출 {total_calls}회")
    for r in rows[:10]:
        print(f"  ★{r['rating']} ({r['rating_count']}) {r['name']}  {r['address']}")
    if len(rows) > 10:
        print(f"  ... 외 {len(rows) - 10}곳")
    print(f"저장: {CSV_FILE}, {JSON_FILE}")


if __name__ == "__main__":
    main()