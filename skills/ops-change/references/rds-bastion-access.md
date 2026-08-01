# RDS Bastion 접근: 네트워크 grant와 DB 권한 분리

## 핵심 경계

`<env>-db-access`는 요청자 CIDR에서 Bastion TCP 22로 들어오는 Security Group ingress만 관리한다. 이 surface는 PostgreSQL role이나 비밀번호를 만들지 않으며, `readonly` 권한을 보장하지 않는다.

- dev: `expires_at` 생략 가능. 생략하면 영구 네트워크 grant다. **dev 기본 경로는 단순하게**: 임시 DB 계정 발급(`rds-temp-user`)은 prod 전용이므로 dev에서 새 계정 생성 여부·사용자명·만료를 되묻지 말고, 요청자 IP만 확인해 network grant를 연다.
  - write 도구의 `usage`가 `expires_at=None`인 경우에도 일반적인 "만료 시 cron 회수" 문구를 포함할 수 있다. 실제 entry에 만료 시각이 없으면 이를 자동 회수 예정으로 전달하지 말고, 영구 grant임을 명시한다.
  - write 도구의 `usage`는 이제 env별 표준 계정을 안내한다(dev=상시 `readonly`, prod=`rds-temp-user` 임시 role). 최종 `psql`의 `user`는 이 값을 쓰되, `usage`는 네트워크 surface의 구성 근거일 뿐 DB role을 live 조회한 결과가 아니므로 읽기 전용 identity나 실제 만료 정책의 권위 있는 검증 근거로 표시하지 않는다. 구버전 스택에서 `usage`가 예외적으로 master(`dbadmin`)를 안내하면(아래 dbadmin-only 항목) 그대로 전달하지 말고 실제 요청 권한과 이 runbook을 우선한다.
  - dev에는 서비스 세팅 apply 후 `rds-readonly-user` ansible이 만든 상시 `readonly` 계정이 있다(LOGIN, SELECT 전용 — 멱등 playbook, 구 bastion user_data 부트스트랩 대체). psql 접속 명령의 `user`에는 `readonly`를 넣는다. 비밀번호는 dev 브랜치 `ansible/rds-readonly-user.yml`의 `readonly_password` 데모 값이다 — 실습용 데모 자격증명이므로 사용자가 물으면 `ops_github_read_file(path="ansible/rds-readonly-user.yml", ref="dev")`로 확인해 실값을 그대로 알려준다. "강의 자료를 참고하라"고 미루거나 마스킹하지 않는다(이 예외는 dev readonly 데모 값 한정 — master(`dbadmin`) 비밀번호·private key 게시 금지는 그대로다). 새 사용자명을 발급하지 않는다. 네트워크 grant apply 성공 후에는 `ops_run_ansible_playbook(playbook="rds-readonly-user", environment="dev")`를 항상 한 번 dispatch해 계정을 보장한다(멱등) — 이전 회수 요청으로 role이 DROP됐을 수 있고, PostgreSQL은 role 부재도 클라이언트에 `password authentication failed`로만 보여 주므로 접속 오류나 과거 실행 기록으로 존재를 가정할 수 없다.
- prod: `expires_at` 필수. 상시 계정이 없으므로 사용자가 계정을 언급하지 않았으면 기존에
  쓸 수 있는 유효한 DB 계정이 있는지 먼저 확인한다 — 있으면 그 계정으로 접속만 안내하고
  `rds-temp-user`를 실행하지 않으며, 없으면 임시 계정 발급을 위해 사용자명을 확인한다.
  IP·기존 계정 여부·사용자명·만료 후 role 실제 삭제(DROP) 예약 여부는 한 번에 묻는다.
  삭제 예약을 원하면 발급 성공 후 Hermes one-shot `cronjob`으로 만료 몇 분 뒤
  `rds-temp-user` `state=absent` dispatch를 예약한다(네트워크 grant 회수는 access-expiry
  cron 담당 — 별도 cronjob 금지). 사용자가
  기간을 지정하지 않으면 현재 UTC 기준 1시간 후를 기본 만료로 계산하고, 기간을 다시 묻지
  않는다. 한 번에 요청한 값들을 사용자가 하나씩 답하면 이미 받은 값은 유지하고
  아직 없는 값만 짧게 다시 묻는다. 사용자명은 소문자·숫자·`_` 제약을 함께 안내한다.
