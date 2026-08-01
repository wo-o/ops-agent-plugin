# monitoring-agents 배포와 서비스 철거가 겹친 경우

## 징후

- `up==0`가 신규 fleet 전체에서 발화하고 CPU/memory 시계열이 비어 있어 `monitoring-agents` 미배포로 보인다.
- 최초 health 조회에서는 인스턴스와 ALB가 정상이어도, 같은 시각 `<env>-service`의 `service_enabled=false` apply가 시작될 수 있다.
- 이 상태에서 `monitoring-agents`를 dispatch하면 inventory가 갱신되는 사이 대상이 사라져 `Could not match supplied host pattern` / `--limit leaves us with no hosts to target`로 실패할 수 있다.
- 다른 환경의 배포는 성공해 해당 환경만 `Normal`로 전환될 수 있으므로 전체 성공·실패로 묶지 않는다.

## 대응 순서

1. 신규 fleet no-data를 확인한 뒤 bounded Ansible을 dispatch하기 직전에, 허용된 GitHub read-only 경로로 진행 중인 최신 `tf-apply`와 대상 root를 확인한다.
2. 같은 환경의 service destroy가 진행 중이거나 방금 머지된 것이 확인되면 그 환경에는 `monitoring-agents`를 실행하지 않는다. 철거를 되돌리거나 재프로비저닝하지 않는다.
3. 이미 dispatch했다면 workflow 결론과 failed log를 확인한다. `env_<env>` 대상 없음은 설치 실패 범위를 뜻하며, 인스턴스 종료 여부는 별도 service-health 조회로 판정한다.
4. 환경별로 분리 검증한다.
   - 존속 환경: workflow success → CPU/memory 시계열 재등장 → 해당 alert instance가 `Normal`인지 확인한다.
   - 철거 환경: tf-apply success → EC2 terminated/ALB 없음 확인 → 남은 `up==0`는 stale discovery/rule 문제로 분류한다.
5. 철거된 instance 라벨이 `for` 기간과 다음 평가 이후에도 계속 firing이면 silence나 playbook 재실행으로 숨기지 않는다. 자동 대응을 중단하고 stale target/rule 정리를 온콜에 Slack으로 인계한다(의도된 철거의 잔여 알람이면 §4 페이지 대상이 아니다).

## 보고 원칙

- `prod 복구`, `dev 철거`, `Grafana 전체 룰 firing`처럼 환경별 결과와 룰 전체 상태를 분리한다.
- ALB healthy나 Ansible success를 exporter 복구로 확대 해석하지 않는다.
- 페이지 전송 성공 응답 없이 `페이지 완료`라고 쓰지 않는다. 온콜 조회만 했으면 `온콜 확인 · Slack 인계 요청`으로 쓴다.
