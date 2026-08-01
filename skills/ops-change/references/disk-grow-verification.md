# `/data` 디스크 확대 검증 예시

여러 app 인스턴스가 있는 환경에서 `<env>-disk` 변경을 검증할 때 쓰는 간결한 대조 절차다.

## 검증 순서

1. PR을 열기 전 `ops_aws_get_service(prefix="ops-agent-iac")`로 대상 환경의 현재 `running` app과 data volume 크기를 확인하면서, `ops_query_metrics(disk_usage_pct)`도 병렬 조회해 각 대상 인스턴스의 `mountpoint="/data"` 사용률을 변경 전 기준값으로 기록한다. 기준값 조회가 실패하거나 `/data` 시리즈가 없으면 작업을 막지는 않되, 이후 사용률 하락을 수치로 주장하지 않는다.
2. `<env>-disk` PR의 `guard`, merge, `tf-apply`를 각각 독립 확인한다.
3. `ops_aws_get_service(prefix="ops-agent-iac")`에서 요청 환경의 `running` app 인스턴스 ID만 추린다.
4. `volumes[].attached_to`를 인스턴스 ID와 대조해 모든 현재 app의 data volume이 목표 GiB이고 `in-use`인지 확인한다. 다른 환경 및 terminated 인스턴스의 attachment는 제외한다.
5. EBS 반영 후에만 `disk-grow` Ansible을 실행하고 workflow가 `completed/success`인지 확인한다.
6. `disk-grow` 성공 뒤 최종 독립 조회는 병렬로 수행한다: `ops_github_get_pr_status`로 최종 guard/apply 상태, `ops_aws_get_service`로 현재 running 인스턴스와 EBS attachment, `ops_query_metrics(disk_usage_pct)`로 사용률을 확인한다. 병렬 조회 결과에서도 요청 환경의 `-dev-`/`-prod-` 이름과 현재 running 인스턴스 ID만 골라 대조한다.
7. 최종 `ops_aws_get_service` 조회에서 확대 대상 app 인스턴스가 모두 계속 `running`인지도 확인한다. EBS 목표 크기와 `disk-grow` 성공이 확인돼도 대상 인스턴스가 중단·교체·누락됐다면 이를 정상 완료로 뭉개지 말고, 볼륨 확대 성공과 서비스 인스턴스 이상을 별도 상태로 보고한다. 정상인 경우 최종 보고의 인스턴스별 항목에 이름·instance ID·volume ID·`in-use`·목표 GiB를 함께 적어 대상 대조와 변경 후 회귀 확인을 한 번에 남긴다.
8. `ops_query_metrics(disk_usage_pct)`의 결과에서 `mountpoint="/data"` 시리즈만 골라 대상 인스턴스의 변경 전·후 사용률을 비교한다. `mountpoint="/"` 값은 `/data` 검증에 쓰지 않고, 최종 보고에 `/` 수치를 `/data` 수치처럼 나열하지 않는다. 변경 전 기준값을 기록하지 못했다면 현재 사용률만 보고하고 `하락` 또는 `before → after`를 쓰지 않는다. Prometheus가 긴 소수값을 반환하면 원본 판정에는 그대로 사용하되 사용자 보고에서는 변경 전·후 모두 소수점 둘째 자리로 같은 방식으로 반올림한다(예: `5.291203...% → 4.420488...%`는 `5.29% → 4.42%`). 여러 대상 인스턴스의 값이 같더라도 대상별 `/data` 시리즈 존재를 각각 확인한 뒤 공통 변화값으로 요약한다.

## 판정 경계

- **`disk-grow`는 한 번만 실행한다.** 확장 확인은 재실행이 아니라 read(EBS 크기 +
  `/data` 사용률)로 한다. playbook은 멱등이라 두 번째 run은 `changed=0`으로 무해하지만,
  "확인차 다시 돌리는" 재dispatch는 불필요하다 — read로 이미 검증되면 그걸로 끝낸다.
- `ops_run_ansible_playbook`이 동일 환경의 기존 실행을 재사용해
  `dispatched=false`, `reused_run=true`를 반환할 수 있다. 이는 실패나 미실행이 아니다.
  반환된 `run_url`의 workflow를 그대로 폴링하되, `head_sha`가 방금 반영한 PR의 merge
  commit과 일치하는지 확인한 경우에만 이번 변경의 파일시스템 확장 근거로 인정한다.
  `ops_github_get_pr_status`가 merge commit SHA를 노출하지 않는 경우에는, 해당 PR의
  `apply_runs`에서 선택해 `completed/success`까지 확인한 `tf-apply` run의 `head_sha`를
  이번 배포 커밋의 기준으로 삼고 `disk-grow` run의 `head_sha`와 정확히 대조한다. 두 SHA가
  다르면 stale run이므로 성공 근거로 쓰지 않는다. 일치하는 재사용 run이 이미
  `in_progress`이면 중복 dispatch하지 않고 한 번만 완료까지 추적한다.
  merge/push가 `disk-grow`를 자동으로 먼저 시작할 수 있으므로, 명시적 dispatch 호출에서
  `reused_run=true`가 나온 것은 정상적인 중복 방지다. 이때 `dispatched=false`를 파일시스템
  확장 미실행으로 오판하지 말고, 동일 `head_sha`의 재사용 run을 완료까지 추적해 감사 링크로 남긴다.
