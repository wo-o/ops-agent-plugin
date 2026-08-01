# 서비스 생성·삭제 완료 판정 체크리스트

`<env>-service`에서 `service_enabled=true` 또는 `service_enabled=false`를 적용한 뒤 사용하는 검증 절차다.

## prod 전체 삭제 빠른 실행 순서

아래 순서를 하나의 연속 작업으로 끝까지 수행한다. Slack 사전 승인과 GitHub CODEOWNERS 리뷰는 서로 다른 게이트다.

1. PR보다 먼저 `OPS_INFRA_SLACK_MENTION`을 실제 값으로 읽어, 대상·`service_enabled=false`·EC2/RDS/Bastion/ALB destroy·복구 불가 영향을 적은 Slack 사전 승인 요청을 남긴다. 이때는 PR도 live 변경도 없다.
2. 승인 답변자의 Slack user ID가 멘션 대상과 일치하면 즉시 승인 완료로 본다. ID를 확인할 수 없거나 다를 때만 승인권자 본인 여부를 한 번 확인한다. 승인과 자격이 확인되면 삭제 범위를 다시 묻지 않는다.
3. `prod-service`, `set_value`, `{"service_enabled":false}`로 PR을 정확히 한 번 열고, `reason`에 승인자 표시명과 같은 스레드의 명시적 승인·자격 확인을 남긴다.
4. PR을 약 2분간 live 폴링한다. `OPEN`이면 Slack 승인은 완료됐지만 GitHub CODEOWNERS 머지와 destroy는 아직 시작되지 않은 상태다. `MERGED`일 때만 `tf-apply` run을 추적한다.
5. RDS destroy가 포함되므로 `tf-apply`가 수 분간 `in_progress`여도 45~90초 간격으로 계속 폴링한다. `completed/success` 전에는 일부 리소스가 사라졌어도 완료로 판정하지 않는다.
6. 성공 후 PR 상태·`ops_aws_get_service(prefix="ops-agent-iac")`·`ops_aws_find_unused_candidates`를 병렬 재조회한다. prod 이름만 분리하고, 귀속 불명 EBS/EIP/orphan SG가 하나라도 있으면 apply 성공과 별개로 ⑤를 `부분 확인`으로 유지하며 임의 삭제하지 않는다. 공용 IAM role 후보는 서비스 destroy 잔여물 판정에서 제외한다.

## 상태 추적

1. PR 생성 직후 prod라는 이유만으로 승인 대기라고 단정하지 않는다.
2. `ops_github_get_pr_status`를 약 2분간 폴링한다. 관측 중 머지되면 승인 방식은 추정하지 않고 `머지 — ✅ MERGED`로 기록한다.
   - 이 폴링 중 `MERGED`가 확인되면 Slack 승인 멘션 값 조회나 승인 요청 메시지는 불필요하다. 멘션 조회는 약 2분 뒤에도 실제로 `OPEN`인 prod 서비스 PR을 승인 대기로 보고할 때만 수행한다.
   - dev 서비스 PR이 2분 넘게 `OPEN`, `mergeable=true`, `checks=[]`이면 `자동 머지 workflow 진행 중`이라고 추정하지 않는다. 관측된 사실은 guard check-run이 아직 보고되지 않았다는 것이므로 첫 verdict를 `🕓 guard 상태 미보고 — apply/destroy 미시작`으로 쓰고, 타임라인은 `② guard(plan) — 🕓 상태 미보고`, `③ 자동 머지 — 🕓 guard 완료 대기`에서 끝낸다. ④·⑤는 쓰지 않으며, `내려가는 중`처럼 live 변경이 시작된 표현도 금지한다.
   - 반대로 `checks`에 `enable-automerge` 또는 다른 명시적 workflow가 `in_progress`로 나타날 때만 그 workflow의 진행/정체를 보고한다. `mergeable=true`는 머지 가능성일 뿐 workflow 실행 증거가 아니다.
3. 머지된 응답의 `apply_runs[].run_url`로 `ops_github_get_workflow_run`을 조회한다.
4. RDS 생성이 포함되면 `in_progress`가 여러 분 지속될 수 있다. 처음 15~30초, 이후 45~60초 간격의 bounded backoff로 `completed`까지 기다린다.
5. `conclusion=success` 전에는 일부 EC2가 보여도 apply 완료로 판정하지 않는다.

## prod 승인 답변을 받은 직후

먼저 현재 흐름이 **사전 승인 때문에 아직 PR이 없는 상태**인지, **이미 PR이 존재하는 후속 승인 상태**인지 구분한다.

