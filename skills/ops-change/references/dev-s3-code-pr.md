# dev S3 코드 PR 체크리스트

기존 tfvars surface에 없는 dev 전용 S3 버킷을 추가할 때 사용한다.

## 범위 판정

- “S3 버킷 하나 추가”처럼 리소스 생성만 명시하면 버킷 코드만 최소 변경한다.
- “앱이 업로드하게 연결”, “업로드 기능이 동작하게”까지 요청하면 버킷만 만들고 끝내지 않는다. 앱 EC2의 IAM role/instance profile, 최소 권한 S3 policy, 앱 설정(`S3_BUCKET` 등), 필요하면 앱 코드 변경까지 별도 범위로 확인한다.
- 응답에서 버킷 생성과 애플리케이션 통합 여부를 분리해 명시한다. 버킷만 만든 경우 “앱 IAM 권한·환경변수 배선은 포함하지 않음”이라고 쓴다.

## 안전한 기본 구성

- 버킷 이름은 전역 유일하도록 `<project>-<env>-app-uploads-<account_id>`처럼 account id를 포함한다.
- `aws_s3_bucket_public_access_block` 네 옵션을 모두 `true`로 둔다.
- `aws_s3_bucket_ownership_controls`는 `BucketOwnerEnforced`를 사용한다.
- 서버 측 암호화는 최소 SSE-S3(`AES256`)를 명시한다. KMS는 키 소유·비용·권한 범위가 추가되므로 요청 없이 임의 도입하지 않는다.
- dev 서비스 전체 삭제 surface와 수명주기를 맞춰야 하면 `service_enabled`로 생성 여부를 게이트한다. `force_destroy=true`는 서비스 삭제 시 객체도 지워지는 의미이므로 코드 주석과 최종 보고에 파괴 영향을 명시한다.
- 기존 variable validation이나 비용 상한을 완화하지 않는다.

## 파일 배치와 PR

### 기존 desired state 중복 방지

- 새 파일 경로를 읽었을 때 이미 존재하면 곧바로 `ops_github_open_code_pr`를 호출하지 않는다. 현재 내용이 요청한 리소스·안전 속성·수명주기와 실질적으로 같은지 먼저 비교한다.
- 동일한 desired state가 이미 dev 브랜치에 있으면 **새 PR을 열지 않는다**. 기존 파일의 최신 커밋과 연결된 merged PR을 read-only GitHub 조회로 찾고, 그 PR의 guard/plan 및 해당 merge commit을 실행한 `tf-apply` 결과를 확인한 뒤 `이미 반영됨`으로 보고한다.
- `ops_github_open_code_pr`는 파일 전체 교체 방식이라 기존 파일과 동일한 내용을 제출해도 빈 diff PR이 만들어질 수 있다. 도구 호출 자체를 no-op 감지 수단으로 쓰지 않는다.
- 실수로 빈 diff PR이 생기면 additions/deletions/changedFiles가 모두 0인지 인증된 read-only 조회로 확인한 후 중복 PR을 닫고, 실제 반영 근거는 그 빈 PR이 아니라 원래 merged PR과 apply run에서 수집한다.
- 기존 파일이 요청과 일부만 다르면 차이를 명확히 식별한 뒤 최소 변경 내용으로 코드 PR을 연다. 단순히 파일명이 존재한다는 이유만으로 완료 처리하지 않는다.

