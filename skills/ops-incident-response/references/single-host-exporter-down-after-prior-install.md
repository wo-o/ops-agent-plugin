# 단일 호스트 node_exporter 중단 — 기존 설치 이후 재발

## 적용 조건

- 현재 fleet의 해당 인스턴스가 `running`이고 ALB 앱 타깃은 `healthy`다.
- 같은 IP에서 과거 `node_exporter` 시계열 수집이 정상임을 확인한 이력이 있다.
- 현재 `up==0`이 한 호스트에서만 firing한다.
- 신규 fleet 전체의 에이전트 미배포 사건과 구분된다.

## 진단 주의점

Prometheus instant query는 lookback 범위의 마지막 표본을 돌려줄 수 있다. 따라서 `cpu_utilization` 결과에 영향 IP가 한 번 보였다는 사실만으로 현재 exporter가 응답 중이거나 확정적인 app soft-hang이라고 단정하지 않는다.

1. 현재 UTC 시각과 반환 표본 timestamp의 차이를 확인한다.
2. `cpu_utilization`과 `memory_used_pct`처럼 서로 다른 metric family를 함께 조회한다.
3. 영향 IP가 한 쿼리에는 있고 다른 쿼리에는 없으면 `최근까지 수집됨 · 현재 수집 단절 가능`으로 기록한다.
4. 다른 호스트의 최신 timestamp와 영향 호스트의 timestamp를 비교해 stale 표본인지 판단한다.
5. 신규 fleet no-data가 아니고 과거 수집 이력이 확실하면 기존 runbook에 따라 환경 단위 `rolling-restart`를 최대 1회만 허용한다. 이는 앱/호스트 soft-hang 완화 시도이지 `node_exporter` 프로세스 복구를 직접 보장하지 않는다.

## 직전의 다른 알람 조치와 구분

같은 호스트에 최근 `memory high`나 `CPU high` 대응으로 `rolling-restart`가 있었다면 그 사실도
mutation 게이트에서 확인한다. 다만 과거 run이 있다는 이유만으로 scrape 사건을 무기한 막지 않는다.

- 이전 run이 아직 `queued`/`in_progress`이거나 현재 ALB가 `draining`이면 새 dispatch를 금지하고 기존 run만 추적한다.
- 이전 run이 `success`로 끝나고 메트릭·ALB·원래 Grafana 알람까지 정상화된 뒤, 더 늦은 새 `active_at`에 scrape down이 시작됐다면 별도 사건으로 취급할 수 있다.
- 이 경우에도 현재 영향 IP가 CPU와 메모리 결과에서 모두 빠졌는지, peer는 최신 표본을 계속 내는지 확인해 `was-up-now-down`을 입증한다. named query가 labels를 엄격히 적용하지 않을 수 있으므로 반환된 `metric.instance` 집합을 직접 대조한다.
- 새 사건에서 허용되는 완화는 해당 환경의 `rolling-restart` 1회뿐이다. 완료 후 exporter 시계열과 Grafana가 복구되지 않으면 과거의 다른 알람 조치까지 포함해 불안정 징후로 기록하고 자동 대응을 중단한다.

## 조치 후 판정

- workflow `success`와 ALB `healthy`는 앱 포트 복귀만 증명한다.
- workflow가 막 끝난 직후의 첫 metric 조회에서는 영향 IP가 아직 빠져 있을 수 있다. 이 한 번의 부재만으로 서킷 브레이커를 즉시 발동하지 말고, 룰의 `for` 기간과 다음 평가 1회까지 기다린 뒤 metric과 Grafana 룰을 함께 다시 조회한다.
- 유예 뒤 영향 IP가 CPU·메모리 시계열에 다시 포함되고 Grafana가 `inactive`/대상 `Normal`이면 수집 복구로 판정한다. 반대로 그때도 시계열이 없거나 `Alerting`이면 실패한 완화로 판정한다.
- serial 롤링 재시작에서는 inventory 순서에 따라 알람 대상이 아닌 peer가 먼저 `draining`될 수 있다. 이는 환경 fleet 전체를 순회하는 정상 동작이므로, 특정 호스트를 잘못 재시작했다고 단정하지 말고 workflow 종료와 환경 내 모든 타깃 복귀를 확인한다.
- 동일 `rolling-restart`를 반복하거나 곧바로 `monitoring-agents`로 전환하지 않는다. 원인이 exporter 프로세스, 호스트, 네트워크, service discovery 중 어디인지 자동 경로로 특정되지 않았기 때문이다.
- 서킷 브레이커를 적용하고 §4 절차로 온콜을 페이지하며, `앱 serving 정상 · exporter 수집 미복구`를 분리해 인계한다.

## 보고 핵심

- `rolling-restart workflow 성공`
- `ALB 앱 타깃 정상`
- `영향 IP node_exporter 시계열 부재`
- `Grafana firing 지속`
- `추가 자동 조치 중단 · 온콜 인계`

이 다섯 상태를 한 줄의 “복구”로 합치지 않는다.