- 사용자가 "읽기 전용"을 요청하면 네트워크 grant와 DB identity 발급을 별도 단계로 보고한다.
- 확인된 계정이 `dbadmin`뿐인 환경(구버전 스택)이면 이를 "읽기 전용"이라고 부르지 않는다. dbadmin이 유일한 계정이라는 사실과 읽기 전용이 아니라는 점을 명시한 채, 사용자가 보유한 비밀번호로 접속하도록 안내하는 것은 허용된다 — 비밀번호 자체는 에이전트가 알지도, 전달하지도 않는다.
- private key와 master(`dbadmin`) 비밀번호는 Slack에 게시하지 않는다. 단, `rds-temp-user`로 발급한 시간제한 임시 계정의 비밀번호는 발급 응답에 포함해 전달한다(시나리오 정본: 자격증명은 응답으로만). 존재하지 않는 "별도 안전 채널 수령 절차"를 안내하며 전달을 생략하면 계정이 사용 불가능한 dead end가 된다.

## CGNAT·VPN에서 SSH 실소스 IP가 다른 경우

`curl ifconfig.me`는 HTTP 요청의 출구 IP를 보여 줄 뿐, Bastion이 관측하는 SSH 연결의
실소스 IP와 항상 같지는 않다. CGNAT, 기업 프록시, split-tunnel VPN, 통신사 경로 때문에
프로토콜별 출구가 달라질 수 있다.

1. 최초 grant와 apply가 성공했고 Bastion SG에 요청 CIDR:22가 보이는데도 사용자의 터널이
   실패하면, 에이전트 호스트에서 대신 probe하지 말고 사용자에게 SSH 실소스 IP 확인 결과를
   받는다. 원인을 키·계정 문제로 단정하지 않는다.
2. 사용자가 새 실소스 단일 IP를 확정하면 `/32`로 정규화하고 기존 `db_grants`의 같은
   `entry_key`를 `set_entry`로 갱신한다. 새 key를 추가하거나 `/24`처럼 넓히지 않는다.
3. prod처럼 기존 entry에 `expires_at`이 있으면 새 CIDR 갱신에서도 동일 절대 시각을 유지한다.
   사용자가 명시하지 않은 만료 연장이나 영구 전환을 하지 않는다.
4. apply 뒤 `ops_aws_get_service.security_groups[].ingress`에서 정확한 환경의 Bastion SG만
   선택해 새 CIDR:22가 있고 이전 CIDR이 없는지 확인한다. 같은 CIDR이 EC2 SSH allowlist,
   monitoring SG, 다른 환경 SG에 남은 것은 별도 surface의 상태이므로 제거하지 않는다.
5. 최종 명령은 기존 성공 apply output의 실제 `bastion_public_ip`와 `db_endpoint`를 다시
   확인해 완성한다. SG 반영 성공과 사용자 PC의 터널 성공은 분리해 보고한다.

### 기존 임시 role을 유지한 CIDR-only 갱신

사용자가 CGNAT·VPN 때문에 SSH 소스 IP만 정정하고 기존 임시 DB 계정은 그대로 두라고 하면,
네트워크와 DB identity를 다시 분리한다.

1. 현재 UTC를 확인해 기존 공통 `expires_at`/`VALID UNTIL`이 아직 유효한지 먼저 대조한다.
   유효하면 기존 절대 만료 시각을 그대로 유지한다. 이미 만료됐거나 사용 시간이 거의 남지
   않았으면 과거 grant를 갱신하거나 만료를 임의 연장하지 말고 새 만료 시각을 확인한다.
2. 기존 `<env>-db-access`의 같은 안정적 `entry_key`를 `set_entry`로 갱신한다. 새 key를
   추가하지 않으며, CIDR만 새 단일 IP `/32`로 바꾸고 description·만료 정책은 유지한다.
3. 사용자가 기존 role 유지라고 명시했다면 `rds-temp-user state=present`를 다시 실행하지
   않는다. 재실행은 비밀번호를 회전시켜 사용자가 보유한 자격증명을 무효화할 수 있다.
   네트워크 PR·apply만 수행하며 사용자명·권한·비밀번호·`VALID UNTIL`은 변경하지 않는다.
