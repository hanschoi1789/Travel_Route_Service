"""
프로젝트 1번 파트: 사용자 조건에 맞는 장소 후보 구성

처리 순서
1. 사용자에게 메인 여행지, 반경, 일정, 교통수단, 취향을 입력받는다.
2. 카카오 Local API에서 메인 여행지를 검색하고 사용자가 정확한 지점을 선택한다.
3. 선택한 좌표의 반경 안에서 취향별 장소 후보를 검색한다.
4. API 카테고리를 자연·맛집·문화·쇼핑·휴식·활동·실내 점수로 변환한다.
5. 사용자 취향과 장소 특성을 비교해 취향 적합도를 계산한다.
6. 학습 데이터가 충분하면 랜덤 포레스트 예상평가를 함께 사용한다.
7. 동일 종류만 몰리지 않도록 다양성을 적용해 후속 단계에 전달할 후보를 만든다.
"""

import os
import re
from pathlib import Path
APP_ROOT = Path(os.environ.get("TRAVEL_OUTPUT_ROOT", Path(__file__).resolve().parent))
from datetime import date, time, datetime, timezone

import numpy as np
import pandas as pd
import pydeck as pdk
import requests
import streamlit as st
from sklearn.ensemble import RandomForestRegressor


# ---------------------------------------------------------
# 1. 프로그램의 기본 설정과 공통 상수
# ---------------------------------------------------------

st.set_page_config(
    page_title="여행 장소 선정",
    page_icon="🧭",
    layout="wide",
)

st.title("🧭 여행 장소 선정")
st.write(
    "메인 여행지를 선택하면 설정한 반경 안에서 장소를 검색하고, "
    "취향에 맞는 후보의 평점과 영업시간을 확인해 방문 장소를 정합니다."
)

# 사용자 취향과 장소 특성에서 공통으로 사용하는 항목이다.
PREFERENCE_COLUMNS = ["자연", "맛집", "문화", "쇼핑", "휴식", "활동", "실내"]

# 카테고리 문자열을 장소 특성으로 바꿀 때 사용하는 기준표이다.
FEATURE_RULES = {
    "자연": ["공원", "산", "해수욕장", "수목원", "정원", "계곡", "호수", "자연명소", "숲"],
    "맛집": ["음식점", "한식", "양식", "일식", "중식", "분식", "시장", "베이커리", "디저트"],
    "문화": ["박물관", "미술관", "문화", "공연", "극장", "유적", "궁", "사찰", "관광명소", "전시"],
    "쇼핑": ["쇼핑", "백화점", "아울렛", "시장", "마트", "패션", "상가"],
    "휴식": ["카페", "공원", "정원", "도서관", "호텔", "스파", "휴양", "숲"],
    "활동": ["체험", "스포츠", "테마파크", "놀이시설", "전망대", "등산", "레저"],
    "실내": ["박물관", "미술관", "공연", "극장", "쇼핑", "백화점", "카페", "음식점", "도서관", "전시"],
}

# 동일 브랜드의 여러 지점이 결과를 차지하지 않도록 대표 체인명을 관리한다.
CHAIN_BRANDS = [
    "스타벅스", "투썸플레이스", "이디야", "메가커피", "컴포즈커피",
    "빽다방", "할리스", "파리바게뜨", "뚜레쥬르", "올리브영",
    "롯데리아", "맥도날드", "버거킹", "서브웨이",
]

# 사용자 평가 데이터가 저장될 경우 사용할 파일 이름이다.
# 다른 팀원이 수집한 training_data.csv를 이 폴더에 넣으면 자동으로 학습을 시도한다.
TRAINING_FILE = str(APP_ROOT / "training_data.csv")


# ---------------------------------------------------------
# 2. 카카오 REST API 키 읽기
# ---------------------------------------------------------

def get_kakao_key():
    """.streamlit/secrets.toml에 저장된 REST API 키를 안전하게 읽는다."""
    try:
        return st.secrets["KAKAO_REST_API_KEY"]
    except (KeyError, FileNotFoundError):
        return ""


KAKAO_KEY = get_kakao_key()

# 카카오 Local API는 Authorization 헤더에 'KakaoAK REST_API_KEY' 형식을 요구한다.
KAKAO_HEADERS = {"Authorization": f"KakaoAK {KAKAO_KEY}"}


# ---------------------------------------------------------
# 3. 카카오 API 호출 및 장소 검색 함수
# ---------------------------------------------------------

def kakao_api_get(url, params):
    """카카오 API에 GET 요청을 보내고 오류를 사용자가 이해할 수 있게 처리한다."""
    response = requests.get(
        url,
        headers=KAKAO_HEADERS,
        params=params,
        timeout=10,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "카카오 REST API 키가 없거나 잘못되었습니다. "
            ".streamlit/secrets.toml을 확인해 주세요."
        )

    if response.status_code == 403:
        message = str(response.json().get("message", ""))
        if "OPEN_MAP_AND_LOCAL" in message:
            raise RuntimeError(
                "카카오맵 사용 설정이 꺼져 있습니다. 카카오디벨로퍼스에서 "
                "해당 앱의 제품 설정 → 카카오맵 → 사용 설정을 ON으로 바꿔 주세요."
            )
        raise RuntimeError("카카오 장소 검색 권한이 없습니다. 앱의 카카오맵 권한과 허용 IP 설정을 확인해 주세요.")

    if response.status_code == 429:
        raise RuntimeError("카카오 API 호출 한도를 초과했습니다. 잠시 후 다시 시도해 주세요.")

    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=3600, show_spinner=False)
