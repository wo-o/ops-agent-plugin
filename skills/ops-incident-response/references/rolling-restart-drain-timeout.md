# rolling-restart의 ALB deregistration timeout 판독

## 대표 증상

`rolling-restart`가 첫 호스트를 target group에서 deregister한 뒤 다음 단계에서 실패한다.

```text
TASK [Wait for connection draining to complete]
fatal: FAILED! rc=255
aws: [ERROR]: Waiter TargetDeregistered failed: Max attempts exceeded
```

워크플로가 이 지점에서 종료되면 이후의 앱 서비스 재시작·`/healthz` 확인·target group 재등록은 실행되지 않았다. `serial: 1`이면 다음 호스트도 처리되지 않았을 수 있으므로 로그의 `PLAY RECAP`과 실행된 host 목록으로 범위를 확인한다.

## 조사와 판정

1. `ops_github_get_workflow_run`으로 workflow가 `completed/failure`인지 확인한다.
2. 허용된 GitHub read-only 경로로 failed job 로그를 읽어 실제 실패 task와 `rc`를 확인한다. CLI를 쓸 수 있으면 예시는 다음과 같다.

```bash
gh run view <run-id> --repo <owner>/<repo> --log-failed
```

3. `ops_aws_get_alb_target_health`로 현재 앱 target 상태를 별도로 확인한다.
4. `ops_explain_alert`로 원래 alert를 다시 확인한다.

## 중요한 해석

- workflow 실패 뒤 target이 `healthy`여도 rolling restart 성공을 뜻하지 않는다. deregistration이 완료되지 않아 기존 앱 프로세스가 계속 serving 중일 수 있다.
- ALB `healthy`는 앱 포트(`/healthz`) 근거일 뿐 `node_exporter:9100` 복구 근거가 아니다.
- 실패 task가 restart 이전이면 보고에 `재시작 단계 미도달`을 명시한다.
- 같은 playbook을 자동으로 다시 실행하지 않는다. 원래 alert가 계속 firing이면 자동 대응을 중단하고 §4 절차로 온콜을 페이지한다.
- PagerDuty read-only 조회는 페이지 발송이 아니다. 페이지 도구 미노출 폴백에서는 `온콜 확인 · Slack 인계 요청`으로만 보고한다.
