# 단일 환경 전체 호스트의 exporter scrape down 대응

## 적용 조건

같은 환경의 여러 호스트가 동시에 `node_exporter:9100` scrape down이고 다음 신호가 함께 보일 때 적용한다.

- EC2 인스턴스는 모두 `running`
- ALB 앱 타깃은 모두 `healthy`
- Grafana 룰의 전체 firing 배열이 그 환경의 모든 현재 호스트를 포함

이 조합은 앱 포트 장애보다 공통 `node_exporter` 프로세스, 호스트 모니터링 경로, 방화벽·서비스 디스커버리 문제 가능성을 높인다. ALB `/healthz`와 exporter scrape는 서로 다른 신호이므로 하나의 정상으로 다른 하나의 복구를 주장하지 않는다.

## 먼저: no-data(에이전트 미배포) vs down 구분 — rolling-restart 전 필수

`up==0`(scrape down)은 두 가지가 섞여 있고, `rolling-restart`는 그중 하나에만 유효하다.

- (a) **no-data / 미배포** — `node_exporter`가 한 번도 리포트한 적 없다. 없는 exporter는 재시작으로 살릴 수 없다.
- (b) **was-up-now-down** — 리포트하다 멈춘 soft-hang. 이때만 `rolling-restart`가 의미 있다.

`up`은 타깃이 서비스 디스커버리로 발견되기만 하면 exporter 미설치여도 값이 `0`으로 뜨는 특수 메트릭이라, `up` 값만으로는 (a)와 (b)를 못 가른다. 대신 **같은 fleet의 다른 node_exporter 메트릭**을 확인한다:

1. `ops_query_metrics(query_name="cpu_utilization")` (또는 `memory_used_pct`)를 영향 fleet 라벨로 조회한다.
2. 전 호스트에서 시계열이 비어 있으면(no-data) → node_exporter 미보고 = **(a) 미배포**다. `cpu/memory/disk` 알람이 함께 `NoData`인 것도 같은 신호다.
3. 최근 값이 있는데 `up`만 `0`이면 → exporter가 살아 있다가 scrape만 끊긴 **(b) soft-hang** 후보다.

`node_exporter`·promtail은 app user_data에 포함되지 않고 `monitoring-agents` 실습에서 설치한다(2-3 README). 서비스 프로비저닝과 app_version bump(blue-green 인스턴스 교체)마다 사라지므로, **방금 프로비저닝됐거나 `monitoring-agents`를 아직 안 돌린 fleet의 `up==0`은 정상 기대 상태**다(발화해도 장애가 아니다).

(a)로 판정되면 **`rolling-restart`를 dispatch하지 않는다.** 실제 조치는 `ops_run_ansible_playbook(playbook="monitoring-agents", environment=<env>)`로 에이전트를 (재)설치하는 것이거나, 배포 여부가 사용자 판단이면 "monitoring-agents 미배포로 판단됨 — 배포하면 해소"로 보고한다. 어느 경우든 prod fleet을 임의로 재시작하지 않는다(최소행동). 아래 순서는 **오직 (b)로 확인됐을 때만** 진행한다.

## 안전한 자동 대응 순서 (그 위 (b) soft-hang로 확인된 경우에만)

1. Grafana 룰 이름 또는 UID로 전체 firing 인스턴스를 확인한다. UID 조회가 실패하고 known-rules가 반환되면 정확한 룰 이름으로 한 번 재조회한다.
2. 영향 환경의 서비스 health를 읽어 EC2 `running`과 ALB `healthy`를 분리해 기록한다.
3. 위 (b) soft-hang로 확인됐고 완전 다운이 아니면 해당 환경 fleet에 `rolling-restart`를 한 번만 dispatch한다. no-data(a)로 판정됐으면 dispatch하지 않고 `monitoring-agents` 경로로 넘어간다. 인스턴스별 중복 실행은 금지한다.
4. workflow가 오래 `in_progress`여도 serial drain·복귀가 진행 중일 수 있으므로 conclusion까지 폴링한다. 실행 중에는 중복 dispatch하지 않는다.
5. workflow 성공 후 EC2 상태와 ALB 타깃 복귀를 다시 확인한다.
6. Grafana 룰의 `for` 기간과 다음 평가 1회를 포함해 기다린 뒤 룰을 재조회한다.
7. 계속 firing이면 같은 재시작을 반복하지 않는다. 서킷 브레이커를 적용하고 §4 절차로 온콜을 페이지한다(도구 미노출 폴백 시 온콜 조회 후 Slack 인계 요청).

## 판정 경계

- `workflow success`는 Ansible 실행 완료만 뜻한다.
- `ALB healthy`는 앱 포트 복귀만 뜻한다.
- `Grafana resolved`만 exporter scrape 복구를 증명한다.
- 온콜 조회는 담당자 확인일 뿐 페이지 또는 통보 완료가 아니다.

## 권장 보고 항목

- 영향 환경과 firing 호스트 수
- EC2 running 수와 ALB healthy 수
- 환경 단위 `rolling-restart` 실행 횟수
- workflow conclusion
- 재평가 유예 후 Grafana 상태
- 자동 대응 중단 여부와 온콜 인계 상태

재평가 후에도 전체 호스트가 firing이면 “앱 서비스는 정상이나 exporter scrape는 미복구”로 명시하고, 공통 exporter·모니터링 경로 문제 가능성을 사람에게 넘긴다.
