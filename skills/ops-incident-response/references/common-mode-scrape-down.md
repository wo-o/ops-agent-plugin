# 여러 환경·여러 호스트의 scrape down 동시 발화 판독

## 신호

같은 `[monitoring] instance scrape down (up==0)` 룰에서 dev/prod의 여러 호스트가 동시에 또는 넓은 범위로 firing하지만, 인스턴스는 `running`이고 앱 ALB 타깃은 `healthy`일 수 있다.

이 조합은 개별 앱 프로세스 장애뿐 아니라 다음 공통 원인도 시사한다.

- 각 호스트의 `node_exporter:9100` 서비스 또는 방화벽 문제
- Prometheus에서 대상 서브넷으로 가는 공통 네트워크·서비스 디스커버리 문제
- exporter를 관리하지 않는 앱 전용 rolling restart의 한계

## 안전한 대응 순서

1. `ops_explain_alert`에 Grafana URL의 rule UID를 넣어 룰을 찾지 못하면, 알림 본문에 표시된 정확한 룰 이름으로 한 번 다시 조회한다. 실패 응답의 known-rules 목록이 있으면 그 문자열을 그대로 사용한다.
2. 환경별 `ops_get_service_health`로 인스턴스 `running`과 ALB 앱 타깃 상태를 확인하고, 현재 인스턴스의 launch 시각을 alert `active_at`과 대조한다.
3. **변경 전에 반드시 no-data와 was-up-now-down을 구분한다.** 영향 환경마다 `ops_query_metrics(cpu_utilization)` 또는 `memory_used_pct`를 조회하고, 반환된 `metric.instance`를 현재 fleet IP와 직접 대조한다. 모든 현재 IP의 시계열이 비어 있고 인스턴스가 알람 직전 새로 기동됐다면 soft-hang이 아니라 신규 fleet의 `node_exporter` 미배포로 판정한다. 이때 `rolling-restart`는 실행하지 않고 `monitoring-agents`를 사용한다. 최근 시계열이 실제로 존재하다가 `up`만 0이 된 경우에만 soft-hang 판별용 `rolling-restart`를 고려한다.
4. 여러 환경이 함께 firing이면 dev/prod를 동시에 변경하지 않는다. 3단계 판정에 맞는 playbook(`monitoring-agents` 또는 `rolling-restart`)을 먼저 dev에만 환경 단위 최대 한 번 실행한다. 한 workflow가 환경 fleet 전체를 처리하므로 호스트별로 dispatch하지 않는다.
5. dev workflow 종료 후 ALB와 메트릭을 독립 검증하고, 룰의 `for` 기간과 다음 평가 1회를 지난 뒤 `ops_explain_alert`로 dev scrape 복구를 확인한다. `monitoring-agents` 경로에서는 현재 dev IP 전체가 CPU 또는 memory 결과에 나타나야 하며, 종료된 구 IP만 반환되면 복구가 아니다. dev에서 명확히 복구된 경우에만 prod에 동일 playbook을 환경 단위 한 번 고려한다. dev가 미복구면 prod를 변경하지 않고 공통 원인으로 에스컬레이션한다.
   - dev만 먼저 복구한 중간 단계에서는 prod 인스턴스 때문에 룰 최상위 `state`가 계속 `firing`일 수 있다. 이 값을 dev 실패로 오판하지 말고 `alerts[]`의 환경별 상태를 분리한다. 현재 dev IP 전체가 named metric 결과에 존재하고 dev 항목이 모두 `Normal`이며 ALB가 healthy라면 dev 검증은 통과한 것이므로 prod 1회 배포를 진행할 수 있다. 반대로 최상위 룰이 `inactive`여도 현재 dev IP 시계열이 없으면 복구로 인정하지 않는다.
   - 2-host serial rolling restart는 드레인·복귀 대기 때문에 10분 이상 `in_progress`일 수 있다. 초반에는 약 15~30초, 이후에는 60~120초로 폴링 간격을 늘리고, 종료 conclusion 전에는 hang이나 실패로 단정하지 않는다.
   - workflow 실행 시간이 이미 Grafana 룰의 `for` 기간과 다음 평가 1회를 충분히 넘겼다면, 완료 직후의 alert 재조회로 유예 조건을 충족한 것으로 볼 수 있다. 기계적으로 추가 대기하지 않는다.
