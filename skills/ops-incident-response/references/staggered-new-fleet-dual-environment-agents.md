# 시차 기동된 dev/prod 신규 fleet의 모니터링 에이전트 복구

## 적용 조건

- dev와 prod가 수분 차이로 새로 프로비저닝되거나 교체됨
- 현재 EC2는 모두 `running`, ALB 앱 타깃은 모두 `healthy`
- 같은 scrape-down 룰에서 먼저 뜬 환경은 `Alerting`, 뒤에 뜬 환경은 `Pending`일 수 있음
- 현재 fleet IP 전체에 CPU·메모리 시계열이 없음

이 조합은 앱 soft hang보다 신규 fleet의 `node_exporter`·`promtail` 미배포를 우선한다. 알림의 `up but unresponsive` 문구만으로 `rolling-restart`를 선택하지 않는다.

## 검증된 순서

1. 룰 API의 전체 `alerts[]`에서 `Alerting`과 `Pending`을 환경별로 분리한다. 같은 원인·시각대의 신규 fleet이면 `Pending` 환경도 common-mode 영향 범위에 포함한다.
2. 전체 프로젝트 health를 한 번 읽어 각 환경의 launch 시각, `running`, ALB `healthy`를 확인한다.
3. CPU와 memory named query의 반환 `metric.instance` 집합을 현재 네 IP와 직접 대조한다. 호출의 `labels`가 결과를 엄격히 필터링한다고 가정하지 않는다.
4. 기존 세션, 현재 IP, `active_at`, `monitoring-agents`, cron을 검색해 동일 사건의 mutation이 없는지 확인한다.
5. dev에 `monitoring-agents`를 환경 단위 한 번 dispatch하고 workflow `success`까지 폴링한다.
6. dev 현재 IP 전부에 CPU·메모리 시계열이 생기고 dev alert가 각각 `Normal`, dev ALB가 healthy인지 독립 검증한다. 최상위 룰은 prod 때문에 계속 `firing`이어도 dev 검증은 통과할 수 있다.
7. prod mutation 직전에 동시 Slack 세션을 다시 읽는다. 다른 세션의 read-only 조사만으로 차단하지 않되, prod run URL·dispatch·후속 cron 등 실제 mutation 증거가 있으면 새 dispatch를 금지한다.
8. prod에 `monitoring-agents`를 한 번 실행하고 workflow, 현재 prod IP 시계열, 각 alert `Normal`, ALB를 검증한다.
9. 룰 `inactive`까지 확인하면 완결이다. PagerDuty는 등장하지 않는다 — 알람은 Slack 단일 통지라 incident가 자동 생성되지 않는다(§8).

## 판정상 주의점

- 최초 알림이 dev 2건만 보여도 룰 API에 prod `Pending` 2건이 있으면 실제 영향 범위를 dev/prod로 확장한다.
- dev 설치 직후 CPU가 일시적으로 높거나 prod 설치 직후 80%에 가까워도, 별도 CPU 알람이 없다면 scrape 복구 과정의 관찰값일 뿐 추가 mutation 근거가 아니다.
- workflow 성공, 시계열 생성, Grafana `Normal`/`inactive`, ALB healthy는 각각 별도 근거로 기록한다.
- `monitoring-agents` 성공 후에도 현재 IP가 named metric 결과에 없으면 재실행하거나 `rolling-restart`로 전환하지 않고 자동 대응을 중단한다.

## 보고 골격

```text
✅ 해소 — 신규 dev/prod fleet에 monitoring agents를 배포해 수집 정상화

🚨 dev/prod · node_exporter scrape 장애
① 원인 판별 .......... ✅ 신규 fleet no-data · soft hang 아님
② dev agents 배포 .... ✅ workflow success
③ dev 수집 검증 ...... ✅ 현재 IP 시계열 · alert Normal
④ prod agents 배포 ... ✅ workflow success
⑤ prod 수집 검증 ..... ✅ 현재 IP 시계열 · alert Normal
⑥ 서비스 상태 ........ ✅ EC2 running · ALB healthy
⑦ 알람 해소 .......... ✅ Grafana inactive
```