- prod 삭제 hard gate에 따라 아직 PR을 만들지 않은 상태라면, 승인권자 자격까지 확인된 직후 `<env>-service` PR을 정확히 한 번 연다. `reason`에는 같은 Slack 스레드에서 확인한 승인자 표시명과 명시적 승인·자격 문구를 남긴다. 그다음 새 PR을 live 폴링한다.
- 이미 PR이 존재하는 후속 승인 상태라면 기존 PR을 즉시 `ops_github_get_pr_status`로 한 번 재조회한다. 같은 desired state의 새 PR을 열지 않는다.
- `승인`처럼 짧은 답변만 왔을 때는 승인 문구와 승인자 신원 확인을 분리한다. 메시지 메타데이터의 Slack user ID가 사전에 멘션한 `OPS_INFRA_SLACK_MENTION`의 ID와 일치하면 조직 승인을 충족한다. ID가 보이지 않거나 다르면 곧바로 `Slack 승인 확인`으로 기록하지 말고, 같은 스레드에서 `인프라팀 승인권자 본인인지`를 한 번만 확인한다. 요청자 이름이나 표시명이 같다는 이유만으로 승인권자라고 추정하지 않는다.
- 자격 확인 답변이 `승인합니다. 인프라팀 승인권자 본인입니다.`처럼 승인과 자격을 함께 명시하면 조직 승인 확인은 끝난다. 같은 사람에게 Slack ID나 자격을 다시 요구하지 않는다.
- 사용자가 `승인합니다 — CODEOWNERS 머지하겠습니다`처럼 승인과 미래형 머지 의사를 함께 밝혀도, 이는 Slack 조직 승인이지 GitHub 머지 완료가 아니다.
- live 상태가 `OPEN`이면 `Slack 승인 확인 · GitHub CODEOWNERS 머지 대기 · apply/destroy 미시작`으로 보고하고 ④·⑤는 쓰지 않는다. 미래형 표현만으로 destroy가 진행 중이라고 표현하지 않는다.
- live 상태가 `MERGED`일 때만 apply run 추적과 삭제 후 독립 검증을 이어간다. 승인 주체나 머지 방식을 추정하지 않는다.

## apply 후 독립 검증

가능한 조회는 병렬로 실행한다.

- `ops_github_get_pr_status`: 최종 guard/check 및 apply run 상태
- `ops_get_service_health(prefix="ops-agent-iac")`: 요청 환경 EC2와 target health
- `ops_aws_get_service(prefix="ops-agent-iac")`: 요청 환경 ALB DNS와 인스턴스 세부정보

응답에는 dev/prod가 함께 섞일 수 있으므로 이름의 `-dev-`/`-prod-`를 기준으로 요청 환경만 고른다. 과거 `terminated` 인스턴스는 제외하고 현재 `running` 인스턴스만 열거한다.

특히 재프로비저닝 직후에는 같은 논리 이름(`*-app-0`, `*-app-1`)의 과거 `terminated` 인스턴스와 새 `running` 인스턴스가 동시에 반환될 수 있다. 이때 `instances_total`이나 이름 중복을 현재 replica 수로 해석하지 말고, 요청 환경이면서 `state=running`인 고유 instance ID만 현재 fleet으로 집계한다. 새 인스턴스의 target health가 `healthy`인지도 같은 ID로 대조한다.

완료 조건:

- 요청 환경의 app EC2가 모두 `running`
- 같은 환경 target group의 모든 대상이 `healthy`
- 실제 ALB DNS의 `GET /healthz`가 HTTP 200

ALB 트래픽 검증은 `curl -sS -i --max-time 15 http://<alb-dns>/healthz`처럼 시간 제한을 두고 실행한다. `curl`의 exit code만으로 성공 처리하지 말고 응답 상태행의 HTTP 200과 짧은 본문(예: `ok`)을 함께 기록한다. 최종 보고에도 상태 코드와 본문을 남겨, ALB 존재와 애플리케이션 health 응답을 같은 사실로 뭉개지 않는다.

`running`만 확인하거나 전체 집계(`instances_running`, `alb_targets_healthy`)만 보고 완료 처리하지 않는다. 전체 집계에는 반대 환경이 포함될 수 있다.

## 보고 경계