def search_keyword(query, x=None, y=None, radius=None, size=15, page=1):
    """
    카카오 키워드 검색 API를 호출한다.

    x, y, radius를 함께 전달하면 특정 지점의 반경 안에서만 검색한다.
    카카오 API에서 x는 경도, y는 위도를 뜻한다.
    """
    params = {
        "query": query,
        "size": min(size, 15),
        "page": page,
        "sort": "distance" if x is not None else "accuracy",
    }

    # 중심 좌표가 있을 때만 반경 검색 조건을 추가한다.
    if x is not None and y is not None and radius is not None:
        params.update({"x": x, "y": y, "radius": radius})

    data = kakao_api_get(
        "https://dapi.kakao.com/v2/local/search/keyword.json",
        params,
    )

    # API의 JSON 응답에서 프로젝트에 필요한 항목만 골라 표 형태로 만든다.
    rows = []
    for item in data.get("documents", []):
        rows.append(
            {
                "place_id": item["id"],
                "장소명": item["place_name"],
                "주소": item["road_address_name"] or item["address_name"],
                "위도": float(item["y"]),
                "경도": float(item["x"]),
                "카테고리": item["category_name"],
                "카테고리그룹": item.get("category_group_name", ""),
                "전화번호": item.get("phone", ""),
                "상세URL": item.get("place_url", ""),
                # 중심 좌표가 주어졌을 때 API가 반환하는 거리는 미터 단위이다.
                "중심거리(m)": float(item["distance"]) if item.get("distance") else np.nan,
            }
        )

    return pd.DataFrame(rows)


# ---------------------------------------------------------
# 4. API 카테고리를 장소 특성 점수로 가공하는 함수
# ---------------------------------------------------------

def category_to_features(category_text):
    """
    카카오 API의 문자 카테고리를 추천 모델이 사용할 1~5점 숫자로 변환한다.

    이 기준은 팀이 합의한 뒤 수정할 수 있다.
    예: '공원'이 포함되면 자연과 휴식 점수를 높게 부여한다.
    """
    text = str(category_text)

    # 어떤 특징에도 해당하지 않는 장소는 기본값 1점을 갖는다.
    scores = {column: 1 for column in PREFERENCE_COLUMNS}

    # 관련 키워드가 하나라도 들어 있으면 해당 특성을 5점으로 설정한다.
    for feature, keywords in FEATURE_RULES.items():
        if any(keyword in text for keyword in keywords):
            scores[feature] = 5

    # 한 장소가 여러 특성을 가질 수 있도록 일부 카테고리는 보조 점수도 준다.
    if "시장" in text:
        scores["문화"] = max(scores["문화"], 3)
        scores["활동"] = max(scores["활동"], 3)

    if "카페" in text:
        scores["맛집"] = max(scores["맛집"], 3)

    if "공원" in text or "정원" in text:
        scores["활동"] = max(scores["활동"], 3)

    return scores


def count_matched_keywords(category_text):
    """장소 카테고리에서 특성 분류용 키워드가 몇 개 발견됐는지 센다."""
    text = str(category_text)
    all_keywords = {keyword for keywords in FEATURE_RULES.values() for keyword in keywords}
    return sum(keyword in text for keyword in all_keywords)


def add_place_features(places):
    """검색된 모든 장소에 자연·맛집·문화 등의 특성 점수를 추가한다."""
    if places.empty:
        return places

    result = places.copy()
    feature_table = result["카테고리"].apply(category_to_features).apply(pd.Series)

    for column in PREFERENCE_COLUMNS:
        result[column] = feature_table[column]

    # 키워드가 하나도 발견되지 않은 장소는 억지로 자연 등에 넣지 않는다.
    result["매칭키워드수"] = result["카테고리"].apply(count_matched_keywords)
    result["분류상태"] = result["매칭키워드수"].apply(
        lambda count: "정상" if count > 0 else "분류 불가"
    )

    return result


# ---------------------------------------------------------
# 5. 사용자 취향과 장소 특성의 적합도 계산
# ---------------------------------------------------------

def calculate_preference_score(place, user_preferences):
    """사용자 취향과 장소 특성의 가중합을 0~100점으로 변환한다."""
    total_weight = sum(user_preferences.values())

    # 사용자가 모든 항목을 0으로 입력한 경우 0점으로 처리한다.
    if total_weight == 0:
        return 0.0

    weighted_sum = sum(
        user_preferences[column] * float(place[column])
        for column in PREFERENCE_COLUMNS
    )

    # 장소 특성의 최대값이 5점이므로 최대 가능한 가중합으로 나누어 정규화한다.
    return round(weighted_sum / (5 * total_weight) * 100, 1)


def make_model_features(user_preferences, place):
    """
    머신러닝 모델에 넣을 한 행의 입력 데이터를 만든다.

    입력에는 사용자 취향, 장소 특성, 두 값의 곱인 일치도를 사용한다.
    """
    features = {}

    for column in PREFERENCE_COLUMNS:
        features[f"사용자_{column}"] = user_preferences[column]
        features[f"장소_{column}"] = float(place[column])
        features[f"일치_{column}"] = user_preferences[column] * float(place[column])

    return features


