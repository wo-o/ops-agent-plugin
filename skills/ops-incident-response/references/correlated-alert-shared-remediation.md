# 동일 호스트의 상관 알람과 조치 중복 방지

한 장애가 여러 Grafana 룰과 PagerDuty incident를 순차 발화시킬 수 있다. 대표적으로 앱의 CPU 소모·메모리 누수·DB connection 누수가 먼저 발생하고, 이어서 `node_exporter` scrape down이 발화한다. 각 룰의 UID와 `active_at`이 달라도 같은 호스트·시간대·원인일 수 있다.

## 판정 순서

1. 새 알람의 정확한 UID와 `active_at`을 확인한다.
2. 같은 UID뿐 아니라 영향 `instance`, `name`, 환경, 최근 15분의 관련 조치명(`rolling-restart` 등)으로 `session_search`한다.
3. ALB에 `draining` 타깃이 있으면 이미 bounded rolling 작업이 진행 중일 가능성을 우선 확인한다.
4. 기존 run URL이 발견되면 그 run만 read로 추적한다. 새 룰이 별도 CPU/메모리/scrape 알람이어도 같은 환경에 playbook을 중복 dispatch하지 않는다.
5. 로그와 메트릭을 함께 사용해 공통 원인을 기록한다. 예: `/troublemaker`의 `burned cpu`, DB 연결 누수, 그 뒤 exporter scrape 단절.

## 상태 보고 경계

- `workflow in_progress`, `ALB draining`, `Grafana firing`, `메트릭 없음`을 각각 분리한다.
- 영향 IP의 CPU 시계열이 사라졌다면 CPU 정상화로 보고하지 않는다. scrape 장애 때문에 관측 불가일 수 있다.
- ALB `healthy`는 앱 `/healthz` 복귀만 증명하며 `node_exporter`나 CPU 알람 해소를 증명하지 않는다.
- 새 PagerDuty incident가 현재 상관 알람과 정확히 일치하고 기존 완화가 진행 중이면 그 incident는 acknowledge할 수 있다. 단, 기존 incident와 새 incident의 ID·상태를 분리해 보고한다.

## 로그 알람의 `Normal (NoData)` 함정

호스트 scrape 장애와 함께 `promtail` 또는 로그 전달 경로도 끊기면, 직전에 firing이던 app 5xx/ERROR 룰이 `inactive`·`Normal (NoData)`로 바뀔 수 있다. 이는 오류가 사라졌다는 증거가 아니라 관측 신호가 사라진 결과일 수 있다.

1. 상관 scrape-down이 firing인 동안 로그 룰의 `Normal (NoData)`를 앱 복구로 판정하지 않는다.
2. 이전 Loki 로그에서 확인된 코드 결함이나 열린 수정 PR은 별도 미해결 상태로 유지한다. scrape 복구용 `rolling-restart`가 성공해도 코드 수정·배포 완료로 확대 해석하지 않는다.
3. 호스트 수집이 복구된 뒤 로그 룰을 다시 읽고, Loki 수집 재개 여부와 새 오류 유입 여부를 확인한다. `inactive`만 확인되고 로그 수집 복구가 검증되지 않으면 `알람 비활성 · 앱 오류 해소 미검증`으로 보고한다.
4. 같은 시각 새 CPU incident가 파생되면 CPU 시계열 복구와 해당 Grafana 룰 상태를 따로 확인한다. PagerDuty 자동 `resolved`는 보조 결과이며 메트릭 검증을 대신하지 않는다.

## 장시간 진행 시 후속 점검

기존 run이 즉시 끝나지 않으면 같은 run 하나만 추적하는 `repeat=1` 후속 job을 둔다. job에는 다음을 반드시 포함한다.

- 관련된 모든 alert UID/name과 영향 환경·인스턴스
- 기존 run URL과 이미 수행한 mutation
- 관련 PagerDuty incident ID 및 현재 상태
- 성공 조건: workflow success, 환경 전체 ALB healthy, 영향 IP 메트릭 복구, 모든 관련 Grafana 룰 inactive
- 실패 조건: run 실패/cancelled/장시간 진행, 메트릭 미복구, 룰 firing 지속
- 실패 시 추가 playbook 금지와 온콜 인계 경계

후속 job을 만들기 전 `cronjob(action="list")`로 같은 run 또는 UID를 추적하는 job이 없는지 확인한다.