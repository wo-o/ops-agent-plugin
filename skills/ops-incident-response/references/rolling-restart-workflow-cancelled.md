# rolling-restart workflow가 중간 취소된 경우

## 적용 조건

`ansible-ops`의 `rolling-restart`가 `failure`가 아니라 `cancelled`로 종료되었고, serial fleet 처리 중 일부 호스트만 완료됐을 가능성이 있을 때 적용한다.

## 판독 순서

1. workflow conclusion만 보고 전체 재시작 성공/실패를 단정하지 않는다.
2. 허용된 GitHub read-only 경로로 job과 step 상태를 확인한다. `ansible-playbook 실행 (env 한정)`이 취소됐으면 해당 step 로그에서 호스트별 마지막 task를 추출한다.
3. 로그의 다음 경계를 호스트별로 기록한다.
   - `Deregister from the target group` / `Wait for connection draining`까지만 도달: 재시작·health check·재등록 미도달
   - `Restart the app service` 도달: 앱 재시작 시도됨
   - `Wait for local health` 통과: 로컬 앱 응답 복구
   - `Register back to the target group` 및 `Wait until healthy` 통과: ALB 복귀
   - debug의 `drained → restart → healthy`: 해당 호스트만 완료
4. workflow 종료 직후 EC2와 ALB를 다시 읽는다. 취소된 호스트가 `draining / Target.DeregistrationInProgress`이면 기존 프로세스가 살아 있어도 트래픽 대상 복귀는 완료되지 않은 것이다.
5. Grafana 룰을 별도로 재조회한다. ALB healthy 또는 일부 호스트 재시작 성공은 `node_exporter:9100` scrape 복구 근거가 아니다.
6. 동일 playbook을 자동 재실행하지 않는다. 부분 실행 상태에서 재실행하면 이미 처리된 호스트를 다시 건드리고 현재 draining 상태를 악화시킬 수 있다. 서킷 브레이커를 적용하고 §4 절차로 온콜을 페이지한다(도구 미노출 폴백 시 온콜 조회 후 Slack 인계 요청).

## 보고에 반드시 포함할 항목

- workflow: `cancelled`
- 호스트별 완료 지점과 미도달 단계
- 현재 EC2 running 수
- 현재 ALB healthy/draining 수와 대상
- Grafana firing 인스턴스 수
- 동일 playbook 재실행 중단 사실
- 온콜 조회는 담당자 확인일 뿐 페이지 전송 완료가 아니라는 경계

## 핵심 판정

- 첫 호스트가 `drained → restart → healthy`를 완료했어도 fleet 전체 성공이 아니다.
- 다음 호스트가 drain 대기 중 취소되면 재시작·재등록이 실행되지 않았을 수 있다.
- `cancelled`는 timeout이나 Ansible 실패와 동일하지 않지만, 라이브 상태가 부분 변경됐다는 점에서는 즉시 검증과 사람 인계가 필요하다.
