# guard OIDC 인증 실패 진단 (AssumeRoleWithWebIdentity)

guard의 plan/apply run이 `AWS 자격증명 구성`(configure-aws-credentials) step에서
`Not authorized to perform sts:AssumeRoleWithWebIdentity` 또는 OIDC 오류로 실패할 때
따른다. 인증 단계 실패는 tfvars 값·Terraform validation 문제가 아니다 — surface 값을
바꾸는 PR이나 새 중복 PR로 대응하지 않는다.

## 진단 원칙

1. sub 형식을 지식으로 단정하지 않는다. 실제 발급되는 sub의 정본은 GitHub API다:

   ```
   gh api repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix
   ```

   2026-07-15 이후 생성·이전·개명된 리포는 `repo:<owner>@<숫자ID>/<repo>@<숫자ID>`
   immutable 형식이 강제된다(opt-out 불가). 숫자 ID는 오염이나 오타가 아니라
   정상값이다 — 제거를 지시하지 않는다. 그 이전 리포는 `repo:<owner>/<repo>` 형식일
   수 있다. 어느 쪽인지 추측하지 말고 위 조회값을 그대로 쓴다.
2. 전체 sub = prefix + suffix. suffix는 실패한 job의 workflow를 직접 읽고 결정한다:
   PR 트리거 job은 `:pull_request`, 브랜치 push job은 `:ref:refs/heads/<branch>`,
   `environment:`가 선언된 job은 `:environment:<이름>`이다.
3. aud는 정확히 `sts.amazonaws.com` 하나다. StringEquals는 완전 일치 비교이므로
   공백·괄호·URL 장식이 한 글자만 붙어도 거부된다.
4. Slack을 거친 값은 링크 변형 오염이 흔하다 — `값 (http://값)`, `<http://...|...>`
   패턴이 사용자가 붙여넣은 JSON에 보이면 채팅 표시 문제가 아니라 저장값 오염을
   의심한다. 채팅에 보이는 JSON으로 live 상태를 확정하지 말고, 사용자가 AWS에서
   다시 읽은 원문으로 확인을 요청한다:

   ```
   aws iam get-role --role-name <project>-plan \
     --query 'Role.AssumeRolePolicyDocument' --output json
   ```

   이 결과와 1의 prefix 조회값을 한 글자 단위로 대조해 보고한다.
5. OIDC 롤과 trust는 2-0-setup foundation Terraform이 관리하는 사람 소유 경로다.
   콘솔 수동 수정은 다음 foundation apply가 되돌릴 수 있다. 에이전트는 IAM을 직접
   변경하거나 workflow·setup 코드를 고치는 PR을 열지 않고, 사용자가 정본 입력을
   고쳐 human apply 하도록 안내한다.
6. 같은 설정으로 같은 step에서 같은 오류가 2회 반복되면 재실행을 멈춘다. 이후에는
   4의 live 증거를 확보한 뒤에만 기존 run의 failed jobs를 재실행하며, 새 PR·새
   run을 만들지 않는다.
7. 인증 성공은 시작일 뿐이다 — plan → guard → merge → apply → live 검증은 각각
   독립 근거로 확인하고, 인증 통과만으로 서비스 반영을 보고하지 않는다.