4. apply 뒤 정확한 대상 Bastion SG에서 새 `CIDR:22`가 존재하고 이전 CIDR이 사라졌는지
   확인한다. 이전 CIDR이 app EC2, dev Bastion 등 다른 SG에 남아 있어도 이번 db-access
   갱신 실패로 보지 않으며, 최종 문구도 `prod Bastion SG에서 이전 CIDR 제거 확인`처럼
   검증 범위를 명시한다.
5. 최종 보고에는 네트워크 PR/apply URL, 유지된 role과 만료 시각, 전달 시점 기준 잔여 시간을
   적는다. 기존 비밀번호는 불필요하게 다시 게시하지 않고, 실제 Bastion IP·RDS endpoint를 넣은
   터널/`psql` 명령만 재안내한다. 요청자 PC의 재접속 성공은 여전히 별도 확인 사항이다.

## apply 후 네트워크 반영 및 접속 정보 확보

우선 `ops_aws_get_service(prefix="ops-agent-iac")`를 호출해 `security_groups[]`에서 정확한 환경의 Bastion SG(`ops-agent-iac-<env>-bastion`)를 고르고, `ingress`에 요청 CIDR의 TCP 22 규칙이 실제로 존재하는지 확인한다. 이 조회는 app EC2·EBS·TG·ALB와 함께 SG ingress를 반환할 수 있으므로, 전체 결과나 다른 환경 SG가 아니라 **대상 Bastion SG의 정확한 이름과 CIDR:22**를 대조한다. 규칙이 확인되면 네트워크 grant 반영 근거로 쓰되, 요청자 PC의 실제 SSH 성공까지 증명한 것으로 표현하지 않는다.

### dev readonly 최종 정보 조립 체크리스트

최종 안내는 각 값의 권위 있는 출처를 섞지 말고 다음 우선순위로 조립한다.

1. 네트워크 반영: `ops_aws_get_service.security_groups[]`의 정확한 `ops-agent-iac-dev-bastion` SG에서 요청 CIDR의 TCP 22를 확인한다. 같은 CIDR이 app EC2 SSH, monitoring, prod Bastion 등 다른 SG에 이미 보여도 dev RDS 접근이 열린 것이 아니다. 이를 `no-op`이나 중복 grant로 오판하지 말고 `<env>-db-access` surface를 정상 적용하며, 이번 grant의 성공·실패 판정과 안내에는 대상 Bastion SG의 규칙만 사용한다.
   - 최종 보고에서도 이번 변경으로 반영한 규칙은 `dev Bastion SG`로 정확히 귀속한다. 조회 결과에 같은 CIDR의 app EC2 SSH 규칙이 함께 있어도 이번 PR이 그것을 생성·수정했다고 표현하거나 접속 정보에 섞지 않는다. 이는 한 번의 전체 SG 조회가 여러 surface의 규칙을 함께 반환할 때 감사 범위를 명확히 하기 위한 경계다.
2. RDS endpoint·상태: `rds_instances[]`의 정확한 dev 항목을 우선 사용한다. endpoint가 여기서 확인되면 로그 값은 보조 대조만 하며 API 관측값을 덮어쓰지 않는다. 최종 접속 정보에는 endpoint뿐 아니라 `status=available` 여부와 `publicly_accessible=false`도 함께 적어, DB 준비 상태와 Bastion 경유가 필요한 이유를 명확히 한다. `available`이 아니면 네트워크 grant가 반영돼도 즉시 접속 가능하다고 단정하지 않는다.
3. Bastion public IP: `ops_aws_get_service`의 `bastion_instances[]`에서 정확한 dev bastion 항목의 `public_ip`를 우선 사용한다. read 응답에 없을 때만 성공한 해당 `tf-apply` 로그에서 `bastion_public_ip` 한 줄만 최소 필터로 추출한다. app EC2 public IP를 Bastion IP로 대신 쓰지 않으며, 최종 안내에 `<bastion_public_ip>` placeholder를 남기지 않는다.
4. identity: dev 읽기 전용 요청은 `user=readonly`, `dbname=appdb`로 완성한다(surface `usage`도 dev를 `readonly`로 안내한다). 계정 존재의 근거는 이번 grant 흐름에서 성공한 `rds-readonly-user` run(멱등 dispatch — 위 정책)이다. 과거 실행 기록이나 "상시 계정" 문구만으로 존재를 주장하지 않는다 — 이전 회수로 DROP됐을 수 있다. 최종 보고에서 계정을 `🟢 확인됨`처럼 실시간 조회된 리소스로 표시하지 말고, `접속 계정: readonly · SELECT 전용 (이번 rds-readonly-user run으로 보장)`처럼 근거를 명시한다. 실제로 확인한 것은 Bastion SG ingress, RDS `available` 상태와 endpoint, apply·ansible run 성공이다.
5. 수명: dev entry에 `expires_at`이 없으면 영구 grant다. 범용 `usage`의 cron 회수 문구를 제거하고 자동 만료를 암시하지 않는다.
6. 최종 명령: 실제 Bastion IP와 RDS endpoint를 모두 치환하고, 터널 터미널과 별도 `psql` 터미널을 분리해 제공한다. 비밀번호는 playbook의 readonly 데모 값임을 밝히고, 사용자가 값을 물으면 위 체크리스트 항목(dev readonly)대로 실값을 알려준다.