- 각 현재 인스턴스를 `이름 · instance type · instance ID · public IP`로 열거한다.
- 사용자가 `EC2`를 단수형으로 썼지만 수량을 명시하지 않았고 실제 인스턴스가 여러 대라면, `<env>-service` surface가 replica 수를 바꾸지 않고 기존 desired capacity를 유지했다는 점을 한 줄로 명시한다. 실제 대수를 요청자가 지정한 값처럼 표현하거나 세팅 과정에서 임의 확장한 것으로 보이게 하지 않는다.
- PR URL과 `tf-apply` run URL을 모두 남긴다.
- 변경 도구가 반환한 generic `usage`/`followup`에 `ops_aws_get_service(2-1-dev)`처럼 Terraform root 이름이 조회 prefix로 적혀 있어도 그대로 전달하거나 실행하지 않는다. 서비스 조회는 Name-tag 프로젝트 접두사인 `ops-agent-iac`를 사용하고, 최종 안내에는 내부 도구 호출 예시 대신 실제로 확인한 ALB URL·리소스 상태를 제공한다.
- 실제 ALB URL과 `/healthz` HTTP 상태를 적는다.
- RDS는 `ops_aws_get_service`의 `rds_instances[]`로 직접 조회한다. 해당 환경 항목이 있으면 `id · status · engine · instance_class · endpoint:port · publicly_accessible`을 그대로 열거하고, 없으면(빈 리스트) `이 환경 RDS 없음`으로 적는다 — RDS 실재·규격·endpoint를 추측하지 않는다. `publicly_accessible=false`는 비공개 노출의 근거일 뿐 private subnet·route-table 배치를 직접 확인한 증거는 아니다.
- 사용자가 `퍼블릭 서브넷 EC2 + 프라이빗 서브넷 RDS`처럼 토폴로지를 지정했으면 최종 보고를 `요청·적용 구성`과 `실제 관측`으로 분리한다. EC2의 public IP, ALB의 `internet-facing`, RDS의 `publicly_accessible=false`는 각각 기록하되, read 응답에 subnet/route-table 정보가 없으면 subnet 배치까지 검증했다고 쓰지 않는다.
- Bastion은 별도 endpoint 필드가 없으므로 해당 환경의 `*-bastion` SG 존재와 ingress를 확인 가능한 범위로 기록한다. 초기 프로비저닝 직후 `ingress=[]`이면 생성 실패가 아니라 기본 폐쇄 상태다. 최종 보고에는 `Bastion은 apply 범위에 포함 · 요청자 CIDR의 SSH 접근 규칙은 아직 열리지 않음`으로 분리하고, 접근이 필요하면 별도의 `<env>-db-access` 요청이 필요하다고 안내한다. 반대로 SG 존재나 apply 성공만으로 Bastion 접속 가능성을 주장하지 않는다.
- app EC2에 public IP가 있어도 SSH 접근이 열린 것은 아니다. `*-ec2` SG의 CIDR ingress가 비어 있으면 관리용 SSH는 기본 폐쇄 상태로 보고하고, 사용자가 SSH 접속도 요청한 경우에만 별도의 `<env>-ec2-ssh` surface로 요청자 CIDR을 개방한다. public IP 존재나 app/TG `healthy`를 TCP 22 허용 근거로 확대하지 않는다.
- `ops_aws_get_service.security_groups[].ingress`는 CIDR 기반 규칙의 관측 근거다. app 또는 DB SG가 `ingress=[]`여도 SG-to-SG 참조 규칙이 도구 응답에서 생략됐을 수 있으므로, 이를 ALB→app·앱→RDS 연결 차단이나 서비스 생성 실패의 증거로 쓰지 않는다. 서비스 생성 완료는 RDS `available`, app/TG health, ALB `/healthz` 결과로 판정하고, 내부 SG-to-SG 네트워크 경로는 관측 범위를 분리해 보고한다.
- prod PR이 관측 중 머지되었더라도 `자동 머지`나 `사람 승인 완료`라고 쓰지 않는다. 관측 가능한 사실은 단순히 `MERGED`다.

## 서비스 삭제(`service_enabled=false`) 검증

삭제 apply 성공 뒤에는 생성 검증과 다른 완료 조건을 적용한다. 독립적인 최종 조회는 가능한 한 병렬로 실행한다.

- `ops_github_get_pr_status`: 최종 guard/check와 apply run 상태를 다시 확인한다.
- `ops_aws_get_service(prefix="ops-agent-iac")`: 이름의 `-dev-`/`-prod-` 표식으로 요청 환경만 분리해 app EC2, EBS, target group, ALB, security group을 대조한다.
- `ops_aws_find_unused_candidates`: unattached EBS, unassociated EIP, orphan security group 등 미연결 후보를 확인한다. 이 도구는 후보 보고 전용이며 삭제 근거가 아니다.

삭제 완료 판정과 보고 경계:

