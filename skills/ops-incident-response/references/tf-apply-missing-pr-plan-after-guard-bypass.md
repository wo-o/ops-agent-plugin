# tf-apply `Plan not found on PR` — PR guard 실패 후 머지된 경우

## 트리거

`dflook/terraform-apply`가 다음 메시지로 실제 apply 전에 중단된다.

```text
Plan not found on PR
Generate the plan first using the dflook/terraform-plan action.
```

이 오류를 단순 transient 실패로 보고 같은 push run을 재실행하지 않는다. PR plan이 애초에 생성되지 않았다면 동일 run 재실행도 같은 plan 부재로 실패할 수 있다.

## 진단 순서

1. 실패 run의 root별 job과 failed log를 읽어 `terraform apply` 또는 `Apply complete!` 도달 여부를 확인한다.
2. merge commit에 연결된 원 PR을 찾고 `plan (<root>)`와 `guard` check의 conclusion을 확인한다.
3. 실패한 plan job 로그에서 plan 생성 전 guard 단계가 막았는지 확인한다. 예: disk surface의 `data_volume_size_gb 40 → 10`은 grow-only 검사에서 차단되므로 PR plan이 저장되지 않는다.
4. PR이 실패한 required check를 우회해 머지됐는지 `merged_at`, merge actor, check 결과로 구분한다. 이 경우 원인은 “state drift로 plan이 바뀜”이 아니라 “승인·저장된 PR plan 자체가 없음”이다.
5. 같은 merge commit SHA를 대상으로 한 더 최신 `tf-apply` run을 반드시 조회한다. 특히 `workflow_dispatch` 재계획 run이 이미 `queued`/`in_progress`이면 중복 재실행하지 않고 그 run만 추적한다.

## 후속 run 판정

- 최신 run의 `head_sha`가 merge commit과 같은지 확인한다.
- `completed/success`까지 폴링한 뒤 로그에서 실제 `Plan:`과 `Apply complete!`를 확인한다.
- apply 성공 뒤에는 변경된 surface를 라이브 read 도구로 검증한다. 예를 들어 WAF reset이면 ruleset이 없어졌는지, DNS reset이면 대상 record가 없어졌는지 확인한다.
- 설정에 disk 축소가 포함됐어도 현재 volume이 없다면 “EBS 축소 성공”이라고 쓰지 않는다. apply 로그의 실제 resource action과 라이브 volume 유무를 분리해 보고한다. 설정값은 향후 재프로비저닝 기본값일 수 있다.

## 안전 경계

- 실패한 PR guard를 고치기 위한 가짜 no-op tfvars PR을 만들지 않는다.
- 허용된 write surface가 아닌 GitHub Actions rerun을 에이전트가 임의로 실행하지 않는다.
- 이미 사람 또는 다른 자동화가 시작한 최신 `workflow_dispatch` run이 있으면 중복 dispatch하지 않는다.
- workflow success만으로 전체 drift 해소를 주장하지 않는다. 실제로 존재하던 리소스와 reset 대상 surface를 read로 대조한다.

## 보고 포인트

- 최초 실패: apply 미실행인지 부분 실행인지
- PR plan/guard: 어떤 사전 검사에서 왜 plan 생성이 막혔는지
- 머지 경계: failed check 상태에서 머지됐는지
- 최신 재실행: 누가 시작했는지가 아니라 동일 commit을 적용했는지와 최종 conclusion
- 실제 반영: add/change/destroy 수와 라이브 리소스 검증
- 선언값만 바뀐 항목과 실제 클라우드에서 변경된 항목의 구분