`ops_aws_get_service`는 Bastion 공인 IP를 `bastion_instances[].public_ip`로, RDS endpoint를 `rds_instances[].endpoint`로 반환한다. 예외적으로 read 응답에 값이 비어 있으면(전파 지연 등) 이미 성공한 `tf-apply`의 GitHub Actions 로그에서 Terraform output인 `bastion_public_ip`와 `db_endpoint`만 최소 필터로 읽는 read-only fallback을 쓴다. 같은 로그의 SG rule 생성(`Creation complete ... [id=sgr-...]`)은 AWS 생성의 보조 감사 근거다. 서비스 app 인스턴스가 running이라는 사실을 Bastion/RDS 접근 검증으로 대신 사용하거나 값을 추측하지 않는다.

### apply run 인덱싱 지연 시 read-only 발견 절차

`ops_github_get_pr_status`가 PR의 MERGED 상태는 반환하지만 `apply_runs`를 계속 빈 배열로 반환할 수 있다. 2분 폴링 후에도 비어 있으면 곧바로 apply 미실행으로 단정하지 말고, GitHub CLI의 read-only run 목록에서 해당 PR 번호·제목과 일치하는 `tf-apply` push run을 찾는다.

```bash
gh run list --repo <owner>/<repo> --limit 30 \
  --json databaseId,name,workflowName,displayTitle,status,conclusion,createdAt,headSha,url,event
```

- `workflowName == "tf-apply"`인 run을 찾는다. 현재 apply는 `event == "workflow_dispatch"`로도 실행될 수 있으므로 `push`만 허용하는 필터를 쓰지 않는다.
- `displayTitle`의 PR 제목·`#<PR번호>`, head SHA, 생성 시각 중 가능한 식별자를 교차 확인해 해당 변경과 연결한다. 이름·시각만 비슷한 run은 다른 변경일 수 있으므로 사용하지 않는다.
- `ops_github_get_pr_status`가 이미 해당 PR의 `apply_runs[].run_url`을 반환했다면 그 URL을 우선 사용하고, CLI 검색은 인덱싱 지연으로 배열이 비어 있을 때만 fallback으로 쓴다.
- 찾은 URL은 `ops_github_get_workflow_run`으로 다시 확인해 `completed/success`를 검증한다. CLI 목록만으로 ④ 성공을 주장하지 않는다.
- run을 찾지 못하면 기존 runbook대로 `🕓 apply run 미발견`으로 남긴다.
- 성공 run을 찾은 뒤 로그에서 SG rule ID와 Terraform output을 추출한다. SG rule 생성은 AWS 반영 근거지만 요청자 PC에서의 실제 SSH 성공과는 분리해 ⑤를 보고한다.

예시 로그 필터:

```bash
gh run view <run-id> --repo <owner>/<repo> --log \
  | python3 -c 'import sys,re
for line in sys.stdin:
    if re.search(r"bastion_public_ip|db_endpoint|Creation complete.*sgr-", line): print(line, end="")'
```

이 필터는 접속 정보와 SG rule ID만 추출한다. 전체 Actions 로그나 `no_log` 주변을 출력해
secret을 노출하지 않는다. 접속 명령은 placeholder 없이 실제 Bastion IP와 DB endpoint를 넣는다.

