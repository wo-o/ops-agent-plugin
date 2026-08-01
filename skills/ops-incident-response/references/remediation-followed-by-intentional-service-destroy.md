# 자동 완화 직후 병행 서비스 철거

## 적용 조건

- 인시던트 대응의 bounded Ansible(`rolling-restart` 등)이 `success`로 끝났다.
- ALB와 메트릭이 잠시 복구되고 기존 Grafana 에피소드도 `inactive`가 됐다.
- 검증 유예 중 또는 최종 보고 직전에 별도 요청의 `<env>-service` `service_enabled=false` PR/apply가 시작돼 리소스가 사라진다.
- 종료된 IP를 service discovery가 아직 유지해 같은 룰이 새 `active_at`으로 다시 발화할 수 있다.

## 판정 순서

1. Ansible workflow success 직후 ALB·EC2·메트릭·Grafana를 각각 확인한다.
2. `for` 기간과 다음 평가를 기다린 뒤 **최종 보고 직전** EC2 state와 ALB/Target Group을 다시 읽는다. 앞선 healthy 결과를 재사용하지 않는다.
3. 인스턴스가 `shutting-down`/`terminated`이거나 환경 ALB/Target Group이 사라졌다면 자동 완화 실패로 단정하지 않는다.
4. 같은 환경의 `service_enabled=false`, `dev-service`/`prod-service`, destroy, 최신 tf-apply를 세션 기록에서 찾고, 발견한 PR URL은 `ops_github_get_pr_status`로 현재 머지/apply 상태를 원본에서 검증한다.
5. destroy apply가 진행 중이면 그 run만 read로 추적한다. 재시작·`monitoring-agents`·재프로비저닝·상쇄 PR을 만들지 않는다.
6. apply success 후 환경 리소스가 의도대로 제거됐는지 `ops_aws_get_service`로 확인한다. prod 등 다른 환경은 별도로 정상 여부를 확인한다.
7. Grafana가 종료된 IP를 대상으로 다시 firing하면 `현재 서비스 장애`가 아니라 `철거 후 stale scrape target/service-discovery`로 분류한다. 이전 에피소드의 resolved 시각과 새 `active_at`을 분리해 기록한다.
8. stale firing에는 앱/호스트 자동 조치를 하지 않는다. 자동 대응을 중단하고 룰/서비스 디스커버리 정리를 온콜에 Slack으로 인계한다(의도된 철거의 잔여 알람이므로 §4 페이지 대상이 아니다).

## 보고 경계

- `rolling-restart success`, `일시적 수집 복구`, `병행 destroy success`, `현재 서비스 부재`, `stale Grafana firing`을 서로 다른 줄로 쓴다.
- "알람 해소"라고 닫지 않는다. 기존 에피소드가 잠시 해소됐더라도 최종 라이브 상태가 firing이면 `자동 대응 중단 · 온콜 인계`가 최종 verdict다.
- Ansible이 서비스를 삭제했다고 쓰지 않는다. 검증된 `<env>-service` PR/apply를 철거 원인으로 제시한다.