6. prod 후속 mutation 직전에는 같은 `active_at`을 다루는 다른 Slack 세션을 다시 검색하고 최신 message까지 scroll한다.
   - 다른 세션이 반복 통지를 받아 룰·메트릭·health를 읽는 중이라는 사실만으로 prod 후속 조치를 막지는 않는다. 중복 방지 게이트의 차단 근거는 prod run URL, prod dispatch, PR, silence, PagerDuty lifecycle write, 후속 cron처럼 **실제 mutation이 시작된 증거**다.
   - 다른 세션에서 mutation 증거가 보이면 새 dispatch를 만들지 말고 그 URL·ID만 추적한다. read-only 조사나 스킬 로드만 보이면, dev workflow 성공·현재 dev IP 전체의 시계열 생성·dev alert `Normal`·ALB healthy가 모두 충족된 경우 기존 오케스트레이션 세션이 prod 1회를 계속 진행할 수 있다.
   - 검색 결과의 bookend나 짧은 snippet만으로 판단하지 않는다. 동시 세션은 도구 호출이 순차 저장될 수 있으므로 가장 최근 message id를 기준으로 scroll하여 mutation 유무를 확인한다.
   - scroll 결과에서는 assistant의 `tool_calls`뿐 아니라 뒤따르는 tool-role 응답의 `dispatched`, `run_url`, `incident id`, `silence_id`, `job_id`까지 확인한다. 호출만 저장되고 응답이 아직 없으면 접수 여부가 불명확하므로 새 mutation을 시작하지 말고 같은 세션을 최신 message id로 한 번 더 읽는다.
   - scroll payload가 너무 커서 잘리거나 별도 파일로 저장되면, 잘린 preview만 보고 "mutation 없음"으로 결론내리지 않는다. 더 작은 window로 최근 message를 다시 읽거나 구조화된 메시지에서 tool 이름·인자와 tool 응답의 URL/ID만 추출한다. 이미 prod run URL이 확인되면 어느 세션이 dispatch했는지와 무관하게 그 run을 인계받아 conclusion과 메트릭·Grafana 상태를 read-only로 끝까지 검증한다.
7. 이미 여러 환경 workflow가 dispatch된 상태라면 self-hosted runner 또는 workflow concurrency 때문에 하나가 `queued`일 수 있다. queued를 실패로 간주하거나 중복 dispatch하지 말고 선행 run 종료 후 후속 run의 실제 conclusion까지 폴링한다.
   - 대기열이 길어 현재 응답을 닫아야 하면 동일 run만 추적하는 **1회성 continuable 후속 작업**을 예약할 수 있다. 후속 작업에는 run URL, 영향 IP, 이미 실행한 환경·playbook, prod 보류 조건, 성공 후 검증 순서를 모두 넣는다. 반복 알림을 만드는 recurring 작업이나 새 dispatch를 허용하는 모호한 프롬프트는 금지한다.
8. workflow가 success여도 `node_exporter`가 재시작됐다는 뜻은 아니다. ALB healthy는 앱 포트 복귀만 증명하므로 현재 fleet 시계열과 Grafana 룰을 별도로 확인한다.
9. 모든 호스트 또는 여러 환경에서 계속 firing이면 공통 모니터링/exporter 경로 문제 가능성을 우선 보고, 원인에 관계없이 같은 playbook을 반복하지 않는다. 온콜을 조회해 사람에게 인계한다.

## 발화 시각과 인스턴스 수명으로 공통 원인 강화하기

여러 환경이 현재 함께 firing이라고 해서 반드시 같은 시각에 시작한 것은 아니다. `active_at`과 인스턴스 `launched` 시각을 함께 비교한다.