```bash
ssh -i ~/.ssh/ops-agent-iac \
  -o ExitOnForwardFailure=yes \
  -N \
  -L 15432:<db-endpoint>:5432 \
  ubuntu@<bastion-ip>
```

다른 터미널:

```bash
psql "host=127.0.0.1 port=15432 dbname=appdb user=<db-user> sslmode=require"
```

- `-N` 터널 터미널에는 셸/프롬프트가 없다 — 거기에 입력한 SQL은 아무 데도 전달되지 않는다. 안내 메시지에 "터널 터미널은 열어 둔 채, SQL은 별도 터미널의 psql에서 실행"을 명시한다.
- `<db-user>`는 prod면 발급한 임시 계정, dev면 상시 `readonly` 계정이다. 발급·프로비저닝되지 않은 계정을 완성된 자격증명처럼 표현하지 않는다.
- prod 임시 비밀번호를 전달할 때는 `PGPASSWORD=... psql ...`처럼 비밀번호가 shell history·프로세스 인자에 남는 완성 명령을 만들지 않는다. 위 `psql` 명령은 비밀번호 없이 제공하고, 프롬프트가 나오면 응답으로 전달한 임시 비밀번호를 입력하라고 안내한다.
- 최종 접속 정보는 실제 `bastion_public_ip`, `db_endpoint`, `temp_user`를 모두 치환해 그대로 붙여넣을 수 있게 제공한다. 비밀번호는 workflow가 `completed/success`이고 role 생성 로그까지 확인된 뒤에만 별도 항목으로 전달한다.

## `rds-temp-user` 성공인데 비밀번호가 반환되지 않는 경우

`ansible-ops`가 `completed/success`여도 `ops_run_ansible_playbook` 응답에
`temp_password`가 없고 Actions 로그에는 `로그 마스킹됨`만 남을 수 있다. 이 경우 role 생성과
자격증명 전달은 서로 다른 판정이다.

1. workflow success와 `temp user <name> state=present ... (readonly, valid until ...)` 메시지는
   role 생성 근거로만 쓴다.
2. apply 로그는 `bastion_public_ip|db_endpoint|Creation complete.*sgr-`처럼 필요한 Terraform
   output과 SG rule ID만 필터링한다. Ansible 로그도 `temp user|temp_user|valid_until|VALID UNTIL|로그 마스킹됨`처럼
   최소한으로 확인하며 전체 로그를 게시하지 않는다. 실제 workflow 출력에서는 최종 검증 문구가
   `temp user <name> state=present on <endpoint> (readonly, valid until <time>)` 형태일 수 있으므로,
   이 한 줄과 workflow `completed/success`를 함께 role 생성 근거로 사용한다. 예:
   ```bash
   gh run view <run-id> --repo <owner>/<repo> --log \
     | python3 -c 'import sys,re
   for line in sys.stdin:
       if re.search(r"temp user|temp_user|valid_until|VALID UNTIL|로그 마스킹됨", line, re.I):
           print(line, end="")'
   ```
   이 필터에 workflow 셸 스크립트의 `temp_password=${TEMP_PW}` 같은 *변수명*이 나타나도 실제
   비밀번호가 반환된 것은 아니다. `gh run view --log`에는 실행된 셸 스크립트 원문과 ANSI
   escape가 섞일 수 있으므로, `TEMP_USER`·`VALID_UNTIL` 입력 echo나 스크립트의 안내 문구도
   role 생성 증거로 세지 않는다. 실제 Ansible 결과의 `msg`에
   `temp user <name> state=present on <endpoint> (readonly, valid until <time>)`가 기록됐는지와
   workflow `completed/success`를 함께 확인한다. 도구 응답에 평문 `temp_password` 값이 있거나
   승인된 정본 전달 경로에서 실제 값을 확인했을 때만 자격증명 전달 완료로 판정한다.
3. 응답에 실제 `temp_password` 값이 없으면 값을 추측하거나 마스킹을 우회하지 않는다.
   같은 playbook을 재실행해도 전달 경로가 달라지지 않으므로 비밀번호 확보 목적으로 반복
   실행하지 않는다.
