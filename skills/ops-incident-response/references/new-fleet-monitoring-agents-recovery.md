# 신규 fleet의 scrape down 복구 사례

## 증상

- Grafana 룰: `[monitoring] instance scrape down (up==0)`
- 같은 dev fleet의 새 인스턴스 2대가 동시에 firing
- EC2는 모두 `running`, ALB 앱 타깃은 모두 `healthy`
- 인스턴스 시작 직후 수분 안에 알람 발생

## 판정 근거

1. `ops_explain_alert`의 전체 `alerts` 배열로 영향 인스턴스 2대를 확정한다.
2. `ops_get_service_health`에서 새 인스턴스의 launch 시각과 ALB `healthy`를 확인한다.
3. 각 영향 인스턴스에 `cpu_utilization`을 조회한다.
4. 전체 결과가 비어 있으면 soft-hang이 아니라 새 fleet의 `node_exporter` 미배포로 판정한다.

알림 문구의 `up but unresponsive`만으로 soft-hang을 단정하지 않는다. 이 상태에서
`rolling-restart`는 원인을 해결하지 못한다.

## 조치

- 환경 단위로 `monitoring-agents`를 한 번 dispatch한다.
- GitHub Actions run이 종료될 때까지 중복 dispatch 없이 폴링한다.
- PagerDuty incident는 자동 생성되지 않는다(Slack 단일 통지) — lifecycle write는 사람이 명시적으로 요청한 기존 incident에만 쓴다(§8).

## 교체 중 구/신 fleet가 함께 보이는 경우

- 같은 `Name`에 종료된 구 IP와 현재 running 신규 IP가 함께 `Alerting`일 수 있다.
- `ops_get_service_health`에서 EC2 state, launch 시각, ALB target 상태를 읽어 IP 세대를
  분리한다. named metric query는 `labels`를 엄격히 필터링하지 않을 수 있으므로 반환된
  `metric.instance` 집합도 직접 대조한다.
- 구 IP에 과거/잔존 시계열이 있고 신규 IP만 비어 있어도, 현재 running fleet 기준으로는
  monitoring-agents 미배포 판정이다. 환경 단위 배포는 한 번만 실행한다.
- 배포 후 신규 IP가 시계열을 내고 각각 `Normal`이지만 종료된 구 IP만 계속 `Alerting`이면
  현재 fleet 복구는 성공한 것이다. 다만 룰 전체는 해소되지 않았으므로 알람 해소를
  주장하지 않는다.
- 잔여 firing은 stale scrape target/service-discovery 문제로 분류한다. 동일 playbook이나
  rolling-restart를 반복하지 않고 자동 대응을 중단한 뒤 §4 절차로 에스컬레이션한다
  (stale target/rule 정리를 인계 사유로 명시).

## 환경별 fleet가 시차를 두고 교체되는 경우

같은 Grafana UID가 직전에 다른 환경에서 해소됐더라도 새 환경의 새 fleet에서 다시 발화할 수 있다.
이를 이전 사건의 반복 통지로 잘못 막지 않는다.

1. 이전 사건과 현재 사건의 `active_at`, 환경, instance IP/name, EC2 launch 시각을 함께 비교한다.
2. 환경이나 신규 IP가 달라지고 `active_at`이 새로 찍혔으며 현재 fleet launch 직후라면 별도
   신규 에피소드로 처리한다. 같은 UID라는 이유만으로 mutation을 금지하지 않는다.
3. named metric query가 영향 환경을 필터링하지 않고 직전 환경의 정상 시계열만 반환할 수 있다.
   결과가 비어 있지 않다는 사실만 보지 말고 `metric.instance` 집합에 현재 영향 IP가 있는지
   직접 확인한다. 현재 영향 IP가 CPU와 memory 양쪽에 없으면 신규 fleet no-data 근거다.
4. 현재 환경에만 `monitoring-agents`를 한 번 실행하고, 이미 정상인 다른 환경에는 재배포나
   `rolling-restart`를 하지 않는다.

## 완료 검증

1. workflow conclusion이 `success`인지 확인한다.
2. 최소한 알람의 `for` 기간과 다음 평가 1회가 지나도록 기다린다. 설치 직후 첫 조회에서
   신규 시계열이 아직 없다는 이유만으로 즉시 실패 처리하거나 다른 playbook을 시작하지 않는다.
3. CPU 또는 memory named query가 비어 있지 않은지 확인한다.
4. 반환된 모든 `metric.instance`를 알람의 영향 인스턴스 집합과 직접 대조한다.
   `labels` 입력이 결과를 엄격히 필터링하지 않을 수 있으므로 호출 횟수나 결과 개수만
   근거로 삼지 않는다. 특히 종료된 구 fleet IP의 시계열만 반환되면 신규 fleet 복구 근거가 아니다.
5. `ops_explain_alert`에서 룰이 `inactive`, 각 인스턴스가 `Normal`인지 확인한다.
6. EC2 `running`과 ALB `healthy`는 서비스 존속 확인으로 별도 기록한다.

## workflow 성공 후에도 신규 IP가 계속 없을 때

- `monitoring-agents` workflow 성공은 패키지·서비스 배치 작업이 종료됐다는 뜻일 뿐,
  Prometheus가 신규 IP를 실제 scrape한다는 증거가 아니다.
- `for` 기간과 다음 평가 이후에도 영향 신규 IP가 CPU/memory 결과에 없으면 룰 상태와 무관하게
  수집 복구를 확정하지 않는다.
  - 룰이 계속 `firing`이면 조치 미해소다.
  - 룰이 `inactive`인데 종료된 구 fleet IP만 시계열에 남으면, 신규 scrape target이
    service-discovery에서 제거되어 `up==0` 평가 대상이 사라진 것일 수 있다. 이는 정상 수집이
    아니라 모니터링 blind spot 가능성이므로 `알람 비활성 · 수집 복구 미검증`으로 분류한다.
- 두 경우 모두 같은 `monitoring-agents`를 재실행하거나 추측으로 `rolling-restart`하지 않는다.
  자동 대응을 중단하고 현재 running fleet IP, 남아 있는 구 IP 시계열, Grafana 룰 상태를 함께
  기록해 §4 절차로 에스컬레이션한다.
- 보고에서는 `workflow success`, `현재 fleet 시계열 존재 여부`, `Grafana 룰 상태`,
  `ALB healthy`를 네 개의 독립된 상태로 남긴다.

## 보고 경계

- workflow 성공, 메트릭 수집 복구, Grafana 알람 해소를 각각 분리한다.
- ALB `healthy`만으로 exporter 복구나 알람 해소를 주장하지 않는다.