- 기존 dev 호스트가 장시간 firing인 상태에서 새로 생성된 prod 호스트도 기동 직후 같은 룰로 firing하면, 개별 앱 soft hang보다 exporter 미설치·미기동, 공통 방화벽/라우팅, Prometheus 서비스 디스커버리 같은 계층적 원인을 더 강하게 의심한다.
- 특히 환경마다 모든 호스트가 firing하면서 인스턴스는 `running`, 앱 ALB는 모두 `healthy`라면 앱 재시작의 기대효과는 낮다. 현재 fleet에 CPU/memory 시계열이 없고 기동 직후라면 `monitoring-agents` 미배포를 우선하며, 최근 시계열이 있었던 경우에만 dev `rolling-restart` 1회로 soft-hang 가설을 검증한다. 어느 경로든 dev가 Grafana 유예 시간 뒤에도 firing이면 그 결과를 반증 근거로 기록하고 prod 조치를 생략한다.
- 에스컬레이션에는 단순히 "여러 대 firing"만 쓰지 말고 환경별 최초 `active_at`, 인스턴스 기동 시각, 앱 ALB 상태, dev 1회 조치 후에도 남은 firing 인스턴스 목록을 함께 적는다. 이 정보가 bootstrap 문제와 런타임 장애를 가르는 데 유용하다.

## 부분 실패 해석

serial rolling restart에서 첫 호스트는 성공하고 다음 호스트가 ALB drain timeout으로 실패할 수 있다. 이때:

- failed 로그와 `PLAY RECAP`으로 호스트별 실행 범위를 분리한다.
- 재시작 단계에 도달하지 않은 호스트를 성공으로 쓰지 않는다.
- 현재 ALB가 다시 healthy여도 exporter 복구나 재시작 성공으로 확대 해석하지 않는다.
- 다른 환경의 독립 workflow가 계속 실행 중이면 그 conclusion까지 확인하되, 실패한 환경의 playbook은 재실행하지 않는다.

## dev 판별 조치가 반증으로 끝난 경우의 종료 보고

dev `rolling-restart` workflow가 성공하고 ALB 타깃도 모두 `healthy`로 복귀했지만, 충분한 재평가 유예 뒤에도 dev의 같은 scrape 알람이 그대로 `firing`이면 그 성공은 복구가 아니라 **앱 재시작으로 해결되는 soft hang 가설의 반증**이다. 이때는 다음을 한 묶음으로 보고한다.

- 알람 확인: dev/prod의 현재 firing 인스턴스 수와 이름
- 앱 경로 확인: 환경별 인스턴스 `running`, ALB `healthy/total`
- 판별 조치: dev 환경 단위 `rolling-restart` 1회와 workflow conclusion
- 독립 검증: dev ALB 복귀와 Grafana 룰의 지속 firing을 서로 다른 줄로 기록
- 변경 중단: prod 재시작은 `실행하지 않음`, 같은 dev 재시작도 반복하지 않음
- 원인 방향: exporter 미기동, 호스트 방화벽, Prometheus 네트워크/서비스 디스커버리 등 공통 경로 우선
- 인계 상태: 에스컬레이션은 §4대로 `ops_pagerduty_page_oncall`(전송 성공 응답 후에만 `페이지 완료`). 도구 미노출 폴백에서는 `ops_pagerduty_get_oncall` 조회만으로 `페이지 완료`라고 쓰지 않고 `온콜 확인 · Slack 인계 요청`으로 표기

권장 타임라인 골격:

```
📟 미해소 — 자동 대응 중단, 온콜 페이지 완료 (dedup_key=<uid:active_at>)

🚨 dev/prod · node_exporter scrape 장애
① 알람 확인 .......... ✅ <firing 수>
② 인스턴스·ALB ....... ✅ running · healthy
③ dev 판별 재시작 ..... ✅ workflow success
④ dev 타깃 복귀 ....... ✅ ALB healthy
⑤ 알람 재확인 ......... ❌ 계속 firing
⑥ prod 재시작 ......... ⚫ 실행하지 않음
⑦ 자동 대응 ........... 🕓 서킷 브레이커 · 추가 조치 중단
⑧ 온콜 페이지 ......... 📟 페이지 완료 (폴백 시: 🕓 온콜 확인 · Slack 인계 요청)
```

`④`와 `⑤`를 합치지 않는다. 앱 서비스 복귀와 exporter scrape 복구는 서로 다른 검증 대상이다.
