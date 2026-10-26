import json
from pathlib import Path

import requests
from streamlit.testing.v1 import AppTest

ROOT=Path(__file__).resolve().parents[1]


def prepare(tmp_path,monkeypatch):
    (tmp_path/"outputs").mkdir()
    manifest=tmp_path/"outputs/latest.json"
    manifest.write_text(json.dumps({"status":"success","run_folder":"previous"}),encoding="utf-8")
    monkeypatch.setenv("TRAVEL_OUTPUT_ROOT",str(tmp_path))
    app=AppTest.from_file(ROOT/"app.py",default_timeout=20)
    app.secrets["KAKAO_REST_API_KEY"]="test_only"
    app.run()
    assert not app.exception
    return app,manifest


def test_opening_ui_does_not_invalidate_previous_export(tmp_path,monkeypatch):
    app,manifest=prepare(tmp_path,monkeypatch)
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"]=="success"


def test_failed_new_search_does_not_claim_old_success(tmp_path,monkeypatch):
    app,manifest=prepare(tmp_path,monkeypatch)
    def failed(*args,**kwargs):
        raise requests.ConnectionError("test network failure")
    monkeypatch.setattr(requests,"get",failed)
    app.text_input[1].set_value("테스트용 고유 장소")
    app.run()
    next(b for b in app.button if b.label=="메인 여행지 검색").click().run()
    assert app.error
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"]=="stale"
