"""Chrome에서 실제 검색·리뷰 조회·다운로드를 확인한다."""
import json
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
out=ROOT/"artifacts"
out.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(channel="chrome",headless=True)
    page=browser.new_page(viewport={"width":1440,"height":1000},device_scale_factor=1)
    page.set_default_timeout(300000)
    page.goto("http://127.0.0.1:8520",wait_until="domcontentloaded")
    page.get_by_role("button",name="메인 여행지 검색",exact=True).click()
    page.get_by_role("button",name="여행 장소 추천",exact=True).click()
    page.get_by_role("heading",name="최종 방문 장소",exact=True).wait_for()
    assert page.locator('[data-testid="stException"]').count()==0
    with page.expect_download() as d:
        page.get_by_role("button",name="경로 입력 JSON",exact=True).click()
    d.value.save_as(out/"download_route_input.json")
    route=json.loads((out/"download_route_input.json").read_text(encoding="utf-8"))
    assert len(route["places"])==route["trip"]["target_count"]
    assert sum(r["meal_required"] for r in route["places"])==2
    assert len({r["google_place_id"] for r in route["places"]})==len(route["places"])
    page.get_by_role("heading",name="최종 방문 장소",exact=True).scroll_into_view_if_needed()
    page.wait_for_timeout(1800)
    page.screenshot(path=str(out/"final_places.png"),full_page=True)
    page.get_by_text("선정 장소 리뷰 확인",exact=True).click()
    page.get_by_role("button",name="경복궁 리뷰 보기",exact=True).click()
    page.wait_for_function("""() => document.body.innerText.includes('제공된 리뷰 본문이 없습니다.') || [...document.querySelectorAll('a')].some(a => a.textContent.trim() === '작성자')""")
    assert page.locator('[data-testid="stException"]').count()==0
    has_reviews=page.get_by_role("link",name="작성자",exact=True).count()>0
    report={"status":"passed","browser":"Chrome","selected":[r["name"] for r in route["places"]],"restaurant_quota":2,"download_verified":True,"review_response":"authors_shown" if has_reviews else "no_reviews_returned"}
    (out/"browser_check.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    browser.close()
