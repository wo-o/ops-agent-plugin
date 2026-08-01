# WAF IP 차단 실행·검증 체크리스트

공유 Cloudflare zone에서 특정 source IP를 차단할 때 사용한다.

## 변경 전 확인

- PR을 열기 전에 `ops_cloudflare_list_waf_rules`로 현재 custom ruleset을 읽는다.
- 동일 IP·action·path 범위의 활성 룰이 이미 있으면 다른 `entry_key`로 중복 룰을 추가하지 않는다. 기존 룰의 description·expression·action·enabled 상태를 근거로 이미 반영됐다고 보고한다.
- 동일 IP가 다른 action 또는 더 좁은 path 조건으로만 존재하면 같은 차단으로 간주하지 않는다. 사용자 요청 범위와 기존 expression의 차이를 명시하고 요청한 desired state로 진행한다.
- 조회 결과가 비어 있으면 신규 룰 생성 경로로 진행하되, 빈 결과 자체를 변경 성공 근거로 쓰지 않는다.

## 변경 입력

- surface: `waf` (환경 비분리)
- op: `set_entry`
- entry key: `block-<IP의 점을 하이픈으로 변환>`
- entry: `{"ip":"<IP>","action":"block"}`
- 사용자가 path를 지정하지 않으면 `path`를 만들지 않는다.

## 완료 조건

1. `ops_github_open_tfvars_pr`로 PR을 연다.
2. `ops_github_get_pr_status`에서 `guard=success`와 `merged=true`를 각각 확인한다.
3. 머지 직후 `apply_runs=[]`여도 미실행으로 단정하지 않는다. GitHub Actions 인덱싱 지연일 수 있으므로 약 15초 뒤 PR 상태를 다시 읽어 run URL을 확보한다.
4. 반환된 `tf-apply` run을 `ops_github_get_workflow_run`으로 폴링해 `completed/success`를 확인한다.
5. apply 뒤 PR 상태를 다시 읽어 최종 check 결과를 갱신한다.
5. `ops_cloudflare_list_waf_rules`에서 아래 세 조건을 모두 직접 대조한다.
   - expression에 요청 IP가 정확히 포함됨
   - action이 요청값과 일치함
   - `enabled=true`
6. 최종 증거에는 read 응답에서 실제 관측한 ruleset 이름을 함께 남긴다. 이렇게 해야 PR/apply 성공과 Cloudflare의 live ruleset 반영을 감사 관점에서 구분할 수 있다. 동일 IP와 action의 활성 룰이 여러 개면 하나만 반영된 것처럼 축약하지 말고 중복 룰 수와 각 ruleset을 보고한다.

workflow 성공만으로 live WAF 반영을 추정하지 않는다. 반대로 read 응답의 generic note가 `dev-waf`처럼 환경별 surface를 제안해도 무시한다. 유효한 쓰기 surface는 항상 `waf` 하나다.

## Slack 보고 경계

- 제목은 `공유 Cloudflare zone · WAF IP 차단`처럼 실제 범위를 드러낸다.
- 사용자가 “prod WAF”라고 표현해도 prod 전용이라고 쓰지 않는다. 공유 zone의 dev/prod 모두에 적용된다고 첫 verdict와 마지막 영향 범위에 명시한다.
- path가 없으면 `경로 제한 없음 — 해당 IP의 모든 요청 차단`을 적는다.
- PR URL과 apply run URL을 모두 남긴다.
- 만료 기능이 없는 WAF 차단에는 cron 자동 회수 문구를 넣지 않는다.
- 완료 타임라인은 PR, guard, merge, apply, live rule 확인을 각각 독립 상태로 표시한다.
