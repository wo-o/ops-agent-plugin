# Cloudflare 관리 zone 사전 확인과 잘못된 FQDN 원복

## 문제 유형

`<env>-dns` surface는 하나의 Cloudflare zone에 종속된다. 요청한 FQDN이 그 zone 밖인데도 `dns_records`의 `name`으로 그대로 넣으면, provider가 이를 절대 이름으로 취급하지 않고 관리 zone을 뒤에 붙여 `<requested-fqdn>.<managed-zone>` 레코드를 만들 수 있다. Terraform plan/apply가 성공해도 사용자가 요청한 DNS는 전혀 연결되지 않는다.

## PR 전 사전 확인

1. `ops_cloudflare_list_dns_records(name_contains=<정규화한 요청 FQDN>)`를 호출해 같은 한 번의
   조회에서 기존 동일 이름 레코드 충돌 여부와 응답의 **`zone_name`**(관리 zone의 실제 apex
   도메인, 예: `example.net`)을 함께 확인한다. 정확한 FQDN 필터 결과가 비어 있어도 응답의
   `zone_name`은 관리 zone 사전 확인 근거로 사용할 수 있으므로, zone 확인만을 위한 무필터
   추가 조회를 반복하지 않는다. 이 값이 tfvars `name`의 근거다 — 프롬프트에 적힌
   `example.com` 같은 placeholder zone을 그대로 쓰지 않는다.
2. 요청 FQDN의 zone suffix가 `zone_name`과 다르면(placeholder이거나 외부 zone),
   **호스트 라벨만 떼어 `zone_name`에 붙인다** — 예: `zone_name=example.net`이고 요청이
   `app-dev.example.com`이면 `app-dev.example.net`으로 등록한다. `name`은 항상
   `<호스트 라벨>.<zone_name>` 형태여야 한다. 치환 사실과 실제 생성될 FQDN을 보고·PR
   본문에 한 줄 명시한다(안전·가역 변경이라 되묻지 않는다).
3. 요청이 명시적으로 외부 zone 자체의 관리를 요구하는 맥락(그 zone의 .tf를 새로 만들라
   등)이면 치환하지 말고, 해당 zone 관리자가 ALB DNS를 CNAME content로 등록하거나
   사람이 IaC에 새 zone surface를 추가해야 한다고 안내한다.
4. `zone_name` 하위 FQDN(치환 결과 포함)만 `name`으로 넣어 PR을 연다. `zone_name`이
   `null`(토큰·zone 미설정)이면 추측하지 말고 그 사실을 보고한다.

## apply 후 검증

검증 대상을 먼저 둘로 구분한다.

- `original_fqdn`: 사용자가 처음 요청한 이름(예: `app-dev.example.com`)
- `effective_fqdn`: 관리 zone 치환 후 실제 PR에 넣은 이름(예: `app-dev.example.net`)

zone 치환이 발생했다면 Cloudflare·DNS·HTTP 검증에는 항상 `effective_fqdn`을 사용한다. 원 요청 이름의 NXDOMAIN이나 다른 응답은 관리 zone 변경의 성공·실패 근거가 아니다.

- `ops_cloudflare_list_dns_records(name_contains=<effective_fqdn>)`에서 `records[].name == effective_fqdn`인지 **완전 일치**로 확인한다. 부분 문자열 일치만으로 성공 처리하지 않는다.
- `records[].content`가 의도한 ALB DNS와 동일하고 `proxied`가 요청값과 같은지 확인한다.
- Cloudflare API의 DNS 레코드 응답에서 `ttl=1`은 자동 TTL을 뜻하는 sentinel이다. 이를 1초 TTL로 해석하거나 보고하지 않는다. 사용자가 TTL을 묻지 않았다면 검증 보고는 name/type/content/proxied 일치에 집중한다.
- `dig +short CNAME <effective_fqdn>` 결과가 ALB DNS인지 확인한다.
- HTTP 요청이면 `curl -i http://<effective_fqdn>/healthz`의 HTTP 200까지 확인한다.
- 최종 verdict와 접속 URL에는 `effective_fqdn`만 사용하고, 별도 요청 줄에 `original_fqdn → effective_fqdn` 치환을 명시한다.

## 잘못 생성됐을 때

1. apply 성공을 요청 성공으로 보고하지 않는다.
2. 생성된 실제 레코드 이름과 요청 FQDN의 차이를 기록한다.
3. 기존 상태가 명확하고 이번 PR이 만든 entry라면 같은 `<env>-dns` surface에서 해당 `entry_key`를 `remove_entry`로 즉시 원복한다.
4. prod 원복 PR이 승인 대기라면 원복을 완료·진행 중으로 표현하지 않는다. Cloudflare read 조회로 잘못된 레코드가 아직 live인지 다시 확인하고, `잘못된 레코드가 현재 남아 있음 · 원복은 code owner 승인 대기`를 첫 verdict와 타임라인에 명시한다. 승인 우회를 위해 직접 mutation하지 않는다.
5. 원복 PR이 merge된 뒤 apply 성공까지 폴링하고, Cloudflare read 조회에서 해당 정확한 이름이 사라졌는지 확인한다. 잘못된 이름은 관리 zone 안의 실제 FQDN이므로 필요하면 그 이름으로 `dig`도 확인한다. 요청했던 zone 밖 FQDN의 NXDOMAIN만으로 원복 성공을 판정하지 않는다.
6. 최종 보고는 원 요청 실패와 원복 상태를 분리한다. 요청한 외부 zone 변경은 현재 surface 범위 밖이며, 해당 zone 관리자 또는 사람이 IaC를 확장해야 한다고 안내한다.

## 핵심 판정

- `guard success`와 `tf-apply success`는 DNS 이름이 올바르다는 증거가 아니다.
- `name_contains` 결과는 부분 일치일 수 있으므로 `records[].name == effective_fqdn`을 반드시 대조한다.
- zone 치환 시 검증·접속에는 `effective_fqdn`을 쓰고, `original_fqdn → effective_fqdn`을 보고한다.
- 외부 zone FQDN을 그대로 넣지 않는다 — 관리 zone으로 치환하고 치환 사실을 명시한다.