- dev 전용 요구라면 `2-1-dev/` 아래에 독립 `.tf` 파일을 추가하면 prod 모듈에 의도치 않게 확산되는 것을 피할 수 있다.
- 기존 `main.tf`·`variables.tf` 상단에 “에이전트는 `*.tf`를 편집하지 않는다”는 사람 소유 주석이 있어도, 승인된 dev 코드 PR 경로 자체가 금지된 것은 아니다. 이 주석은 기존 사람 소유 파일을 덮어쓰지 말라는 경계로 해석한다. `ops_github_open_code_pr`가 허용하는 `2-1-dev/` 아래에 기능별 새 독립 `.tf` 파일을 추가하고, 기존 사람 소유 파일은 수정하지 않는 방식으로 최소 diff를 유지한다.
- 새 파일도 `ops_github_read_file(path, ref="dev")`로 존재 여부를 먼저 확인한다.
- S3 독립 파일을 새로 추가하는 기본 preflight는 서로 독립적인 다음 읽기를 한 번에 병렬 수행한다: `2-1-dev/` 디렉터리, 후보 파일(`2-1-dev/app-uploads-s3.tf`), `service.auto.tfvars`, `main.tf`, `variables.tf`. 이 조합으로 파일 중복, 현재 생성 게이트, root 배선, 기존 data/resource/local label 충돌, 이름·변수 규칙을 PR 전에 확인한다. 실제로 수정할 파일은 후보 독립 파일 하나로 제한하고 주변 파일은 읽기 전용 문맥으로만 사용한다.
- 사용자가 “tfvars surface로 안 되는 것을 알고 dev 브랜치에 코드로 직접 추가”라고 명시했으면 변경 경로를 다시 확인하지 않는다. 필수 비용·수명주기 값이 빠진 경우만 질문하고, 검증된 S3 템플릿의 안전 기본값으로 충분한 단순 버킷 요청은 즉시 코드 PR로 진행한다.
- `service_enabled`로 생성을 게이트하려면 `2-1-dev/service.auto.tfvars`도 먼저 읽어 현재 값을 확인한다. 현재 `true`이면 코드 PR의 merge·apply가 즉시 버킷 생성 경로를 실행하므로 apply 완료까지 추적한다. 현재 `false`이면 이번 apply는 코드만 반영하고 버킷은 만들지 않는다.
- 사용자가 “dev 브랜치에 코드로 추가”를 명시했다면 위의 지연 생성도 코드 전달 범위로 보고 진행할 수 있다. 반대로 “지금 버킷을 만들어 달라”는 live 생성 요청이면 `service_enabled=false` 게이트를 조용히 넣고 완료 처리하지 않는다. 서비스 활성화가 필요한지, 서비스와 독립된 버킷 수명주기가 필요한지 범위를 확인한다.
- `service_enabled=true`에서 apply가 성공한 경우에도 S3 전용 read 도구가 없다면 `Terraform이 버킷 생성 경로를 성공적으로 적용함`과 `S3 API로 버킷 존재·속성을 독립 확인함`을 구분한다. 전자는 완료로, 후자는 미지원/미확인으로 표시하며 전체 작업을 실패처럼 표현하지 않는다.
- 한 PR에는 S3 기능 하나만 담고 `ops_github_open_code_pr`로 base=dev 코드 PR을 연다.

## 검증 경계

