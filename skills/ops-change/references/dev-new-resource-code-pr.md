# dev 신규 리소스 코드 PR — 일반 체크리스트

surface 밖 dev 신규 리소스 요청(캐시, 큐, 토픽, 버킷, 스트림 등 종류 무관)을
재PR 없이 첫 apply에 끝내기 위한 공통 절차. 2026-07-31 실측: 이 절차 없이
캐시 요청을 처음부터 저작했을 때 파일 탐색 8분 + apply-time API 함정으로
실패·재PR 6분 + 느린 리소스 변형 선택으로 생성 10분 ≈ 30분이 걸렸다. 세
원인 모두 리소스 종류와 무관하게 반복될 수 있다.

## 리소스 선택 원칙

- **dev 데모 기본값은 생성이 빠르고 최소 과금인 변형** — serverless/온디맨드/
  최소 티어를 우선한다. 생성에 10분+ 걸리는 변형(노드 기반 클러스터,
  프로비저닝드 용량 등)은 사용자가 그 제어를 명시 요청할 때만 쓴다.
- 리소스 타입에 사용량·용량 상한 설정이 있으면 최소 허용값으로 캡해
  비용 백스톱을 코드에 남긴다.
- **apply-time API 제약은 guard(plan)가 못 잡는다.** 같은 서비스에 Terraform
  리소스가 여러 개면(cluster vs replication group vs serverless 등) 요청한
  엔진·모드를 실제 지원하는 리소스를 고른다. plan 통과는 리소스 선택이
  옳다는 증거가 아니다.

### known apply-time gotchas (누적)

| 요청 | 함정 | 올바른 선택 | 근거 |
|---|---|---|---|
| Valkey 캐시 | `aws_elasticache_cluster`는 plan 통과 후 apply에서 `This API doesn't support Valkey engine` 실패 | `aws_elasticache_serverless_cache`(생성 1분 미만, 최소 100MB·월 $6 수준) 또는 명시 요청 시 `aws_elasticache_replication_group`(생성 ~10분) | 2026-07-31 tf-apply run 30602927635 |

## preflight (병렬 read 1회)

서로 독립이므로 한 번의 병렬 호출로 읽고, 이후 추가 탐색을 반복하지 않는다:

- `modules/service/main.tf` — 재사용할 공용 참조: `local.name`,
  `local.enabled`, `data.aws_vpc.foundation`, `aws_subnet.private[*]`(RDS용
  private 서브넷 2개), `aws_security_group.ec2`(앱 EC2 SG)
- 저작할 후보 파일 경로 — 존재 여부와 과거 원복 stub 잔존 여부
- `2-1-dev/` 디렉터리 목록 — 파일·label 충돌 확인
- `2-1-dev/service.auto.tfvars` — `service_enabled` 게이트 상태 (false면 이번
  apply는 코드만 반영하고 리소스는 만들지 않는다)

실제로 수정할 파일은 최소로 제한하고 나머지는 읽기 전용 문맥으로만 쓴다.

## 배치 패턴 (둘 중 하나)

- **모듈 내부 참조가 필요하면** (private 서브넷, 앱 SG, ALB 등):
  `modules/service/<기능>.tf` 독립 파일. 모듈은 prod와 공유되므로 반드시
  environment 게이트를 넣는다. root output 노출이 필요하면 `2-1-dev/<기능>.tf`
  독립 파일에 `module.service.<output>` 참조를 함께 추가한다.
- **독립 리소스면** (모듈 내부 참조 불필요 — S3 버킷 등): `2-1-dev/` 아래
  독립 `.tf` 파일. S3 사례는 `references/dev-s3-code-pr.md`와
  `templates/dev-app-uploads-s3.tf` 참조.

모듈 내부 골격 (리소스 종류만 바꿔 재사용):

```hcl
locals {
  <기능>_enabled = local.enabled * (var.environment == "dev" ? 1 : 0)
}

# 전용 SG가 필요하면: 앱 EC2에서만 해당 포트 인바운드
resource "aws_security_group" "<기능>" {
  count  = local.<기능>_enabled
  name   = "${local.name}-<기능>"
  vpc_id = data.aws_vpc.foundation.id
  tags   = { Name = "${local.name}-<기능>" }
}

resource "aws_<타입>" "<기능>" {
  count = local.<기능>_enabled
  # 사용량·용량 상한이 있으면 최소 허용값으로 캡
  tags  = { Name = "${local.name}-<기능>" }
}

output "<기능>_endpoint" {
  value = try(aws_<타입>.<기능>[0].<endpoint 속성>, null)
}
```

- 과거 원복 흔적 주의: 코드 PR 도구는 파일 삭제를 지원하지 않아 원복된
  기능은 null output stub 파일로 남는다. 같은 경로를 먼저 읽고 전체 교체한다.
  동일한 desired state가 이미 있으면 빈 diff PR을 열지 않는다 (S3 체크리스트와
  동일 규칙).
- 한 PR에 기능 하나만. 기존 variable validation·비용 상한을 완화하지 않는다.

## 범위 판정

- "하나 세팅해줘/만들어줘" → 리소스 생성 + endpoint/식별자 보고까지. 앱 연결
  (`/opt/app/env` 환경변수 주입 등)은 미포함이라고 최종 보고에 명시한다.
- "앱이 쓰게 해줘/연결까지" → `modules/service/app.tf` user_data 주입과
  인스턴스 교체 영향까지 범위를 별도로 확인한다.

## apply 실패 시

- 에러 본문에서 **API 이름과 거부 사유를 먼저 읽는다**. apply-time API 제약이면
  파라미터를 바꿔 재시도하지 말고 리소스 타입 교체를 검토한다 — 같은 실패를
  파라미터 순열로 반복하는 것이 최대 시간 낭비다.
- 해결한 함정은 최종 보고에 한 줄 남겨 위 gotchas 표에 축적될 수 있게 한다.

## 검증 경계 (전용 read 도구가 없는 리소스 공통)

- apply run `success`까지 폴링하고, dev 브랜치 파일 재조회로 코드 반영을
  확인한다. 이 재조회는 코드 반영 증거일 뿐 리소스의 live 존재 증명이 아니다.
- `tf-apply 성공`과 `<서비스> API 독립 조회 미지원`을 분리해 보고하고, raw
  CLI로 우회하지 않는다.
- `ops_get_service_health`는 기존 앱 무영향(회귀) 근거로만 쓴다.
- endpoint/이름 실값은 도구 출력에서 관측된 경우에만 보고하고, 아니면
  `${local.name}-<기능>` 이름 규칙만 제시한다.
