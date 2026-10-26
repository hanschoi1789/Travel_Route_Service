"""최신 성공 결과와 실행 코드를 묶는다. API 키와 가상환경은 제외한다."""
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
import tomllib
import zipfile

ROOT=Path(__file__).resolve().parents[1]
manifest=json.loads((ROOT/"outputs/latest.json").read_text(encoding="utf-8"))
if manifest.get("status") != "success":
    raise SystemExit("최신 조회가 성공한 뒤 전달 파일을 만들 수 있습니다.")
run=(ROOT/"outputs"/manifest["run_folder"]).resolve()
if run.parent != (ROOT/"outputs").resolve():
    raise SystemExit("올바르지 않은 실행 폴더입니다.")
result=json.loads((run/"selection_result.json").read_text(encoding="utf-8"))
names=", ".join(r["장소명"] for r in result["selected"])
for filename in ("final_places.csv", "route_input.json"):
    shutil.copyfile(run/filename, ROOT/filename)
handoff = {
    "status": "success",
    "snapshot": True,
    "created_at": result["created_at"],
    "packaged_at": datetime.now(timezone.utc).isoformat(),
    "source_run": run.name,
    "trip": result["context"],
    "selected_count": len(result["selected"]),
    "files": {name: {"sha256": hashlib.sha256((ROOT/name).read_bytes()).hexdigest()} for name in ("final_places.csv", "route_input.json")},
    "note": "전달 시점의 결과입니다. 새 실행 후 이 스크립트를 실행하면 최신 성공 결과로 갱신합니다.",
}
(ROOT/"handoff_manifest.json").write_text(json.dumps(handoff,ensure_ascii=False,indent=2),encoding="utf-8")
guide = (
    "# 경로 계산에 사용할 파일\n\n"
    "이 폴더 맨 위의 두 파일을 사용하면 됩니다. 저장된 결과를 읽는 데에는 API 키가 필요하지 않습니다.\n\n"
    "- [final_places.csv](final_places.csv): 최종 장소·좌표·필수 여부·선정 근거\n"
    "- [route_input.json](route_input.json): 체류시간·영업 구간·점심/저녁 도착 가능 구간\n\n"
    f"조회 시각: {result['created_at']}\n\n"
    f"여행 날짜: {result['context']['trip_date']}\n\n"
    f"선정 장소: {names}\n\n"
    "식당 두 곳을 점심과 저녁에 하나씩 배치해야 합니다. `meal_assignment_example`은 가능한 배치 예시이며 방문 순서가 아닙니다. "
    "이동시간을 포함한 전체 일정의 실행 가능성은 경로 계산에서 확인해 주세요. "
    "영업시간의 `null`은 미확인이므로 24시간 영업으로 처리하지 않습니다.\n\n"
    f"영업시간 미제공 장소: {result['summary']['unknown_hours_count']}곳. "
    "세부 필드와 시간 단위는 [ROUTE_INPUT.md](ROUTE_INPUT.md)에 있습니다.\n\n"
    "이 파일은 위 조회 시각의 전달본입니다. 새로운 여행 결과를 사용하려면 앱에서 다시 검색한 뒤 "
    "`python scripts/package_handoff.py`를 실행하세요. 성공한 최신 결과로 이 두 파일과 전달용 ZIP을 갱신합니다. "
    "재조회에는 `.streamlit/secrets.toml`의 API 키가 필요합니다.\n"
)
(ROOT/"00_전달파일_안내.md").write_text(guide,encoding="utf-8")
summary=(
    "최종 장소 선정\n\n"
    "카카오에서 받은 취향별 후보에 Google 평점·평가 수·영업시간을 연결했습니다. "
    "메인 장소와 점심·저녁 식당 2곳을 포함하고, 시간 확인 여부와 평가 수를 반영한 보정 평점으로 자동 선정합니다. "
    "이전의 용도 중복·거리 감점은 최종 선정에 사용하지 않습니다.\n\n"
    f"최근 실행: {result['created_at']}\n선정 장소: {names}\n"
    f"영업시간 미제공: {result['summary']['unknown_hours_count']}곳\n\n"
    "폴더 맨 위의 final_places.csv와 route_input.json을 사용하면 됩니다. 00_전달파일_안내.md에 조회 날짜와 파일 용도를 적었습니다. "
    "카카오 좌표와 ID를 유지했고, 필수 식당 표시·체류시간·여행일 영업 구간·식사별 도착 가능 구간을 담았습니다. "
    "목록 순서는 방문 순서가 아니므로 경로 단계에서 이동시간과 식사 배치를 확인해야 합니다. "
    "시간 미확인은 null이며 24시간 영업으로 가정하지 않습니다.\n\n"
    "실행은 run.cmd, 선정 로직은 selector.py, 통합 코드는 app.py입니다. "
    "키는 압축 파일에서 제외했습니다. 재실행하려면 .streamlit/secrets.toml에 두 API 키를 설정하면 됩니다. "
    "리뷰 본문은 Google이 제공한 경우에만 별도로 확인하며, 전체 리뷰 수집이나 감성 모델 학습은 하지 않았습니다.\n"
)
(ROOT/"전달내용.txt").write_text(summary,encoding="utf-8-sig")
files=[]
for name in ("app.py","selector.py","README.md","ROUTE_INPUT.md","00_전달파일_안내.md","handoff_manifest.json","final_places.csv","route_input.json","전달내용.txt","run.cmd","run.ps1","run_console.cmd","requirements.txt","requirements-dev.txt","requirements-lock.txt",".gitignore",".gitattributes"):
    p=ROOT/name
    if p.exists(): files.append(p)
for directory in ("scripts","tests"):
    files += sorted((ROOT/directory).glob("*.py"))
files += [ROOT/".streamlit/config.toml",ROOT/".streamlit/secrets.example.toml",ROOT/"data/candidate_places.csv",ROOT/"data/trip_context.json",ROOT/"outputs/latest.json"]
files += sorted(run.glob("*.json"))+sorted(run.glob("*.csv"))
keys=tomllib.loads((ROOT/".streamlit/secrets.toml").read_text(encoding="utf-8-sig"))
secrets=[str(v).encode() for v in keys.values() if isinstance(v,str) and len(v)>15]
for p in files:
    content=p.read_bytes()
    if any(secret in content for secret in secrets):
        raise SystemExit(f"비밀 설정이 들어 있는 파일은 전달하지 않습니다: {p.name}")
target=ROOT/"REAL_Project1+2_전달용.zip"
with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as archive:
    for p in files:
        archive.write(p,"REAL_Project1+2/"+p.relative_to(ROOT).as_posix())
verification={"package":target.name,"files":len(files),"sha256":hashlib.sha256(target.read_bytes()).hexdigest(),"contains_api_key":False,"latest_run":run.name}
(ROOT/"artifacts/package_check.json").write_text(json.dumps(verification,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(verification,ensure_ascii=False))
