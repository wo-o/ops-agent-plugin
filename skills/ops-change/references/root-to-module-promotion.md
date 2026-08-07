# 승격을 위한 root→module 상태 주소 이전

dev에서 `2-1-dev/` 독립 파일로 만든 리소스를 prod로 승격할 때, 승격 경로
(`ops_github_open_promotion_pr`)는 modules/·ansible/의 dev↔main diff만 담는다. 따라서
dev root(`2-1-dev/`)에만 있는 리소스는 그대로 승격되지 않는다 — 먼저 그 리소스를
`modules/service/`로 이관하는 dev 코드 PR이 선행해야 한다. 이관은 기존 인프라를
재생성하지 않고 Terraform state 주소만 옮기는 작업이다.

## 절차

1. 변경 대상 root 파일과 module의 모든 관련 `.tf` 파일을 먼저 읽는다.
   - 같은 module scope의 resource·data source·local·output label은 파일이 달라도 중복될 수 있다.
   - 특히 `data "aws_caller_identity" "current"` 같은 공통 data source가 이미 있으면 새
     파일에서 재선언하지 말고 재사용한다.
2. 새 module 파일에는 리소스 정의와 module-level output을 둔다. 기존 보안·수명주기 값은 바꾸지 않는다.
3. 기존 root에는 각 상태 보유 리소스마다 `moved` 블록을 작성한다.
   - `from`은 기존 root 주소, `to`는 정확한 `module.service.<resource address>`.
   - 주 리소스뿐 아니라 연결된 access block, ownership control, encryption configuration 등
     주소가 바뀌는 모든 상태 보유 리소스를 포함한다.
4. 기존 root output이 외부 계약이면 삭제하지 말고 새 module output을 참조하도록 유지한다.
5. dev 코드 PR로 plan과 guard를 통과시키고 auto-merge 뒤 `tf-apply` 성공을 확인한다.
6. 해당 기능의 전용 cloud read 도구가 없으면 apply 성공과 서비스 회귀 점검을 별도 근거로
   기록한다. S3 API 독립 조회 미지원 같은 관측 한계를 생성 성공으로 과장하지 않는다.
7. dev apply 및 read-only 회귀 점검이 성공한 뒤에만 dev→main 승격 PR을 연다. prod main에는
   직접 코드를 저작하지 않는다.

## 예시 (S3 버킷 이관)

기존 root 리소스:

```hcl
resource "aws_s3_bucket" "app_uploads" {
  # ...
}
```

동일 리소스를 `module "service"` 내부로 옮긴 뒤, root에 다음을 둔다.

```hcl
moved {
  from = aws_s3_bucket.app_uploads
  to   = module.service.aws_s3_bucket.app_uploads
}

moved {
  from = aws_s3_bucket_public_access_block.app_uploads
  to   = module.service.aws_s3_bucket_public_access_block.app_uploads
}

moved {
  from = aws_s3_bucket_ownership_controls.app_uploads
  to   = module.service.aws_s3_bucket_ownership_controls.app_uploads
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.app_uploads
  to   = module.service.aws_s3_bucket_server_side_encryption_configuration.app_uploads
}
```

기존 root output은 삭제하지 않고 새 module output을 참조한다.

```hcl
output "app_uploads_bucket_name" {
  value = module.service.app_uploads_bucket_name
}
```

## 흔한 오류: `Duplicate data "aws_caller_identity" configuration`

Terraform의 label 유일성은 파일별이 아니라 module scope별이다. `modules/service/main.tf`에
이미 `data "aws_caller_identity" "current" {}`가 있으면 새 파일에서 같은 label을 선언하면
안 된다. 새 리소스 파일은 기존 `data.aws_caller_identity.current.account_id`를 그대로
참조한다. 중복 선언을 제거한 뒤 dev plan과 apply를 다시 검증한다.

## 실패 처리 · 검증 경계

- `Duplicate data ... configuration` 또는 중복 label 오류가 나면 동일 module scope에서
  재선언된 resource/data/local/output label을 찾아 최소 수정으로 같은 PR을 보정한다.
  보정 PR의 plan과 apply가 다시 성공하기 전에는 승격 PR을 열지 않는다.
- `moved` 블록은 state 주소 보존의 의도이며, 실제 판단은 Terraform plan이다. plan이 state
  이동 대신 destroy/create를 제안하면 이관을 멈추고 주소, `count` index, `for_each` key를
  다시 대조한다. 재생성 위험을 무시하고 진행하지 않는다.
- cloud API read 도구가 없는 리소스는 `tf-apply` 성공과 독립 서비스 회귀 점검을 나누어
  보고한다. apply 성공만으로 속성의 독립 검증을 주장하지 않는다.

## 검증 체크리스트

- `moved` 블록이 모든 이관 리소스 주소를 포함한다.
- module 내 label 중복이 없다.
- dev plan, guard, merge, apply가 각각 성공했다.
- 가능한 범위의 read-only 서비스 회귀 점검이 정상이다.
- prod 승격은 dev 검증 증거가 PR 본문에 포함된 상태에서만 요청한다.
