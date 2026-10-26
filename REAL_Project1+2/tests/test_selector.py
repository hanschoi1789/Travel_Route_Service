import importlib.util
import itertools
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("selector", Path(__file__).resolve().parents[1]/"selector.py")
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


def period(day, start, end_day, end):
    return {"open": {"day": day, "hour": start//60, "minute": start%60}, "close": {"day": end_day, "hour": end//60, "minute": end%60}}


def row(i, food=False, mandatory=False):
    return {"place_id": str(i), "장소명": "식당"+str(i) if food else "장소"+str(i), "주소": "서울 종로구 사직로 161",
            "카테고리": "음식점 > 한식" if food else "여행 > 관광지", "위도": 37.5796, "경도": 126.977,
            "필수여부": "필수" if mandatory else "후보", "사용자선택": "포함"}


CTX = {"main_place_id": "0", "target_count": 4, "trip_date": "2026-10-26", "start_time": "09:00", "end_time": "19:00", "average_stay_minutes": 90, "meal_stay_minutes": 60}


class Fake:
    def __init__(self, override=None):
        self.override = override or {}
    def find(self, r):
        p = {"id": "g"+r["place_id"], "displayName": {"text": r["장소명"]}, "rating": 4.2, "userRatingCount": 100,
             "businessStatus": "OPERATIONAL", "regularOpeningHours": {"periods": [period(1, 540, 1, 1200)]}}
        p.update(self.override.get(r["place_id"], {}))
        return p, {"status": "matched", "distance_m": 0}


def test_closed_unknown_and_split_hours():
    assert s.opening_windows({}, "2026-10-26", "2026-10-26")[0] is None
    assert s.opening_windows({"regularOpeningHours": {}}, "2026-10-26", "2026-10-26")[0] == []
    p = {"regularOpeningHours": {"periods": [period(1, 660, 1, 840), period(1, 1020, 1, 1200)]}}
    windows, _ = s.opening_windows(p, "2026-10-26", "2026-10-26")
    assert windows == [[660,840], [1020,1200]]
    assert s.feasible_starts(windows, 800, 1100, 60) == [[1020,1040]]
    assert s.opening_windows(p, "2026-10-27", "2026-10-26")[0] == []


def test_overnight_and_always_open():
    p = {"regularOpeningHours": {"periods": [period(0, 1320, 1, 120)]}}
    assert s.opening_windows(p, "2026-10-26", "2026-10-26")[0] == [[0,120]]
    p["regularOpeningHours"]["periods"] = [{"open": {"day":0,"hour":0}}]
    assert s.opening_windows(p, "2026-10-26", "2026-10-26")[0] == [[0,1440]]
    p["regularOpeningHours"]["periods"] = [{"open": {"day":1,"hour":9}}]
    assert s.opening_windows(p, "2026-10-26", "2026-10-26")[0] is None


def test_current_special_day_and_future_regular():
    p = {"currentOpeningHours": {}, "regularOpeningHours": {"periods": [period(1,540,1,1080)]}}
    assert s.opening_windows(p, "2026-10-26", "2026-10-26") == ([], "currentOpeningHours")
    assert s.opening_windows(p, "2026-11-02", "2026-10-26") == ([[540,1080]], "regularOpeningHours")


def test_current_date_overnight():
    p = {"currentOpeningHours": {"periods": [{"open":{"day":0,"hour":22,"date":{"year":2026,"month":10,"day":25}}, "close":{"day":1,"hour":2,"date":{"year":2026,"month":10,"day":26}}}]}}
    assert s.opening_windows(p, "2026-10-26", "2026-10-25")[0] == [[0,120]]


def test_matching_rejects_wrong_branch_and_ambiguous():
    r = row(0)
    p = {"id":"x", "displayName":{"text":r["장소명"]}, "location":{"latitude":37.5796,"longitude":126.977}}
    assert s.match_place(r,[p])[1]["status"] == "matched"
    far = {**p,"location":{"latitude":37.9,"longitude":126.977}}
    assert s.match_place(r,[far])[0] is None
    assert s.match_place(r,[p,{**p,"id":"y"}])[1]["status"] == "ambiguous"
    assert s.match_place(r,[{**p,"displayName":{"text":"다른 장소"}}])[0] is None


@pytest.mark.parametrize("category", ["음식점 > 카페", "음식점 > 술집", "음식점 > 베이커리"])
def test_cafes_not_meals(category):
    assert not s.is_restaurant({"카테고리":category})


@pytest.mark.parametrize("child,parent", [("국립현대미술관 서울 미술도서관","국립현대미술관 서울"),("국립고궁박물관 별관","국립고궁박물관"),("국립고궁박물관 고궁배움터","국립고궁박물관")])
def test_sub_facility_does_not_inherit_parent_rating(child,parent):
    r={**row(0),"장소명":child}
    p={"id":"x","displayName":{"text":parent},"formattedAddress":r["주소"],"location":{"latitude":r["위도"],"longitude":r["경도"]}}
    assert s.match_place(r,[p])[0] is None


def test_cafe_does_not_match_museum():
    r={**row(0),"장소명":"이디야커피 국립고궁박물관점","카테고리":"음식점 > 카페"}
    p={"id":"x","displayName":{"text":"국립고궁박물관"},"formattedAddress":r["주소"],"types":["museum","tourist_attraction"],"location":{"latitude":r["위도"],"longitude":r["경도"]}}
    assert s.match_place(r,[p])[0] is None


def test_supplement_restaurants_only_fill_two_meal_slots():
    ctx={**CTX,"restaurant_candidates_added":["1","2","3"]}
    rows=[row(0,mandatory=True),row(1,True),row(2,True),row(3,True),row(4)]
    data=s.enrich_candidates(rows,ctx,Fake({"3":{"rating":5},"4":{"rating":3}}),"2026-10-26")
    selected=s.choose_places(data,ctx)["selected"]
    assert "4" in {r["place_id"] for r in selected}
    assert sum(r["is_restaurant"] for r in selected)==2


def test_selection_and_export(tmp_path):
    rows = [row(0,mandatory=True),row(1,True),row(2,True),row(3),row(4)]
    enriched = s.enrich_candidates(rows, CTX, Fake({"3":{"rating":5,"userRatingCount":1},"4":{"rating":4.7,"userRatingCount":500}}), "2026-10-26")
    result = s.choose_places(enriched, CTX)
    assert {r["place_id"] for r in result["selected"]} == {"0","1","2","4"}
    assert sum(r["meal_required"] for r in result["selected"]) == 2
    assert all(r["mandatory"] for r in result["selected"] if r["meal_required"])
    out = s.save_result(result,tmp_path)
    assert (out/"route_input.json").exists()
    payload = s.route_payload(result)
    assert payload["route_verified"] is False
    assert all(p["coordinate_source"] == "kakao_local" for p in payload["places"])
    assert payload["meal_assignment_example"]["lunch"] != payload["meal_assignment_example"]["dinner"]


def test_meal_hours_missing_cannot_satisfy_quota():
    rows = [row(0,mandatory=True),row(1,True),row(2,True),row(3)]
    enriched = s.enrich_candidates(rows,CTX,Fake({"2":{"regularOpeningHours":None}}),"2026-10-26")
    with pytest.raises(ValueError,match="조건에 맞는"):
        s.choose_places(enriched,CTX)


def test_closed_mandatory_blocks_and_unknown_is_flagged():
    rows = [row(0,mandatory=True),row(1,True),row(2,True),row(3)]
    with pytest.raises(ValueError,match="필수 장소"):
        s.choose_places(s.enrich_candidates(rows,CTX,Fake({"0":{"regularOpeningHours":{}}}),"2026-10-26"),CTX)
    result = s.choose_places(s.enrich_candidates(rows,CTX,Fake({"0":{"regularOpeningHours":None}}),"2026-10-26"),CTX)
    assert result["summary"]["unknown_hours_count"] == 1
    assert s.route_payload(result)["places"][0]["requires_hours_check"]


def test_duplicates_and_exclusion():
    rows=[row(0,mandatory=True),row(1,True),row(2,True),row(3),row(4)]
    rows[-1]["사용자선택"]="제외"
    data=s.enrich_candidates(rows,CTX,Fake({"3":{"id":"g0"}}),"2026-10-26")
    assert data[3]["exclusion_reason"]
    assert data[4]["match_status"] == "not_requested"


def test_no_lunch_dinner_window_and_too_few_places():
    rows=[row(0,mandatory=True),row(1,True),row(2,True)]
    with pytest.raises(ValueError, match="두 식사 시간대"):
        s.enrich_candidates(rows,{**CTX,"end_time":"16:00"},Fake())
    with pytest.raises(ValueError):
        s.choose_places(s.enrich_candidates(rows,CTX,Fake()),{**CTX,"target_count":2})


def test_old_scores_do_not_change_selection():
    rows = [row(0,mandatory=True),row(1,True),row(2,True),row(3),row(4)]
    a=s.choose_places(s.enrich_candidates(rows,CTX,Fake(),"2026-10-26"),CTX)
    for i,r in enumerate(rows): r["최종추천점수"]=i*9999
    b=s.choose_places(s.enrich_candidates(rows,CTX,Fake(),"2026-10-26"),CTX)
    assert [r["place_id"] for r in a["selected"]] == [r["place_id"] for r in b["selected"]]


def test_dp_matches_brute_force_with_duration_budget():
    ctx={**CTX,"target_count":5,"average_stay_minutes":180,"end_time":"18:00"}
    rows=[row(0,mandatory=True)]+[row(i, i<4) for i in range(1,8)]
    data=s.enrich_candidates(rows,ctx,Fake({str(i):{"rating":3+i/5,"userRatingCount":20*i} for i in range(1,8)}),"2026-10-26")
    result=s.choose_places(data,ctx)
    all_rows=result["selected"]+result["not_selected"]
    possible=[]
    for combo in itertools.combinations(all_rows,5):
        if not any(r["place_id"]=="0" for r in combo): continue
        if sum(r["stay_minutes"] for r in combo)>540: continue
        if sum(r["is_restaurant"] for r in combo)<2: continue
        possible.append(sum(r["adjusted_rating"] for r in combo))
    assert sum(r["adjusted_rating"] for r in result["selected"]) == pytest.approx(max(possible))


def test_csv_formula_safe():
    assert "'=cmd" in s.result_csv_bytes([{"장소명":"=cmd"}]).decode("utf-8-sig")


def test_regular_hours_require_day_confirmation():
    rows=[row(0,mandatory=True),row(1,True),row(2,True),row(3)]
    result=s.choose_places(s.enrich_candidates(rows,CTX,Fake(),"2026-10-26"),CTX)
    assert all(r["requires_hours_check"] for r in s.route_payload(result)["places"])


def test_invalid_time_and_stale_manifest(tmp_path):
    with pytest.raises(ValueError,match="HH:MM"):
        s.parse_time("09:90")
    s.mark_pending(tmp_path,"failed","test")
    import json
    assert json.loads((tmp_path/"latest.json").read_text(encoding="utf-8"))["status"]=="failed"


def test_csv_coordinates_export_as_json_numbers():
    rows=[row(0,mandatory=True),row(1,True),row(2,True),row(3)]
    for r in rows:
        r["위도"],r["경도"]=str(r["위도"]),str(r["경도"])
    result=s.choose_places(s.enrich_candidates(rows,CTX,Fake(),"2026-10-26"),CTX)
    assert all(isinstance(r["위도"],float) for r in result["selected"])
    assert all(isinstance(r["lat"],float) and isinstance(r["lng"],float) for r in s.route_payload(result)["places"])
