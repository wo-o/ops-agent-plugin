# rolling-restart와 서비스 철거가 겹칠 때의 판독

## 대표 상황

`rolling-restart`가 환경의 여러 호스트를 `serial: 1`로 처리하는 동안 `<env>-service`의
`service_enabled=false` 변경이 머지·apply되어 EC2와 ALB target group이 철거된다.
그 결과 앞 호스트는 정상 재시작됐지만 뒤 호스트는 drain 이후 다음과 같이 실패할 수 있다.

```text
TASK [Wait for connection draining to complete]
fatal: FAILED! rc=255
aws: [ERROR]: Waiter TargetDeregistered failed:
An error occurred (TargetGroupNotFound): Target groups '<arn>' not found
```

## 조사 순서

1. `ops_github_get_workflow_run`으로 ansible workflow의 최종 conclusion을 확인한다.
2. GitHub read-only 경로(`gh run view <run-id> --log-failed` 등)로 호스트별 실행 범위를 읽는다.
3. 같은 시각의 최신 `tf-plan`/`tf-apply` run을 조회해 `<env>-service` 철거가 병행됐는지 확인한다.
4. 철거 apply를 최종 conclusion까지 확인한다.
5. `ops_get_service_health`와 `ops_aws_get_alb_target_health`로 인스턴스 종료·ALB 부재를 검증한다.
6. 원래 Grafana 룰을 `for` 기간과 다음 평가 이후 다시 읽는다.

## 판정과 대응

- `TargetGroupNotFound`는 단순 drain timeout과 다르다. 같은 시각의 성공한 서비스 철거 apply가
  확인되면 target group 삭제와 ansible 실패의 인과관계가 성립한다.
- 호스트별로 `드레인 → 재시작 → /healthz → 재등록` 중 어디까지 갔는지 분리한다. 앞 호스트의
  성공을 fleet 전체 성공으로 확대하지 않고, 뒤 호스트가 drain까지만 갔다면 재시작·health
  check·재등록 미도달로 보고한다.
- 서비스 철거가 의도대로 성공했고 현재 인스턴스가 terminated이며 ALB가 없다면 복구 목적의
  rolling-restart 재실행이나 재프로비저닝을 하지 않는다.
- 철거 후에도 제거된 instance 라벨이 Grafana에서 계속 firing하면 서비스 장애와 모니터링
  정리 문제를 구분한다. 자동 대응을 중단하고 stale target/rule 정리를 온콜에 Slack으로
  인계한다(의도된 철거의 잔여 알람이면 §4 페이지 대상이 아니다).
- PagerDuty 조회는 호출 완료가 아니다. 페이지 전송 성공 근거가 없으면 `온콜 확인 · Slack
  인계 요청`으로 보고한다.

## 보고에 반드시 포함할 것

- ansible run과 철거 apply run URL
- 호스트별 완료 단계와 미도달 단계
- 현재 인스턴스/ALB 상태
- 알람이 계속 firing하는지
- 동일 playbook을 재실행하지 않는 이유