- 삭제 후 `ops_aws_get_service(prefix="ops-agent-iac")` 응답 전체가 비어 있을 것을 기대하지 않는다. 삭제된 환경의 과거 EC2가 `terminated` 상태로 계속 반환되고, 반대 환경의 running 리소스와 공유 리소스도 함께 보이는 것이 정상이다. 완료 판정은 반드시 이름의 `-dev-`/`-prod-` 표식과 리소스 상태를 함께 필터링해 수행한다.
- 요청 환경 app EC2는 `terminated`, 같은 환경 EBS/TG/ALB/SG는 없음으로 직접 관측된 범위만 열거한다. 반대 환경이나 공유 monitoring 리소스가 남아 있어도 요청 환경 삭제 실패로 세지 않는다.
- RDS는 `rds_instances[]`로 직접 확인하되, 응답 전체가 아니라 요청 환경의 ID(`*-dev`/`*-prod`)로 먼저 필터링한다. destroy 뒤 요청 환경 항목이 0건이면 반대 환경 RDS가 남아 있어 전역 배열이 비어 있지 않더라도 `요청 환경 RDS 삭제 확인`으로 판정한다. 요청 환경 항목이 아직 `deleting`/`available`이면 그 status를 그대로 적는다. 반대 환경 RDS의 존재를 요청 환경 삭제 실패로 세지 않는다. Bastion 인스턴스는 별도 필드가 없어 개별 실재를 직접 확인할 수 없으므로 `tf-apply 성공 범위에 포함되지만 Bastion 인스턴스 개별 조회는 미지원`으로 분리한다. 다만 `security_groups[]`에 요청 환경의 `*-bastion` SG가 없으면 `Bastion SG 없음`은 직접 관측 사실로 별도 기록할 수 있다. SG 부재를 Bastion 인스턴스 부재의 직접 증거로 확대하지 않는다.
- 미연결 후보가 있어도 Name/environment 태그나 attachment 이력 등 귀속 근거가 없으면 이번 destroy의 dev 잔여물이라고 추정하지 않는다. 후보 ID·크기/IP와 `환경 귀속 미확인 · 임의 삭제 안 함`을 기록한다.
- `ops_aws_get_service`에서 요청 환경의 연결 EBS가 0건이고 `ops_aws_find_unused_candidates`에서 귀속 불명의 unattached EBS가 발견될 수 있다. 이때 `dev EBS 없음`은 *서비스 조회에서 dev에 연결·귀속된 EBS가 없음*이라는 범위로만 표현하고, 계정 전체에 EBS가 전혀 없다는 뜻으로 확대하지 않는다. 타임라인 ⑤는 미연결 후보가 있는 한 `🕓 부분 확인`으로 유지하며 후보를 별도 열거한다.
- 후보 이름에 `seed`, `shared`, `orphan`, `dev` 같은 문자열이 들어 있어도 이름만으로 귀속을 확정하거나 `seed 리소스로 보임`처럼 가능성을 덧붙이지 않는다. 도구가 반환한 사실(종류·ID·크기/IP)과 `환경 귀속 미확인`만 보고한다.
- `unused_iam_roles`에 나오는 apply/plan runner, Hermes read role, monitoring, seed 등 계정 공용 control-plane 역할은 서비스 destroy 잔여물 판정에서 제외한다. 사용자가 별도로 미사용 리소스 정리를 요청하지 않았다면 최종 후보 목록에도 늘어놓지 않는다.
- 환경 귀속 미확인 후보가 하나라도 있으면 첫 verdict를 `전체 삭제 완료`라고 쓰지 않는다. `서비스 destroy 적용 완료 · 미연결 리소스 귀속 확인 필요`로 쓰고, 타임라인 ⑤는 `🕓 부분 확인`으로 표시한다.
- 최종 보고에서는 삭제된 서비스 리소스와 귀속 불명 후보를 반드시 별도 섹션으로 나눈다. 요청 환경 EC2는 인스턴스별 `⚫ <name> — terminated`, 요청 환경 RDS 0건과 ALB/TG/SG 부재는 각각 직접 관측 범위로 적는다. Bastion은 SG 부재와 인스턴스 개별 조회 미지원을 한 문장으로 합쳐 부재를 과장하지 않는다.
- 귀속 불명 후보는 종류별로 `🟡` 상태 줄을 만들고 ID·크기 또는 IP·allocation ID만 적은 뒤 `환경 귀속 근거 없음 · 임의 삭제 안 함`으로 닫는다. 후보 이름에 환경처럼 보이는 문자열이 있어도 요청 환경 잔여물로 분류하지 않는다. 이렇게 해야 ④의 apply 성공과 ⑤의 부분 확인이 시각적으로 섞이지 않는다.
- PR URL과 `tf-apply` run URL을 모두 남기고, 반대 환경의 유지 리소스가 실제 관측되면 영향 범위가 요청 환경에 한정됐음을 마지막에 짧게 밝힌다.
