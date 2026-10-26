"""실제 API를 사용해 Streamlit 입력부터 결과 저장까지 확인한다."""
import json
import os
from pathlib import Path
import sys
import tomllib
from streamlit.testing.v1 import AppTest

ROOT=Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
os.chdir(ROOT)
app=AppTest.from_file(ROOT/"app.py",default_timeout=300)
for k,v in tomllib.loads((ROOT/".streamlit/secrets.toml").read_text(encoding="utf-8-sig")).items():
    app.secrets[k]=v
app.run()
for label in ("메인 여행지 검색","여행 장소 추천"):
    buttons=[b for b in app.button if b.label==label]
    if not buttons:
        print(json.dumps({"step":label,"errors":[x.value for x in app.error],"exceptions":[x.message for x in app.exception]},ensure_ascii=False))
        raise SystemExit(1)
    buttons[0].click().run()
    if app.error or app.exception:
        print(json.dumps({"step":label,"errors":[x.value for x in app.error],"exceptions":[x.message for x in app.exception]},ensure_ascii=False))
        raise SystemExit(1)
assert "p2_result" in app.session_state
result=app.session_state["p2_result"]
report={"status":"passed", "summary":result["summary"], "places":[{"name":r["장소명"],"rating":r["google_rating"],"count":r["google_rating_count"],"meal":r["meal_required"],"windows":r["opening_intervals"]} for r in result["selected"]],"folder":app.session_state["result_folder"]}
# 一般 rerun must not issue another API call or create another output run.
calls=app.session_state["google_client"].calls
folder=app.session_state["result_folder"]
app.run()
assert app.session_state["google_client"].calls==calls
assert app.session_state["result_folder"]==folder
report["rerun_no_extra_calls"]=True
artifacts=ROOT/"artifacts"
artifacts.mkdir(exist_ok=True)
(artifacts/"live_check.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(report,ensure_ascii=False,indent=2))