4. 최종 verdict는 `부분 완료`로 하고 `네트워크 grant ✅`, `임시 role 생성 ✅`,
   `비밀번호 전달 ❌/미확인`을 분리한다. SSH tunnel과 `psql` 명령은 실제 Bastion IP,
   DB endpoint, 사용자명으로 완성할 수 있지만, 비밀번호가 없어 **현재 접속 가능**하다고
   표현하지 않는다.
5. 만료 시각은 네트워크 grant와 role 모두에 그대로 유지된다. 자격증명 전달 실패를 이유로
   임의 연장하거나 grant를 제거하지 않는다.

이 상태는 role 생성 실패가 아니므로 아래의 실패 재시도 절차를 적용하지 않는다.

## 최종 전달 전 일관성 검증 체크리스트

prod 임시 계정 발급이 성공했더라도 접속 정보를 전달하기 직전에 아래 근거를 한 번에 맞춘다.
각 근거는 서로 대체하지 않는다.

1. `ops_github_get_pr_status`에서 PR이 `MERGED`이고 `guard`·`plan`이 최종 `success`인지 확인한다.
2. `tf-apply` run이 `completed/success`인지 확인하고, 그 로그의 `bastion_public_ip`와
   `db_endpoint`만 최소 필터로 추출한다. `ops_aws_get_service`의 정확한 prod RDS endpoint와
   서로 다르면 값을 임의 선택하지 말고 불일치로 보고한다.
3. `ops_aws_get_service`에서 정확한 prod Bastion SG에 요청 CIDR의 TCP 22 규칙이 있고,
   prod RDS가 `available`·`publicly_accessible=false`인지 확인한다.
4. `rds-temp-user` run의 `head_sha`가 앞선 apply의 `head_sha`와 같은지 확인하고,
   `completed/success`와 실제 Ansible 결과의 `temp user <name> state=present ...
   (readonly, valid until <time>)`, `unreachable=0`, `failed=0`을 함께 확인한다. workflow input echo나
   셸 스크립트 원문은 role 생성 증거가 아니다.
5. 마지막 현재 UTC를 새로 읽어 공통 `valid_until`까지 남은 시간을 계산한다. 최종 안내에는
   단순히 "1시간 사용 가능"이라고 쓰지 말고 절대 만료 시각과 전달 시점 기준 잔여 분을 쓴다.
6. `temp_password`는 위 검증이 모두 성공한 run의 dispatch 응답에서 받은 값만 전달한다.
   SSH tunnel 명령과 `psql` 명령에는 실제 Bastion IP·RDS endpoint·사용자명을 넣되,
   비밀번호는 command line이나 `PGPASSWORD`에 넣지 않고 psql 프롬프트에서 입력하도록 안내한다.

### 최종 증거를 한 번에 최소 수집하는 패턴

apply와 Ansible이 모두 끝난 뒤에는 전체 로그를 여러 번 읽지 말고, 아래처럼 두 run에서 필요한
증거만 한 번에 필터링한 뒤 현재 UTC도 같은 검증 단계에서 새로 읽는다. 이 패턴은 로그에 있을 수
있는 secret을 불필요하게 컨텍스트나 Slack으로 가져오지 않으면서 Bastion IP·RDS endpoint·SG rule,
실제 role 생성 메시지·`PLAY RECAP`을 함께 대조하는 데 유용하다.

```bash
set -o pipefail
printf '%s\n' '--- APPLY EVIDENCE ---'
gh run view <apply-run-id> --repo <owner>/<repo> --log \
  | python3 -c 'import sys,re
for line in sys.stdin:
    if re.search(r"bastion_public_ip|db_endpoint|Creation complete.*sgr-", line):
        print(line, end="")'
printf '%s\n' '--- ANSIBLE EVIDENCE ---'
gh run view <ansible-run-id> --repo <owner>/<repo> --log \
  | TEMP_USER='<temp-user>' python3 -c 'import os,sys,re
u = re.escape(os.environ["TEMP_USER"])
p = re.compile(rf"temp user {u} state=present|PLAY RECAP|unreachable=|failed=", re.I)
for line in sys.stdin:
    if p.search(line): print(line, end="")'
printf '%s\n' '--- NOW ---'
date -u '+%Y-%m-%dT%H:%M:%SZ'
```

- `temp_password`·`TEMP_PW`·workflow input 전체를 검색 패턴에 넣지 않는다. 비밀번호의 정본은
  성공한 `ops_run_ansible_playbook` dispatch 응답뿐이다.
