# 경로 계산에 연결하기

이번 전달본은 폴더 맨 위의 [final_places.csv](final_places.csv)와 [route_input.json](route_input.json)입니다. 조회 시각과 여행 조건은 [handoff_manifest.json](handoff_manifest.json)에 기록되어 있습니다.

프로그램을 새로 실행했을 때의 결과 확인 방법은 다음과 같습니다.

`outputs/latest.json`의 `status`가 `success`인 경우에만 `run_folder` 안의 파일을 사용합니다. 새 조회가 실패하면 `failed`, 조건이 바뀌면 `stale`이 됩니다. 이전 실행 폴더는 남아 있어도 최신 결과가 아닙니다.

`route_input.json`의 `trip`은 여행 날짜·시작/종료 시각·교통수단·반경을 담고, `places`는 최종 장소 목록입니다.

| 필드 | 의미 |
| --- | --- |
| `place_id` | 카카오 ID, 문자열로 유지 |
| `google_place_id` | 연결한 Google ID, 카카오 ID와 구분 |
| `name`, `lat`, `lng` | 장소명·위도·경도. 후보 단계의 카카오 값 |
| `mandatory` | 메인·사용자 필수·필수 식당 여부 |
| `meal_required` | 점심·저녁용으로 반드시 방문할 식당 2곳 |
| `meal_options` | 이 식당에 배치 가능한 `lunch`, `dinner` 목록 |
| `stay_minutes` | 체류시간. 식당 60분, 나머지는 입력값 |
| `opening_intervals` | 여행일 영업 구간. 자정부터 분 단위, `[[660,840],[1020,1200]]`이면 11~14시, 17~20시 |
| `visit_start_windows` | 도착 가능 구간. 체류시간을 이미 뺀 값으로 양 끝 포함 |
| `meal_start_windows` | 점심·저녁 시간대별 도착 가능 구간. 체류시간을 이미 뺀 값 |
| `hours_source` | `currentOpeningHours`: 조회일 포함 7일 내 날짜 정보 / `regularOpeningHours`: 평소 요일 정보 / `unknown`: 미확인 |
| `requires_hours_check` | 영업시간이 없거나 평소 요일별 정보여서 방문일에 재확인이 필요한지 |
| `selection_reason` | 선정 근거 |

`null`은 영업시간 미확인입니다. 빈 배열 `[]`은 이용 가능한 구간이 없다는 뜻입니다. `null`을 24시간 영업으로 바꾸면 안 됩니다. 운영시간의 중간 공백은 방문할 수 없는 구간으로 처리합니다.

식당 두 곳에 점심·저녁을 하나씩 배정해야 합니다. `meal_assignment_example`은 가능성을 확인한 예시이며 고정 순서가 아닙니다. 두 식당 모두 양쪽 시간대가 가능하면 이동시간에 맞춰 바꿀 수 있습니다. 식당 두 곳을 모두 점심에 배치하거나 한 곳을 두 번 방문하는 것으로 대체하지 않습니다.

선정 단계에서는 각 장소가 영업시간 안에 체류 가능한지와 체류시간 합계를 확인합니다. 전체 경로가 가능한지는 아직 계산하지 않았으므로 `route_verified`는 `false`입니다. 다음 단계에서 이동시간을 더하고, 여러 영업 구간·식사 구간을 적용해 일정 전체의 실행 가능성을 검증해야 합니다. 불가능하면 장소를 조용히 삭제하지 말고 재선정 또는 일정 변경을 요청합니다.

화면의 표 순서와 CSV 행 순서는 방문 순서가 아닙니다. 원본 Google 평점은 `google_rating`, 자체 보정값은 `adjusted_rating`으로 구분합니다. Google 자료를 지도에 표시한다면 Google Maps의 표시·출처 조건을 확인해 주세요. 이 앱은 Google 장소 링크와 표로 결과를 제공합니다.
