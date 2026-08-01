# tf-apply의 GitHub API 일시 장애 판독

## 대표 신호

Terraform plan 출력 뒤 실제 apply 전에 PR 탐색·댓글 처리 코드가 실패할 수 있다.

```text
Plan: 35 to add, 0 to change, 0 to destroy.
...
github_pr_comment
...
requests.exceptions.HTTPError: 503 Server Error: Service Unavailable
for url: https://api.github.com/repos/<owner>/<repo>/pulls?state=all
```

이 경우 `Plan:` 출력은 계획 계산 성공만 뜻하며 리소스 반영을 증명하지 않는다.
`github_pr_comment` 또는 `find_pr`가 PR 목록을 조회하다 실패했다면 Terraform apply가
시작되기 전에 job이 종료됐을 수 있다.

## 판독 순서

1. matrix job별 conclusion으로 실제 실패 root를 특정한다.
2. failed job 로그에서 `Plan:`과 traceback의 선후관계를 확인한다.
3. traceback이 `github_pr_comment` → `find_pr` → GitHub `/pulls` 조회에서 끝났는지 본다.
4. traceback 이후 `terraform apply`, 리소스 생성 로그, `Apply complete!`가 있는지 확인한다.
   없으면 `apply 미실행`으로 분류하며 `부분 apply`라고 추측하지 않는다.
5. 같은 workflow 또는 리포 전체에 더 최신 run이 있는지 확인한다. 최신 run이 있으면
   중복 재실행하지 말고 종료 상태를 추적한다.
6. 최신 run이 없으면 대상 환경을 ops read 도구로 조회해 현재 리소스 상태를 확인한다.
   plan의 add/change/destroy 수와 클라우드 상태를 함께 보고한다.

## 조치 판단

- GitHub API 5xx는 외부 제어면의 일시 오류일 가능성이 높지만, 원인이 일시적이라는 이유만으로
  허용되지 않은 workflow rerun을 실행하지 않는다.
- 최신 후속 run이 성공했다면 대상 root의 클라우드 상태와 ALB target health를 검증한다.
- 최신 run이 없고 workflow rerun write surface가 없다면 no-op tfvars PR을 만들지 않는다.
  현재 온콜을 조회하고 `재실행 필요 · Slack 인계 요청`으로 보고한다.
- 온콜 조회는 호출/페이지 전송 증거가 아니다. 별도 전송 근거가 없으면 `페이지 완료`라고 쓰지 않는다.

## 보고에 포함할 근거

- 실패 root와 job 이름
- plan 요약 (`A to add, C to change, D to destroy`)
- 실패 API와 HTTP 상태
- 실제 apply 시작 여부
- 더 최신 run 유무
- 대상 환경의 현재 리소스/ALB 상태
- 자동 재실행을 하지 않은 정책상 이유와 온콜 인계 상태
