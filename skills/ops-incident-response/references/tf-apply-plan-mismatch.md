# tf-apply PR plan 불일치 판독

## 대표 신호

```text
Not applying the plan - it has changed from the plan on the PR
The plan on the PR must be up to date.
```

`dflook/terraform-apply`는 PR에서 만든 plan과 apply 시점의 새 plan을 비교한다. 두 plan이
다르면 실제 Terraform 오류가 없어도 apply를 거부한다. workflow 전체 `failure`만 보고
AWS API 오류나 권한 문제로 오인하지 않는다.

## 흔한 원인

- 앞선 apply가 같은 state를 변경함
- EC2 blue/green 교체가 부분 실패한 뒤 `deposed object`가 남았다가 다른 run에서 정리됨
- 병행 merge/apply로 PR plan 생성 이후 state가 전진함
- `moved` block이나 주소 key 변경이 다른 run에서 먼저 반영됨

## 판독 순서

1. matrix job별 conclusion을 확인해 실제 실패 root만 특정한다.
2. failed log에서 PR plan과 실행 plan의 마지막 `Plan: A to add, C to change, D to destroy`를
   각각 추출한다.
3. diff에 `deposed object`, `left over from a partially-failed replacement`, `has moved to`가
   있는지 확인한다.
4. 최신 workflow run 목록에서 더 새로운 commit의 apply가 진행 중인지 확인한다.
5. 최신 apply가 성공하면 해당 root의 클라우드 상태를 ops read 도구로 검증한다.

## 조치 판단

- 최신 plan 기반 run이 진행 중: 중복 rerun 금지, 해당 run을 폴링한다.
- 최신 run 성공: stale-plan 실패는 대체되었으므로 같은 run을 다시 실행하지 않는다.
- 최신 run 없음: 임의의 no-op tfvars PR이나 직접 workflow rerun을 만들지 말고 온콜에게
  재계획이 필요하다고 에스컬레이션한다.

## 서비스 교체 검증

apply 성공 뒤 새 ALB 타깃이 모두 `healthy`면 서비스 용량은 복구된 것으로 본다. 이전
타깃의 `draining`/`Target.DeregistrationInProgress`는 deregistration delay 동안의 정상
종료 단계다. 다만 새 healthy 타깃 수가 기대치보다 적으면 복구 완료로 보고하지 않는다.