- 필터 결과의 `temp user <name> state=present ...` 실제 Ansible `msg`와 `unreachable=0`,
  `failed=0`을 확인한다. 단순 input echo나 실행된 셸 스크립트 원문은 성공 증거가 아니다.
- 추출된 `db_endpoint`는 `ops_aws_get_service`의 정확한 prod RDS endpoint와 다시 대조한다.
- 마지막 UTC를 기준으로 남은 분을 도구로 계산하고, 최종 응답에는 “1시간 사용 가능” 대신
  절대 만료 시각과 전달 시점 기준 잔여 시간을 쓴다.

최종 verdict는 `네트워크 grant 및 임시 계정 발급 완료 — 요청자 PC에서 아래 명령으로 접속
확인`처럼 쓴다. SG 반영과 role 발급은 완료 사실이지만, 요청자 CIDR에서의 실제 SSH 성공은
에이전트가 검증하지 않았으므로 `접속 가능 확인 완료`라고 과장하지 않는다.

## `temp_password` 전달 계약 (현행)

`ops_run_ansible_playbook`은 `rds-temp-user` + `state=present` dispatch 시 비밀번호를
플러그인이 직접 생성해 workflow에 전달하고, 같은 값을 응답의 `temp_password`로 반환한다.

- 응답에 `temp_password`가 있으면: workflow 성공(`completed/success`)과 role 생성 근거를
  확인한 뒤, 그 값을 최종 접속 안내(`psql` 명령)와 함께 요청 응답으로 전달하고 정상 완료로
  판정한다. 자격증명은 응답으로만 전달한다 — 별도 안전 채널 절차를 지어내지 않는다.
- workflow가 실패하면 반환된 비밀번호는 실제 role과 무관하다 — 전달하지 말고 실패 처리
  절차를 따른다.
- `state=absent`(DROP)에는 비밀번호가 없다.
- 응답에 `temp_password`가 없는 구버전 조합에서만 위의 미반환 절차를 적용한다. 마스킹된
  Actions 로그를 파싱해 비밀번호를 복구하려 하지 않는다.
- dispatch input에 값이 남는 것은 이 실습 위협모델(본인 계정, 시한부 role)에서 수용된
  설계다 — 암호화 artifact·secret broker 같은 추가 레이어를 제안하지 않는다.

## `rds-temp-user` 실패 처리

네트워크 grant의 Terraform apply와 PostgreSQL role 발급은 서로 독립된 결과다. 전자가
성공하고 후자가 실패할 수 있으므로 하나의 통합 성공/실패로 뭉개지 않는다.

1. `ops_github_get_workflow_run`으로 `ansible-ops`가 `completed/success`인지 확인한다.
   네트워크 grant PR 뒤 이어서 실행한 경우에는 Ansible run의 `head_sha`가 방금 반영된
   Terraform apply의 `head_sha`와 일치하는지도 대조한다. 불일치하면 stale main 구성으로
   실행된 것이므로 role 발급 성공 근거로 쓰지 말고, 최신 SHA가 반영된 run을 다시 dispatch해
   완료까지 확인한다.
2. 실패하면 read-only 로그에서 `TASK [...]`, `fatal:`, `PLAY RECAP`만 추려 실패 task를
   식별한다. secret이 섞일 수 있는 전체 로그를 Slack에 붙이지 않는다.
3. `no_log: true` 때문에 결과가 `censored`라면 원인·부분 생성 여부·비밀번호를 추측하지
   않는다. 같은 입력으로 한 차례 재시도는 허용하되 동일 task에서 다시 실패하면 중단한다.
4. role 생성 전에 `Ensure psql present` 같은 준비 task에서 Bastion SSH `UNREACHABLE`·
   `Connection timed out`으로 실패한 경우도 일시적 연결 실패일 수 있으므로, 로그에서 실패
   task와 `PLAY RECAP`만 최소 확인한 뒤 동일 `temp_user`·`valid_until`·`grant_mode`로 실제
   run을 딱 한 번 재시도한다. 재시도도 실패하면 더 반복하지 않는다.
   - 각 `state=present` dispatch는 새 `temp_password`를 반환할 수 있다. 실패한 run이 반환한
     비밀번호는 실제 role의 자격증명으로 간주하거나 사용자에게 전달하지 않는다.
   - 재시도 workflow가 `completed/success`이고 실제 Ansible 결과의 `temp user <name>
     state=present ...` 문구까지 확인된 경우에만 *재시도 응답의* `temp_password`를 전달한다.
   - 두 run의 `head_sha`가 반영된 merge commit과 일치하는지도 각각 대조하며, 재시도 성공을
     첫 run 성공으로 바꾸어 기록하지 않는다. 최종 보고에는 첫 실패 원인과 성공한 재시도 run
     URL을 분리해 감사 근거를 남긴다.