- PR의 `guard`와 `plan`, merge, `tf-apply` 성공을 각각 독립적으로 확인한다.
- `tf-apply` 성공 뒤 최종 PR/check 갱신, dev 브랜치의 새 S3 파일 재조회, dev 서비스 health 회귀 점검은 서로 의존하지 않으므로 한 번의 병렬 호출로 수행한다. 이때 PR/check는 변경 전달 근거, 파일 재조회는 코드 반영 근거, service health는 기존 앱 무영향 근거일 뿐이며 어느 것도 S3 API 독립 조회로 표현하지 않는다.
- `ops_github_get_pr_status`가 해당 PR의 `apply_runs`를 직접 반환하지만 merge commit SHA 필드는 제공하지 않는 경우, SHA를 추측하거나 다른 ARN에서 조합하지 않는다. PR에 연결된 apply run이 `head_branch=dev`에서 `completed/success`인지 확인하고, 최종 PR 상태의 `merged=true`·guard 성공 및 dev 브랜치 파일 재조회를 함께 근거로 삼는다. merge SHA가 별도로 관측될 때만 `head_sha` 일치까지 추가 대조한다.
- dev 코드 PR에서 2분 이상 `checks=[]`인 채 OPEN이면 CODEOWNERS 승인 대기나 plan 성공/실패로 추정하지 않는다. `guard 상태 미보고 · 아직 머지/apply되지 않아 live 변경 없음`으로 도달한 단계까지만 보고한다. 이때 PR에 제출한 안전 속성은 `제안 코드 구성`으로만 설명하고 dev 브랜치나 S3에 이미 반영된 구성처럼 쓰지 않는다. 사용자가 후속 확인을 요청하면 새 PR을 열지 말고 기존 PR URL을 재조회해 merge·apply 추적을 이어간다.
- merge·apply 뒤 `ops_github_read_file(path="2-1-dev/<기능>.tf", ref="dev")`로 새 파일을 다시 읽어, 제출한 S3 리소스와 public access 차단·`BucketOwnerEnforced`·SSE-S3·`force_destroy` 설정이 dev 브랜치에 실제 남았는지 대조한다. 이 재조회는 **코드 반영 확인**일 뿐 S3 버킷의 live 존재·속성을 독립 검증한 것은 아니므로, S3 API 조회와 같은 증거로 합치지 않는다.
- `service_enabled=false` 상태에서 게이트된 버킷 코드를 apply했다면 첫 verdict는 “코드 반영 완료 · 실제 생성은 서비스 활성화 시”처럼 쓴다. ⑤를 버킷 생성 완료로 표시하지 말고, 현재 변수값 때문에 생성이 보류됐음을 명시한다.
- 현재 ops read 도구가 S3 버킷 목록·속성을 반환하지 않으면 AWS CLI로 우회하지 않는다. ⑤는 “S3 API 독립 조회 미지원”으로 남기고, `tf-apply` 성공을 별도의 live 조회처럼 중복 주장하지 않는다.
- 버킷 이름의 account id를 도구 출력에서 확인하지 못했다면 실제 숫자를 추측하지 말고 이름 규칙만 보고한다.
- 최종 보고에는 public access 차단, 소유권, 암호화, `force_destroy` 영향, 앱 통합 포함 여부를 짧게 열거한다.
- 사용자가 “앱 업로드 파일 저장용 버킷”이라고만 말한 것은 용도 설명이지 애플리케이션 통합 요청으로 확대 해석하지 않는다. 버킷 리소스만 최소 추가하고, IAM policy·instance profile·`S3_BUCKET` 배선은 미포함이라고 최종 보고한다. “앱에서 실제 업로드 가능하게 연결”처럼 동작까지 명시한 경우에만 통합 범위를 별도로 다룬다.
- `service_enabled=true`에서 `tf-apply`가 성공했어도 S3 전용 read 도구가 없다면 첫 verdict는 `Terraform 반영 완료`로 한정한다. ⑤는 `🕓 S3 API 독립 조회 미지원`처럼 관측 경계를 드러내고, 다른 리소스 ARN에서 본 account id를 조합해 실제 버킷 이름이나 존재를 독립 검증했다고 쓰지 않는다.
- 회귀 점검은 전체 집계 대신 `-dev-` 이름을 가진 app 인스턴스와 dev target group만 골라 `running`·`healthy`를 열거한다. 이 health 결과는 S3 생성 증거가 아니라 기존 서비스 무영향 증거로 분리한다.

## 최종 보고 골격

S3 전용 read 도구가 없는 상황에서 `service_enabled=true`이고 apply가 성공했다면, 완료 범위와 미검증 범위를 한 문장에 섞지 않는다.

- 첫 verdict: `✅ Terraform 적용 완료 — dev 코드와 tf-apply 성공 확인, S3 API 독립 조회는 현재 도구에서 미지원`
- ④에는 `tf-apply` workflow 결론만 기록한다.
- ⑤에는 `🕓 Terraform 적용 성공 · S3 API 독립 조회 미지원`처럼 독립 검증 경계를 남긴다. 이를 `❌ 미반영`으로 쓰거나 `✅ 버킷 생성·속성 확인`으로 과장하지 않는다.
- 그 아래에 dev 브랜치 파일 재조회 결과와 dev app/TG 회귀 점검을 별도 근거로 둔다. 둘 다 S3 live 조회의 대체 증거로 사용하지 않는다.
- 버킷 이름은 apply 출력이나 S3 전용 조회로 실제 값이 관측되지 않았다면 `<project>-dev-app-uploads-<account_id>` 규칙만 제시한다. 다른 리소스 ARN에 보이는 account id로 완성된 이름을 조립하지 않는다.
- 마지막에 반드시 `force_destroy=true`의 객체 동반 삭제 영향과, 단순 버킷 요청일 때 IAM policy·instance profile·`S3_BUCKET` 배선 미포함을 함께 적는다.