- EBS 목표 크기 + `in-use`: 블록 디바이스 확대 확인.
- `disk-grow` 성공: 파일시스템 확장 playbook 완료 확인.
- `mountpoint="/data"` 사용률 하락: 확장 반영의 보조 직접 증거. 대상 인스턴스의 `/data` 시리즈가 응답에 없으면 사용률 하락은 미확인으로 남긴다 — 성공 사실을 취소하지도, 사용률 하락을 추정하지도 않는다.
- `disk-grow` 성공 직후의 `/data` metric이 변경 전 값과 동일하게 남아 있을 수도 있다. 이때 metric이 반드시 즉시 변해야 한다고 가정해 playbook을 재실행하거나, 수치가 달라질 때까지 임의로 반복 조회하지 않는다. `EBS 목표 크기 + in-use`, 동일 배포 SHA의 `disk-grow` 성공, 관측된 `/data` metric을 각각 그대로 보고하고, 사용률은 `변경 전 X% → 변경 후 X%`처럼 동일 수치도 숨기지 않는다. 절대 파일시스템 크기가 필요하면 `df -h /data`를 사용자 확인 방법으로 안내한다.
- 사용자가 "거의 찼다"고 설명했지만 변경 전 `/data` metric이 낮게 나오더라도, 환경과 목표 크기가 명시됐고 EBS가 grow-only 조건을 만족하면 요청된 확대를 막지 않는다. 다만 최종 보고에서 사용자의 긴급도 표현을 관측 사실로 반복하지 말고, 실제 변경 전·후 metric을 그대로 제시한다. 관측값이 설명과 크게 다르면 `변경 전 관측 사용률은 <n>%`라고 짧게 밝혀 용량 압박이 확인된 것처럼 과장하지 않는다.
- 용량을 크게 늘렸는데 사용률 하락 폭이 작거나 예상 비율과 다르더라도 원인을 추측하지 않는다. EBS 목표 크기, `disk-grow` workflow 성공, `/data` metric은 서로 독립된 근거로 보고하고, percentage 하나만으로 파일시스템의 절대 크기나 확장 실패를 역추정하지 않는다.
  - 실측 예시: prod app 2대의 EBS를 10GiB→40GiB로 키운 뒤 두 인스턴스 모두 `in-use` 40GiB, `disk-grow` 성공이 확인됐지만 `/data` 사용률은 5.29%→4.42%로만 변했다. 이 경우에도 수치가 4분의 1이 되지 않은 이유를 추측하지 말고 세 근거를 그대로 분리한다.
  - `ops_query_metrics(disk_usage_pct)`는 비율만 보여 주므로, 이 metric과 workflow 성공만으로 `/data`의 절대 파일시스템 크기가 정확히 40GiB라고 직접 관측했다고 표현하지 않는다. 최종 verdict는 runbook 권장 문구를 쓸 수 있지만, 상세 근거에는 `EBS 40GiB`, `disk-grow` 성공, `/data 사용률`을 별도 항목으로 남긴다.
- `/data` 시리즈가 없거나 절대 파일시스템 크기를 사용자가 직접 재확인해야 할 때 최종 확인 안내: `df -h /data`.

## 권장 보고 문구

- 사용자가 `GB`라고 요청하더라도 AWS EBS의 관측 크기와 Terraform 적용 결과는 이진 단위인 `GiB`로 보고한다. 요청 요약은 `40GB 요청`, 실제 반영 근거는 `EBS 40GiB`처럼 구분할 수 있다. 필드명이 `size_gb` 또는 `data_volume_size_gb`라는 이유로 AWS 관측값을 십진 `GB`라고 단정하거나 임의 환산하지 않는다.
- 첫 verdict는 증거 수준에 맞춘다. `df` 또는 workflow의 실제 파일시스템 크기 출력으로 `/data` 절대 크기를 확인하지 못했다면 `✅ 확대 완료 — dev EC2 N대의 EBS를 <size>GiB로 확대하고 /data 파일시스템 확장 작업을 완료했습니다.`라고 쓴다. `disk-grow` 성공과 비율 metric만으로 `/data 파일시스템이 정확히 <size>GiB`라고 단정하지 않는다. 실제 `df` 출력으로 절대 크기를 확인한 경우에만 파일시스템의 정확한 크기를 명시한다.
- 변경 전 기준값이 있으면 ⑤: `✅ EBS <size>GiB 반영 · disk-grow 성공 · /data 사용률 <before>% → <after>%`
- 변경 전 기준값이 없고 사후 `/data` 시리즈만 있으면 ⑤: `✅ EBS <size>GiB 반영 · disk-grow 성공 · 현재 /data 사용률 <after>%` — 하락했다고 표현하지 않는다.
- 사후 `/data` 시리즈도 없으면: `disk_usage_pct 응답에 /data 시리즈가 없어 사용률은 직접 확인하지 못했습니다.`
- 감사 링크: PR, `tf-apply`, `ansible-ops` URL을 모두 남긴다.

인스턴스별로 이름, instance ID, volume ID, 목표 GiB, `in-use`를 열거한다. Markdown 표는 쓰지 않는다.