5. 최종 보고는 `네트워크 grant ✅`와 `임시 DB 계정 ❌`를 분리한다. 실제 사용자명과
   비밀번호가 확인되지 않았으면 `psql` 명령은 향후 사용할 형태로만 제공하고 현재 사용
   가능하다고 말하지 않는다.
6. 네트워크 grant의 `expires_at`과 cron 회수는 role 발급 실패와 무관하게 유지된다.
   불필요한 네트워크 grant를 즉시 회수해 달라는 별도 요청이 없으면 임의 제거하지 않는다.

prod 승인 대기로 시간이 지난 뒤 role을 만들 때는 실행 직전에 현재 UTC와 `valid_until`을
다시 비교한다. 이미 만료됐거나 사용 가능한 시간이 거의 남지 않았다면 과거 시각으로
Ansible을 실행하지 않는다. 그렇다고 요청한 2시간을 승인 시점부터 임의 재시작하지도 말고,
새 만료 시각이 필요한지 사용자에게 확인한다.

최종 자격증명 전달 직전에도 현재 UTC를 한 번 읽어 `valid_until`까지 남은 시간을 계산한다.
기본 1시간 grant는 최초 요청 시각부터 흐르므로 PR·apply·Ansible에 걸린 시간이 빠진다. 최종
응답에서 단순히 "1시간 사용 가능"이라고 쓰지 말고 공통 절대 만료 시각과 전달 시점 기준의
대략적인 잔여 시간을 함께 적는다. 잔여 시간이 충분하면 그대로 전달하고, 비정상적으로 짧으면
임의 연장하지 말고 새 만료 시각을 확인한다. 이 계산은 네트워크 grant와 PostgreSQL role의
공통 만료 시각을 바꾸지 않는 보고·판단용 검증이다.

## 검증 문구

`tf-apply` 로그의 `Creation complete ... [id=sgr-...]`는 AWS가 SG rule resource를 생성했다는 보조 근거다. 그러나 요청자 CIDR에서의 실제 TCP/SSH 성공을 독립 검증한 것은 아니다.

- ④ apply: workflow 성공 여부만 기록한다.
- ⑤ 반영 확인: `ops_aws_get_service`에서 정확한 대상 Bastion SG에 요청 `CIDR:22`가 확인되면 `✅ SG 규칙 반영 (<cidr>:22) · 요청자 PC 접속 확인 권장`으로 쓴다. SG 반영 자체는 실제 read 결과로 완료됐으므로 단순히 `🕓 요청자 PC에서 접속 확인 필요`로 낮추지 않는다.
- 대상 Bastion SG에 요청 규칙이 없으면 `❌ SG 규칙 미반영`으로 쓴다. apply 성공이나 로그의 SG rule ID만으로 이를 ✅ 처리하지 않는다.
- 첫 verdict와 본문에서는 `네트워크 grant 반영 확인`과 `요청자 PC의 실제 터널 성공 미검증`을 분리한다. `접속 가능 확인`처럼 source 측 성공까지 단정하지 않는다.
- **최종 문장 교정:** SG 규칙만 확인한 상태에서는 `아래 명령으로 접속할 수 있습니다`도 실제 client 성공을 이미 확인한 것처럼 읽히므로 쓰지 않는다. 대신 `아래 명령으로 접속을 시도해 주세요` 또는 `요청자 PC에서 아래 명령으로 접속 확인이 필요합니다`라고 쓴다. 타임라인 ⑤가 ✅인 것은 SG 반영 완료를 뜻할 뿐, 터널 성공까지 뜻하지 않는다.
- 에이전트 호스트에서 Bastion 22를 probe하지 않는다. source IP가 달라 검증 근거가 되지 않는다.
