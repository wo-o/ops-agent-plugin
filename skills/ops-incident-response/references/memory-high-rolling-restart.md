# instance memory high — 환경 단위 롤링 재시작 검증 패턴

단일 인스턴스의 메모리 고사용률 알람이더라도 `rolling-restart`는 해당 환경 fleet 전체를 `serial: 1`로 순회한다. 다음 순서로 처리한다.

## 1. 동일 UID의 과거 사건과 현재 사건 구분

- `ops_explain_alert`의 현재 `active_at`, firing instance IP/name을 확인한다.
- `date -u`로 현재 시각을 읽고 `session_search`에서 UID 또는 정확한 alertname을 검색한다.
- 같은 UID의 과거 대응이 검색돼도, 과거 사건이 `resolved`됐고 현재 `active_at`과 instance/fleet가 다르면 새 에피소드다. 과거 run 때문에 현재 mutation을 막지 않는다.
- 반대로 현재 `active_at`과 instance가 같고 기존 run/PR/silence가 있으면 반복 통지이므로 새 mutation을 만들지 않는다.

## 2. 대응 시작

- 현재 룰의 전체 alerts에서 firing 환경을 산정하고 `ops_get_service_health(prefix=<project-prefix>)`로 전체 fleet을 한 번 조회한다.
- PagerDuty는 이 단계에 등장하지 않는다 — 알람은 Slack 단일 통지라 incident가 자동 생성되지 않고, lifecycle write는 사람이 명시적으로 요청한 기존 incident에만 쓴다(§8).
- 해당 환경에 `rolling-restart`를 정확히 한 번 dispatch한다.
- dispatch 성공 후에만 영향 instance로 제한한 bounded Grafana silence를 만들 수 있다. comment에는 조치와 검증 후 즉시 해제 예정임을 적는다.
- silence 기간은 임의의 짧은 고정값보다 `fleet 호스트 수 × target deregistration delay + workflow 대기·검증 여유`를 기준으로 잡는다. `serial: 1`이라 2대 fleet도 각 타깃의 drain을 순차로 기다리면 10분 이상 걸릴 수 있다. 조치 중 silence가 먼저 만료되어 반복 통지가 재개되지 않도록 충분한 bounded 기간(예: 2대 fleet에 30분)을 사용하되, 복구 검증 직후에는 남은 기간과 무관하게 즉시 expire한다.

## 3. 실행 중 관찰

- 같은 workflow URL만 conclusion까지 폴링한다. 두 호스트 이상이면 정상 실행도 수 분 걸릴 수 있다.
- serial 롤링의 첫 대상은 알람을 발화한 호스트와 다를 수 있다. ALB에서 비발화 peer가 먼저 `draining`되고 이후 발화 호스트로 교대해도 환경 fleet 순회의 정상 진행 신호다. 특정 호스트 오조치로 단정하거나 중복 dispatch하지 않는다.
- target deregistration delay 때문에 각 타깃이 수 분간 `draining`일 수 있다. workflow가 계속 `in_progress`이고 다른 타깃이 `healthy`라면 짧은 간격의 반복 폴링 대신 bounded backoff로 같은 run과 ALB만 추적한다.
- workflow가 `in_progress`인 동안 ALB 상태만 보고 완료를 주장하지 않는다.

## 4. 완료 검증

workflow `success` 뒤 다음을 서로 독립적으로 확인한다.

1. `ops_get_service_health` 또는 `ops_aws_get_alb_target_health`: 영향 환경 인스턴스 모두 running, ALB 타깃 모두 healthy.
2. `ops_query_metrics(memory_used_pct)`: 원래 firing instance가 임계치 아래로 하락했고 현재 시계열이 존재함.
   - named metric 도구의 `labels`가 결과를 엄격히 필터링하지 않고 fleet 전체 시계열을 반환할 수 있다. 호출 성공이나 첫 번째 값만 보지 말고, 반환된 각 `metric.instance`를 직접 대조해 원래 firing IP의 값을 골라 검증한다.
3. `ops_explain_alert`: 룰 `inactive`, 영향 instance와 나머지 instance 모두 `Normal`.

완료 후 `Normal` alert의 `active_at`이 원래 firing 시작 시각이 아니라 정상 전환 평가 시각으로 바뀔 수 있다. 반복 에피소드 판정과 감사 기록에는 mutation 전에 확보한 firing 상태의 `active_at`을 보존해 사용하고, 복구 후 `Normal.active_at`으로 사건 시작 시각을 덮어쓰지 않는다.

ALB healthy만으로 메모리 정상화나 Grafana 해소를 대신 증명하지 않는다.

## 5. 통지 제어 종료

- Grafana가 inactive이고 메트릭·ALB 검증도 통과한 뒤 silence를 즉시 expire한다.
- 마지막으로 active silence 목록이 비었는지, Grafana가 inactive인지 read-back한다.
- 이 사건이 런북으로 완결됐으므로 페이지는 나가지 않는다 — 조치가 실패해 §4 서킷 브레이커로 넘어간 경우에만 `ops_pagerduty_page_oncall`이 등장한다.

## 보고에 포함할 근거

- 알림 당시와 조치 후 메모리 사용률
- workflow URL과 conclusion
- 영향 환경의 running 및 ALB healthy 수
- Grafana 룰/instance 상태
- silence ID와 해제 결과