def calculate_available_minutes(start_value, end_value):
    """시작 시각과 종료 시각의 차이를 분 단위로 계산한다."""
    start_minutes = start_value.hour * 60 + start_value.minute
    end_minutes = end_value.hour * 60 + end_value.minute
    return end_minutes - start_minutes


def recommend_place_count(available_minutes, stay_minutes, transport):
    """여행시간, 평균 체류시간, 교통수단으로 적정 장소 수를 계산한다."""
    average_travel_minutes = {"도보": 25, "대중교통": 30, "자동차": 20}[transport]
    return max(1, int(available_minutes // (stay_minutes + average_travel_minutes)))


def calculate_distance_score(distance_m, radius_m):
    """메인 여행지와 가까울수록 높은 0~100점의 거리점수를 만든다."""
    if pd.isna(distance_m):
        return 0.0
    ratio = min(max(float(distance_m) / radius_m, 0), 1)
    return round((1 - ratio) * 100, 1)


def combine_preference_and_distance(preference_score, distance_score, transport):
    """교통수단에 따라 취향과 거리의 중요도를 다르게 적용한다."""
    weights = {"도보": (0.60, 0.40), "대중교통": (0.70, 0.30), "자동차": (0.80, 0.20)}
    preference_weight, distance_weight = weights[transport]
    return round(preference_score * preference_weight + distance_score * distance_weight, 1)


def representative_place_name(place_name):
    """체인점 지점명을 대표 브랜드명으로 통일하고 일반 장소명도 정규화한다."""
    name = str(place_name).strip()
    for brand in CHAIN_BRANDS:
        if brand in name:
            return brand
    normalized = re.sub(r"\s+", "", name)
    normalized = re.sub(r"(본점|[가-힣A-Za-z0-9]+점|\d+호점)$", "", normalized)
    return normalized or name


def remove_chain_and_similar_duplicates(scored_places):
    """같은 체인 또는 유사한 이름은 점수가 가장 높은 한 곳만 남긴다."""
    result = scored_places.copy()
    result["대표장소명"] = result["장소명"].apply(representative_place_name)
    result = result.sort_values("최종추천점수", ascending=False)
    return result.drop_duplicates(subset="대표장소명", keep="first").reset_index(drop=True)


# 모델에서 사용할 열의 순서를 미리 고정한다.
MODEL_COLUMNS = (
    [f"사용자_{column}" for column in PREFERENCE_COLUMNS]
    + [f"장소_{column}" for column in PREFERENCE_COLUMNS]
    + [f"일치_{column}" for column in PREFERENCE_COLUMNS]
)


@st.cache_resource(show_spinner=False)
def train_model_if_possible(training_file):
    """
    training_data.csv가 있고 데이터가 충분하면 랜덤 포레스트를 학습한다.

    데이터가 부족하거나 열 이름이 다르면 모델을 사용하지 않고
    기존 가중치 추천으로 안전하게 돌아간다.
    """
    if not os.path.exists(training_file):
        return None, "학습 데이터 없음"

    data = pd.read_csv(training_file)
    required_columns = MODEL_COLUMNS + ["사용자평가"]

    if not all(column in data.columns for column in required_columns):
        return None, "학습 데이터 열 형식 불일치"

    if len(data) < 50 or data["사용자평가"].nunique() < 2:
        return None, f"학습 데이터 부족: {len(data)}개"

    model = RandomForestRegressor(
        n_estimators=250,
        max_depth=8,
        random_state=42,
    )
    model.fit(data[MODEL_COLUMNS], data["사용자평가"])

    return model, f"랜덤 포레스트 학습 완료: {len(data)}개"


# ---------------------------------------------------------
# 6. 취향에 따라 검색할 키워드 결정
# ---------------------------------------------------------

def make_search_keywords(user_preferences):
    """선호도가 높은 상위 4개 취향에서 장소 검색 키워드를 만든다."""
    keyword_map = {
        "자연": ["공원", "정원", "자연 관광명소"],
        "맛집": ["맛집", "음식점", "전통시장"],
        "문화": ["박물관", "미술관", "문화 관광명소"],
        "쇼핑": ["쇼핑몰", "백화점", "시장"],
        "휴식": ["카페", "공원", "정원"],
        "활동": ["체험", "스포츠", "테마파크"],
        "실내": ["박물관", "미술관", "쇼핑몰"],
    }

    # 점수가 높은 취향부터 정렬하고, 0점인 취향은 검색에서 제외한다.
    ranked_preferences = sorted(
        user_preferences,
        key=lambda column: user_preferences[column],
        reverse=True,
    )
    selected_preferences = [
        column for column in ranked_preferences
        if user_preferences[column] > 0
    ][:4]

    # 동일한 키워드가 여러 취향에 포함될 수 있으므로 중복을 제거한다.
    keywords = []
    for preference in selected_preferences:
        for keyword in keyword_map[preference]:
            if keyword not in keywords:
                keywords.append(keyword)

    return keywords


def collect_candidate_places(main_place, radius_m, user_preferences):
    """메인 여행지의 설정 반경 안에서 취향과 관련된 장소 후보를 모은다."""
    frames = []

    for keyword in make_search_keywords(user_preferences):
        # 검색 결과를 늘리기 위해 키워드마다 1, 2페이지를 요청한다.
        for page in [1, 2]:
            result = search_keyword(
                query=keyword,
                x=main_place["경도"],
                y=main_place["위도"],
                radius=radius_m,
                size=15,
                page=page,
            )

            if not result.empty:
                frames.append(result)

    if not frames:
        return pd.DataFrame()

    candidates = pd.concat(frames, ignore_index=True)

    # 같은 장소가 여러 키워드에서 검색될 수 있으므로 place_id로 중복을 제거한다.
    candidates = candidates.drop_duplicates(subset="place_id")

    # 메인 여행지 자체가 후보에 다시 포함되는 것을 방지한다.
    candidates = candidates[candidates["place_id"] != main_place["place_id"]]

    # 좌표나 카테고리가 없는 데이터는 추천 계산에서 제외한다.
    candidates = candidates.dropna(subset=["위도", "경도", "카테고리"])

    return add_place_features(candidates).reset_index(drop=True)


# ---------------------------------------------------------
# 7. 장소가 한 종류로 몰리지 않게 다양성을 적용
# ---------------------------------------------------------

def choose_recommendation_group(place):
    """장소 특성 중 가장 높은 항목을 대표 추천 분류로 사용한다."""
    if place.get("분류상태") == "분류 불가":
        return "기타"
    scores = {column: float(place[column]) for column in PREFERENCE_COLUMNS}
    if max(scores.values()) <= 1:
        return "기타"
    return max(scores, key=scores.get)


def calculate_category_quotas(user_preferences, target_count):
    """취향 점수 비율에 따라 분류별 추천 장소 수를 배분한다."""
    active = {key: value for key, value in user_preferences.items() if value > 0}
    if not active:
        return {}
    total = sum(active.values())
    raw = {key: target_count * value / total for key, value in active.items()}
    quotas = {key: int(value) for key, value in raw.items()}
    ranked = sorted(active, key=active.get, reverse=True)
    quotas[ranked[0]] = max(quotas[ranked[0]], min(2, target_count))
    if len(ranked) > 1 and target_count > 1:
        quotas[ranked[1]] = max(quotas[ranked[1]], 1)
    cap = max(1, int(np.ceil(target_count * 0.5)))
    quotas = {key: min(value, cap) for key, value in quotas.items()}
    while sum(quotas.values()) < target_count:
        added = False
        for key in sorted(active, key=lambda item: raw[item] - int(raw[item]), reverse=True):
            if quotas[key] < cap:
                quotas[key] += 1
                added = True
                break
        if not added:
            break
    return quotas


def select_diverse_places(scored_places, target_count, user_preferences):
    """취향 비율로 계산한 분류별 목표 개수에 맞춰 후보를 선택한다."""
    selected_rows = []
    selected_ids = set()
    quotas = calculate_category_quotas(user_preferences, target_count)
    sorted_places = scored_places.sort_values("최종추천점수", ascending=False)
    for group, quota in quotas.items():
        for _, place in sorted_places[sorted_places["추천분류"] == group].head(quota).iterrows():
            selected_rows.append(place)
            selected_ids.add(place["place_id"])
    if len(selected_rows) < target_count:
        for _, place in sorted_places.iterrows():
            if place["place_id"] not in selected_ids:
                selected_rows.append(place)
                selected_ids.add(place["place_id"])
            if len(selected_rows) >= target_count:
                break
    if not selected_rows:
        return pd.DataFrame()
    return pd.DataFrame(selected_rows).sort_values("최종추천점수", ascending=False).reset_index(drop=True)


# =========================================================
# Google Places 조회와 최종 선정 로직 (selector.py와 동일)
# =========================================================

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


# ---------------------------------------------------------
# 8. 사용자 입력 화면
# ---------------------------------------------------------

with st.sidebar:
    st.header("여행 조건 입력")

    region = st.text_input(
        "여행 지역",
        value="서울",
        help="동일한 장소명이 여러 지역에 존재할 때 검색 정확도를 높입니다.",
    )

    main_place_name = st.text_input(
        "메인 여행지",
        value="경복궁",
        help="여행 일정에 반드시 포함할 기준 장소입니다.",
    )

    radius_km = st.slider(
        "검색 반경(km)",
        min_value=1,
        max_value=20,
        value=5,
    )

    trip_date = st.date_input("여행 날짜", value=date.today())
    start_time = st.time_input("여행 시작 시간", value=time(9, 0))
    end_time = st.time_input("여행 종료 시간", value=time(18, 0))

    transport = st.selectbox(
        "교통수단",
        ["도보", "대중교통", "자동차"],
    )

    average_stay_minutes = st.slider(
        "장소당 예상 체류시간(분)", 30, 180, 90, 30
    )

    desired_places = st.slider(
        "최종 희망 장소 수",
        min_value=2,
        max_value=8,
        value=5,
    )

    auto_adjust_count = st.checkbox(
        "여행시간에 맞춰 장소 수 자동 조정",
        value=True,
        help="선택하면 희망 장소 수와 권장 장소 수 중 더 작은 값을 적용합니다.",
    )

    st.subheader("여행 취향")
    st.caption("0점: 관심 없음 / 5점: 매우 중요")

    user_preferences = {
        column: st.slider(column, 0, 5, 3, key=f"preference_{column}")
        for column in PREFERENCE_COLUMNS
    }


# ---------------------------------------------------------
# 9. 메인 여행지 검색 및 선택
# ---------------------------------------------------------

if not KAKAO_KEY:
    st.error(
        "카카오 REST API 키가 설정되지 않았습니다. "
        ".streamlit/secrets.toml 파일을 확인해 주세요."
    )
    st.stop()

if start_time >= end_time:
    st.warning("종료 시간은 시작 시간보다 늦어야 합니다.")

# 입력받은 시간과 교통수단을 실제 권장 장소 수와 반경 안내에 사용한다.
available_minutes = calculate_available_minutes(start_time, end_time)
recommended_place_count = recommend_place_count(
    available_minutes, average_stay_minutes, transport
) if available_minutes > 0 else 0
recommended_radius = {"도보": 3, "대중교통": 10, "자동차": 20}[transport]
effective_place_count = (
    min(desired_places, recommended_place_count)
    if auto_adjust_count and recommended_place_count > 0
    else desired_places
)
# 최종 장소 조합을 비교할 수 있도록 여분의 후보를 전달한다.
candidate_count = min(15, effective_place_count + 5)

if available_minutes > 0:
    st.info(
        f"전체 일정은 {available_minutes}분입니다. {transport}, 장소당 "
        f"{average_stay_minutes}분 체류 기준으로 약 {recommended_place_count}곳을 권장합니다."
    )
if radius_km > recommended_radius:
    st.warning(
        f"{transport} 이용 시 권장 검색 반경은 약 {recommended_radius}km 이내입니다. "
        "먼 장소는 추천점수에서 거리 감점을 받습니다."
    )

# 검색 버튼을 눌렀을 때 결과를 session_state에 저장한다.
# Streamlit은 입력할 때마다 코드를 다시 실행하므로 결과 보존에 session_state가 필요하다.
def clear_recommendation(requested=False):
    if requested or "p2_result" in st.session_state:
        mark_pending(APP_ROOT / "outputs", "stale", "여행 조건 변경 또는 새 검색")
    for key in ("final_candidates", "p2_result", "trip_context", "candidate_signature", "saved_signature", "result_folder", "google_client"):
        st.session_state.pop(key, None)


search_terms = (region.strip(), main_place_name.strip())
if st.session_state.get("search_terms") != search_terms:
    st.session_state.pop("main_search_result", None)
    clear_recommendation()

if st.button("메인 여행지 검색", type="primary"):
    st.session_state.pop("main_search_result", None)
    clear_recommendation(requested=True)
    st.session_state["search_terms"] = search_terms
    try:
        search_result = search_keyword(f"{region} {main_place_name}", size=5)

        if search_result.empty:
            st.warning("메인 여행지를 찾지 못했습니다. 지역과 장소명을 다시 확인해 주세요.")
        else:
            st.session_state["main_search_result"] = search_result
    except Exception as error:
        st.error(f"장소 검색 중 오류가 발생했습니다: {error}")


if "main_search_result" in st.session_state:
    main_search_result = st.session_state["main_search_result"]

    # 장소명과 주소를 함께 보여주어 동명이인 장소를 구분할 수 있게 한다.
    option_labels = [
        f"{row['장소명']} | {row['주소']}"
        for _, row in main_search_result.iterrows()
    ]

    selected_label = st.selectbox(
        "검색 결과에서 정확한 메인 여행지를 선택하세요.",
        option_labels,
    )

    selected_index = option_labels.index(selected_label)
    main_place = main_search_result.iloc[selected_index]

    st.info(
        f"선택한 메인 여행지: {main_place['장소명']} / {main_place['주소']}"
    )

    # -----------------------------------------------------
    # 10. 반경 내 후보 수집, 점수 계산, 다양성 적용
    # -----------------------------------------------------

    current_context = {
        "source": "kakao_local", "min_restaurants": 2,
        "region": region.strip(), "main_query": main_place_name.strip(),
        "main_place_id": str(main_place["place_id"]),
        "target_count": int(effective_place_count), "desired_count": int(desired_places),
        "radius_m": float(radius_km * 1000), "transport": transport,
        "trip_date": trip_date.isoformat(), "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(), "average_stay_minutes": average_stay_minutes,
        "auto_adjust_count": auto_adjust_count, "preferences": dict(user_preferences),
    }
    if "candidate_signature" in st.session_state and st.session_state["candidate_signature"] != current_context:
        clear_recommendation()
        st.info("여행 조건이 바뀌었습니다. 여행 장소 추천을 눌러 새 결과를 확인하세요.")

    if st.button("여행 장소 추천", type="primary"):
        clear_recommendation(requested=True)
        if sum(user_preferences.values()) == 0:
            st.warning("여행 취향을 한 개 이상 선택해 주세요.")
            st.stop()

        if start_time >= end_time:
            st.warning("여행 시작 시간과 종료 시간을 다시 확인해 주세요.")
            st.stop()

        try:
            with st.spinner("반경 안의 장소를 수집하고 취향 점수를 계산하고 있습니다..."):
                # 카카오 API의 반경 단위는 m이므로 km에 1,000을 곱한다.
                candidates = collect_candidate_places(
                    main_place=main_place,
                    radius_m=radius_km * 1000,
                    user_preferences=user_preferences,
                )

                if candidates.empty:
                    st.warning("조건에 맞는 후보 장소를 찾지 못했습니다. 반경을 넓혀 주세요.")
                    st.stop()

                # 모든 후보에 기본 취향 적합도 점수를 계산한다.
                candidates["취향적합도"] = candidates.apply(
                    lambda place: calculate_preference_score(place, user_preferences),
                    axis=1,
                )

                candidates["거리점수"] = candidates["중심거리(m)"].apply(
                    lambda distance: calculate_distance_score(distance, radius_km * 1000)
                )

                # 학습 데이터가 있으면 모델 예상평가를 계산한다.
                model, model_status = train_model_if_possible(TRAINING_FILE)

                if model is not None:
                    model_input = pd.DataFrame(
                        [
                            make_model_features(user_preferences, place)
                            for _, place in candidates.iterrows()
                        ]
                    )[MODEL_COLUMNS]

                    candidates["모델예상평가"] = np.clip(
                        model.predict(model_input),
                        1,
                        5,
                    ).round(2)

                    # 학습 예측 80%, 기본 취향 계산 20%로 통합 선호점수를 만든다.
                    candidates["통합선호점수"] = (
                        candidates["모델예상평가"] / 5 * 80
                        + candidates["취향적합도"] * 0.2
                    ).round(1)
                    recommendation_method = "학습 모델 + 취향 가중치"
                else:
                    candidates["모델예상평가"] = np.nan
                    candidates["통합선호점수"] = candidates["취향적합도"]
                    recommendation_method = "취향 가중치"

                # 도보는 거리를 많이, 자동차는 취향을 더 많이 반영한다.
                candidates["최종추천점수"] = candidates.apply(
                    lambda place: combine_preference_and_distance(
                        place["통합선호점수"], place["거리점수"], transport
                    ), axis=1
                )

                # 각 장소의 대표 분류를 지정하고 같은 종류가 과도하게 몰리지 않게 한다.
                candidates["추천분류"] = candidates.apply(
                    choose_recommendation_group,
                    axis=1,
                )

                candidates = remove_chain_and_similar_duplicates(candidates)

                recommended = select_diverse_places(
                    candidates,
                    target_count=candidate_count,
                    user_preferences=user_preferences,
                )

                # 메인 여행지를 결과의 첫 행으로 추가한다.
                main_features = category_to_features(main_place["카테고리"])
                main_row = main_place.to_dict()
                main_row.update(main_features)
                main_row.update(
                    {
                        "필수여부": "필수",
                        "취향적합도": np.nan,
                        "거리점수": 100.0,
                        "통합선호점수": np.nan,
                        "모델예상평가": np.nan,
                        "최종추천점수": np.nan,
                        "추천분류": "메인 여행지",
                        "중심거리(m)": 0,
                        "분류상태": "정상",
                        "대표장소명": main_place["장소명"],
                        "사용자선택": "필수",
                    }
                )

                recommended = recommended.copy()
                recommended["필수여부"] = "후보"
                recommended["사용자선택"] = "포함"
                final_candidates = pd.concat(
                    [pd.DataFrame([main_row]), recommended],
                    ignore_index=True,
                )

                st.session_state["candidate_signature"] = current_context.copy()
                st.session_state["trip_context"] = {
                    **current_context, "captured_at": datetime.now(timezone.utc).isoformat(),
                }
                st.session_state["final_candidates"] = final_candidates
                st.session_state["model_status"] = model_status
                st.session_state["recommendation_method"] = recommendation_method

        except Exception as error:
            st.error(f"후보 장소를 구성하는 중 오류가 발생했습니다: {error}")





OUTPUT_COLUMNS = [
    "place_id", "필수여부", "장소명", "주소", "카테고리", "추천분류", "분류상태",
    "중심거리(m)", "취향적합도", "거리점수", "모델예상평가", "최종추천점수", "사용자선택",
    "자연", "맛집", "문화", "쇼핑", "휴식", "활동", "실내", "위도", "경도", "전화번호", "상세URL",
]


def ensure_restaurant_candidates(rows, context):
    """식당 후보가 부족할 때 같은 반경의 실제 검색 결과로 최대 12곳을 보충한다."""
    minimum = int(context.get("min_restaurants", 2))
    available = [r for r in rows if r.get("사용자선택") != "제외"]
    mandatory = [r for r in rows if r.get("필수여부") == "필수" or r.get("사용자선택") == "필수"]
    required_size = len(mandatory) + max(0, minimum - sum(is_restaurant(r) for r in mandatory))
    if context["target_count"] < required_size:
        raise ValueError(f"메인·필수 장소와 식당 {minimum}곳을 포함하려면 최소 {required_size}곳이 필요합니다. 희망 장소 수나 여행시간을 늘리거나 장소 수 자동 조정을 해제해 주세요.")
    if sum(is_restaurant(r) for r in available) >= 8:
        return rows, context
    center = next(r for r in rows if str(r["place_id"]) == str(context["main_place_id"]))
    seen = {str(r["place_id"]) for r in rows}
    batches = []
    for page in (1, 2):
        found = search_keyword("음식점", x=float(center["경도"]), y=float(center["위도"]),
                               radius=context["radius_m"], size=15, page=page)
        if found.empty:
            break
        found = found[found.apply(lambda row: is_restaurant(row.to_dict()) and str(row["place_id"]) not in seen, axis=1)]
        if not found.empty:
            batches.append(found)
        if sum(len(batch) for batch in batches) >= 12:
            break
    if not batches:
        return rows, context  # The selector reports an explicit unsatisfied constraint.
    extra = add_place_features(pd.concat(batches, ignore_index=True).drop_duplicates("place_id"))
    preferences = context["preferences"]
    extra["취향적합도"] = extra.apply(lambda row: calculate_preference_score(row, preferences), axis=1)
    extra["거리점수"] = extra["중심거리(m)"].apply(lambda value: calculate_distance_score(value, context["radius_m"]))
    model, _ = train_model_if_possible(TRAINING_FILE)
    extra["모델예상평가"] = np.nan
    extra["통합선호점수"] = extra["취향적합도"]
    if model is not None:
        features = pd.DataFrame([make_model_features(preferences, row) for _, row in extra.iterrows()])[MODEL_COLUMNS]
        extra["모델예상평가"] = np.clip(model.predict(features), 1, 5).round(2)
        extra["통합선호점수"] = (extra["모델예상평가"] / 5 * 80 + extra["취향적합도"] * .2).round(1)
    extra["최종추천점수"] = extra.apply(lambda row: combine_preference_and_distance(row["통합선호점수"], row["거리점수"], context["transport"]), axis=1)
    extra["추천분류"] = extra.apply(choose_recommendation_group, axis=1)
    extra = remove_chain_and_similar_duplicates(extra)
    extra = extra.sort_values(["최종추천점수", "place_id"], ascending=[False, True]).head(min(12, max(0, 30 - len(rows))))
    extra["필수여부"] = "후보"
    extra["사용자선택"] = "포함"
    additions = extra[OUTPUT_COLUMNS].to_dict("records")
    return rows + additions, {**context, "restaurant_candidates_added": [str(r["place_id"]) for r in additions]}



# ---------------------------------------------------------
# 13. Google 조회, 최종 장소 선정, 전달 파일
# ---------------------------------------------------------

def persist_inputs(rows, context):
    data_dir = APP_ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "candidate_places.csv").write_bytes(result_csv_bytes(rows))
    (data_dir / "trip_context.json").write_text(json.dumps(clean(context), ensure_ascii=False, indent=2), encoding="utf-8")


if "final_candidates" in st.session_state:
    if "p2_result" not in st.session_state:
        mark_pending(APP_ROOT / "outputs")
        try:
            api = GooglePlaces(load_keys(APP_ROOT).get("GOOGLE_PLACES_API_KEY", ""))
            with st.status("후보 장소의 평점과 영업시간을 확인하고 있습니다…", expanded=True) as progress:
                input_rows = st.session_state["final_candidates"][OUTPUT_COLUMNS].to_dict("records")
                context = {**st.session_state["trip_context"], "meal_stay_minutes": 60}
                input_rows, context = ensure_restaurant_candidates(input_rows, context)
                persist_inputs(input_rows, context)
                st.write(f"카카오 후보 {len(input_rows)}곳의 이름과 위치를 Google 장소와 대조합니다.")
                enriched = enrich_candidates(input_rows, context, api)
                st.write("여행 날짜의 영업 구간과 점심·저녁 식당을 확인합니다.")
                selection = choose_places(enriched, context)
                selection["summary"]["google_requests"] = api.calls
                folder = save_result(selection, APP_ROOT / "outputs")
                st.session_state["p2_result"] = selection
                st.session_state["result_folder"] = str(folder)
                st.session_state["final_candidates"] = pd.DataFrame(input_rows)
                st.session_state["trip_context"] = context
                st.session_state["google_client"] = api
                progress.update(label="최종 장소 선정 완료", state="complete", expanded=False)
        except (ValueError, RuntimeError, requests.RequestException, OSError) as error:
            mark_pending(APP_ROOT / "outputs", "failed", str(error))
            st.error(str(error))
            st.info("여행 조건을 조정한 뒤 ‘여행 장소 추천’을 다시 눌러 주세요. 실패한 실행에서는 최종 결과를 만들지 않습니다.")
            st.stop()

    result = st.session_state["p2_result"]
    summary = result["summary"]
    st.divider()
    st.subheader("최종 방문 장소")
    st.caption("Google Maps · 평점·평가 수·영업시간 / Kakao Local · 후보 장소·좌표")
    m1, m2, m3 = st.columns(3)
    m1.metric("최종 장소", f"{summary['selected_count']}곳")
    m2.metric("필수 식사 장소", "2곳")
    m3.metric("영업시간 확인 필요", f"{summary['unknown_hours_count']}곳")
    st.write("메인 여행지와 점심·저녁 식당을 포함했습니다. 아래 목록의 방문 순서는 경로 계산에서 정합니다.")
    if summary["unknown_hours_count"]:
        st.warning("영업시간이 없는 장소가 포함되어 있습니다. 해당 장소는 방문 전에 운영시간을 확인해야 합니다.")
    if any(r["hours_source"] == "regularOpeningHours" for r in result["selected"]):
        st.info("평소 요일별 영업시간을 적용한 장소가 있습니다. 휴일·특별 운영시간은 여행일에 다시 확인해 주세요.")
    if sum(r["stay_minutes"] for r in result["selected"]) > parse_time(result["context"]["end_time"])-parse_time(result["context"]["start_time"]):
        st.warning("장소별 체류시간의 합이 여행시간을 초과합니다. 장소 수를 줄여 주세요.")

    def hours_text(row):
        intervals = row["opening_intervals"]
        if intervals is None:
            return "확인 필요"
        return ", ".join(f"{a//60:02d}:{a%60:02d}~{b//60:02d}:{b%60:02d}" for a, b in intervals)

    table = pd.DataFrame([{
        "장소": r["장소명"], "구분": "필수 식당" if r["meal_required"] else r["필수여부"],
        "Google 평점": r["google_rating"], "평가 수": r["google_rating_count"],
        "보정 평점": round(r["adjusted_rating"], 3) if r["adjusted_rating"] is not None else None,
        "여행일 영업시간": hours_text(r), "장소 정보": r.get("google_maps_url", "")
    } for r in result["selected"]])
    st.dataframe(table, hide_index=True, width="stretch", column_config={
        "장소 정보": st.column_config.LinkColumn(display_text="Google Maps"),
        "Google 평점": st.column_config.NumberColumn(format="%.1f"),
        "보정 평점": st.column_config.NumberColumn(format="%.3f"),
    })
    st.caption("영업 구간은 현지 시각(한국)입니다. 영업시간 안에 체류할 수 있는지 확인했으며, 이동시간·예약·마지막 주문 시간은 경로 계산 시 추가 확인이 필요합니다.")
    for r in result["selected"]:
        for attribution in r.get("attributions", []):
            st.write(f"출처: {attribution.get('provider', '')} {attribution.get('providerUri', '')}")

    with st.expander("선정 기준과 후보 비교"):
        st.write("취향에 맞는 후보는 첫 단계에서 좁힙니다. 최종 단계에서는 필수 장소, 여행일 영업시간, 서로 다른 식당 2곳을 확인합니다. 조건을 만족하는 집합 중 영업시간이 확인된 장소 수, 평가가 있는 장소 수, 보정 평점 합계 순으로 비교합니다. 추가 검색한 식당은 식사 두 자리에만 사용합니다.")
        st.code("보정 평점 = (평가 수 × Google 평점 + 20 × 후보 평균 평점) / (평가 수 + 20)", language=None)
        st.write("평가가 몇 개 없는 5점 장소가 바로 최상위가 되지 않도록 평균 쪽으로 보정합니다. 20은 현재 정한 기준값이며 학습된 값은 아닙니다. 이전의 추천점수·용도 중복·거리 감점은 최종 선정에 사용하지 않습니다.")
        st.caption("보정 평점은 자체 계산값입니다. Google 원본 평점과 별도이며 만족도 확률이 아닙니다. 리뷰 본문 일부로 전체 방문자 감정을 추정하지 않습니다.")
        st.dataframe(pd.DataFrame([{"장소": r["장소명"], "선정": r["selected"], "연결 상태": r["match_status"],
            "Google 평점": r["google_rating"], "평가 수": r["google_rating_count"], "영업시간": hours_text(r),
            "근거": r["selection_reason"]} for r in result["selected"]+result["not_selected"]]), hide_index=True, width="stretch")

    with st.expander("선정 장소 리뷰 확인"):
        st.caption("Google에서 제공하는 관련성 순 리뷰 최대 5개입니다. 전체 리뷰가 아니며, 버튼을 누를 때만 추가 조회합니다.")
        for r in result["selected"]:
            if st.button(f"{r['장소명']} 리뷰 보기", key="review_"+r["place_id"]):
                try:
                    review_data = st.session_state["google_client"].reviews(r["google_place_id"])
                    st.write(f"Google Maps · {r['장소명']}")
                    reviews = review_data.get("reviews", [])
                    if not reviews:
                        st.info("제공된 리뷰 본문이 없습니다.")
                    for review in reviews:
                        author = review.get("authorAttribution", {})
                        st.write(f"{author.get('displayName', '작성자')} · {review.get('rating', '—')}점 · {review.get('publishTime', '')[:10]}")
                        if author.get("uri"):
                            st.link_button("작성자", author["uri"])
                        st.write(review.get("text", {}).get("text", ""))
                        if review.get("googleMapsUri"):
                            st.link_button("리뷰 원문", review["googleMapsUri"])
                    for attribution in review_data.get("attributions", []):
                        st.write(f"출처: {attribution.get('provider', '')} {attribution.get('providerUri', '')}")
                except RuntimeError as error:
                    st.error(str(error))

    st.subheader("경로 계산용 파일")
    st.caption("CSV에는 최종 장소·카카오 좌표·선정 근거가, JSON에는 체류시간·영업 구간·점심/저녁 도착 가능 구간이 들어 있습니다.")
    c1, c2 = st.columns(2)
    c1.download_button("최종 장소 CSV", result_csv_bytes(result["selected"]), "final_places.csv", "text/csv", type="primary", width="stretch")
    c2.download_button("경로 입력 JSON", json.dumps(route_payload(result), ensure_ascii=False, indent=2), "route_input.json", "application/json", width="stretch")
    st.caption(f"저장 위치: {st.session_state['result_folder']}")
    st.caption("결과는 조회 당시 정보입니다. 영업시간이 바뀔 수 있으므로 실제 여행일에 다시 조회해 주세요.")

with st.expander("데이터 출처"):
    st.markdown("후보 검색: Kakao Local · 평점, 평가 수, 영업시간, 리뷰: Google Maps. [Google Maps 이용약관](https://maps.google.com/help/terms_maps/) · [Google 개인정보처리방침](https://policies.google.com/privacy) · [Google 리뷰 정책](https://support.google.com/contributionpolicy/answer/7400114)")
