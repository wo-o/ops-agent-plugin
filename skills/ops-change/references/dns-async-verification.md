# DNS 변경의 비동기 상태 대조

GitHub PR/Actions 상태와 실제 DNS 반영은 서로 독립된 증거다. 어느 한쪽으로 다른 쪽을 추정하지 않는다.

## 확인 순서

1. PR 전에는 `ops_cloudflare_list_dns_records(name_contains=<정확한 FQDN>)`를 **type 필터 없이** 조회해 관리 `zone_name`과 같은 이름의 A/AAAA/CNAME 충돌을 한 번에 확인한다. ALB 연결이면 `ops_aws_get_service`에서 이름이 `<prefix>-<env>-alb`인 항목의 `dns_name`을 CNAME content로 사용한다.
2. Slack 링크 마크업과 평문으로 같은 요청이 반복돼도 hostname·환경·목표 ALB가 같으면 하나의 desired state로 정규화해 PR을 한 번만 연다.
3. `ops_github_get_pr_status`로 PR의 `merged`와 `apply_runs`를 확인한다.
4. 머지됐지만 `apply_runs`가 비어 있으면 약 15초 간격으로 최대 2분 재조회한다.
5. apply 성공 뒤 최종 검증은 독립 항목을 한 번에 병렬 실행한다: 최종 PR/check 상태, Cloudflare 레코드, `dig` + 요청 프로토콜의 `/healthz`.
6. 최종 Cloudflare 조회도 우선 type 필터 없이 실행하고, 결과에서 `name` 완전 일치, `type`, `content`, `proxied`를 각각 대조한다. 결과가 많아 CNAME만 추가 확인해야 할 때만 `type="CNAME"` 조회를 덧붙인다.
7. 일반 CNAME이면 `dig +short CNAME <domain>`으로 권한 DNS에 노출된 값을 확인한다. 응답 끝의 `.`만 정규화하고 hostname 본문은 ALB DNS와 정확히 일치해야 한다.
8. HTTP ALB 연결 요청이면 `curl -sS -i --max-time 15 http://<domain>/healthz`로 상태행의 `HTTP 200`과 정상 본문을 함께 확인한다. `curl` exit code 0만으로 성공 처리하지 않는다.
9. HTTPS 또는 `proxied=true` 요청만 `https://` 검증을 성공 기준으로 삼는다.
10. 최종 보고에는 PR URL과 `tf-apply` run URL을 모두 남겨 구성 변경과 실행 근거를 분리한다.

## 증거를 분리해 보고하기

- ② guard(plan)은 응답의 `checks` 필드(head 커밋 check-run conclusion)로 판정한다.
  `checks`가 비어 있거나 `checks_lookup_error`가 있을 때만 `상태 미확인`이다.
- PR이 MERGED여도 apply run이 2분 내 발견되지 않으면 `apply run 미발견`이다.
- apply run 미발견이어도 Cloudflare 레코드, `dig`, `/healthz`가 모두 맞으면 `반영 확인`은 성공이다.
- 반대로 apply workflow가 성공해도 레코드가 없거나 `/healthz`가 실패하면 실제 반영 성공으로 보고하지 않는다.
- prod PR이 MERGED여도 승인 주체 정보가 없으면 `자동 머지`나 `사람 승인 완료`라고 쓰지 않고 단순히 `MERGED`라고 쓴다.
- Cloudflare API의 DNS 레코드 응답에서 `ttl: 1`은 일반적으로 `Auto` TTL을 뜻한다. 이를 `TTL 1초`로 해석하거나 보고하지 않는다. TTL이 요청의 핵심이 아니면 생략하고, 필요하면 `TTL Auto`로 표시한다.

## 권장 타임라인 예시

```text
✅ 반영 확인 — 레코드 살아 있음 · apply run 조회는 실패 (아래 참조)

① PR 열림 ........... ✅ #<number>
② guard(plan) ....... ✅ 통과
③ 머지 .............. ✅ MERGED
④ apply ............. 🕓 apply run 미발견
⑤ 반영 확인 ......... ✅ CNAME 일치 · HTTP 200
```

HTTP만 요청받았다면 HTTPS 지원을 확인하거나 성공했다고 표현하지 않는다.