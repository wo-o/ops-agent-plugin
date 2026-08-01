# EC2 SSH 임시 접근 체크리스트

`<env>-ec2-ssh` surface로 단일 요청자 IP의 TCP 22 접근을 제한된 시간 동안 열 때 사용한다.

## 입력 확정

- 환경(`dev`/`prod`)을 확인한다.
- `내 IP`는 요청자 PC의 공인 IPv4를 직접 받아야 한다. 에이전트·게이트웨이·러너의 IP를 대신 쓰지 않는다.
- 이전 메시지에서 요청자 IP를 물었고 사용자가 Slack 스레드 답글로 IPv4만 보내면, 이는 입력 확정 답변으로 처리한다. 환경·요청 목적은 원문에서 이어받아 재질문하지 말고 즉시 같은 변경을 계속한다.
- `clarify`로 공인 IPv4를 요청한 뒤 같은 턴의 응답으로 IPv4가 반환된 경우에도 대화가 끊긴 것으로 취급하지 않는다. 반환값을 `/32`로 정규화해 즉시 PR 생성·머지/apply 폴링·SG 검증·실제 인스턴스 IP가 포함된 SSH 명령 제공까지 이어간다.
- Slack 스레드의 직전 문맥이 특정 환경의 ALB 뒤 app EC2 상태·인스턴스 목록이고, 사용자가 공인 IPv4를 주며 `이 IP로 열어줘`라고 답했으며 DB/RDS/Bastion 언급이 없다면 `<env>-ec2-ssh` 요청으로 이어서 처리한다. 반대로 직전 문맥이 RDS/Bastion 접근이면 `<env>-db-access`로 라우팅한다. 서로 충돌하는 문맥이 있거나 열 대상 포트가 별도로 언급되면 추측하지 말고 한 번만 확인한다.
- 받은 값은 PR 생성 전에 공인 IPv4 단일 주소인지 확인한다. IPv6, 사설·예약 대역, hostname, URL, 포트가 붙은 값은 그대로 CIDR로 만들지 말고 올바른 공인 IPv4를 다시 받는다. 유효한 단일 IPv4만 `/32`로 정규화한다.
- IP 확인용 명령은 요청자 PC에서 실행하게 한다. 에이전트가 실행한 `ifconfig.me` 결과는 요청자 IP의 검증이나 대체값으로 사용하지 않는다.
- dev 요청에 만료 조건이 없으면 별도의 만료 확인 질문을 추가하지 않고 영구 grant로 진행한다. IP를 되물을 때 `만료 요청이 없어 영구 적용`임을 함께 밝혀 요청자가 필요하면 그 자리에서 기간을 지정할 수 있게 한다.
  - 권장 질문 문구: `본인 PC에서 curl -4 -s ifconfig.me를 실행해 나온 공인 IPv4를 보내주세요. 해당 단일 IP만 /32로 열며, 만료 요청이 없어 영구 적용합니다.`
  - 질문에는 다음 작업(PR 생성·반영 확인·실제 접속 명령 제공)을 짧게 덧붙여도 되지만, 영구 적용 고지를 생략하지 않는다.
- 상대 만료(`2시간 뒤`)는 요청자 IP를 받은 뒤 현재 UTC를 도구로 읽어 절대 ISO8601 UTC `expires_at`으로 계산한다.
  - 현재 시각과 만료 시각은 각각 독립된 `date` 호출로 얻는다. 한 번의 `date -d '+2 hours'` 기준으로 둘 다 출력하면 `now`까지 미래 시각으로 잘못 기록될 수 있다.
  - 권장 형태: `printf 'now='; date -u '+%Y-%m-%dT%H:%M:%SZ'; printf 'expires='; date -u -d '+2 hours' '+%Y-%m-%dT%H:%M:%SZ'`. 첫 PR 시도 전에 계산한 절대 `expires_at`을 재시도에서도 그대로 써서 요청한 종료 시각을 암묵적으로 연장하지 않는다.
- 사용자가 `cron으로 자동 회수`를 요청해도 별도의 Hermes `cronjob`을 만들지 않는다. `<env>-ec2-ssh` entry의 `expires_at`이 기존 repository `access-expiry` cron의 회수 입력이므로, PR에는 절대 만료 시각만 기록하고 최종 보고에서 해당 cron이 회수한다고 안내한다. 별도 스케줄을 중복 생성하면 동일 grant에 회수 경로가 둘 생겨 감사와 실패 원인 추적이 어려워진다.
- `entry_key`와 `description`은 ASCII로 작성한다. 동일 환경·동일 CIDR의 중복 key를 만들지 않는다.
- `entry_key`는 IP 주소가 아니라 요청자·용도를 나타내는 안정적인 ASCII 식별자(예: `jin-ssh`)로 잡는다. 이후 같은 사용자의 공인 IP가 바뀌면 새 key를 추가하지 않고 기존 key를 갱신해 이전 규칙이 함께 제거되도록 한다. Slack 표시명이 한글·비ASCII여도 SG description에 그대로 복사하지 말고, 문맥상 명확한 ASCII 이름을 사용한다. 적절한 로마자 표기가 불명확하면 이름을 임의 번역하지 말고 중립적인 ASCII 용도 식별자를 쓴다.

## 변경 및 추적

1. `ops_github_open_tfvars_pr(surface="<env>-ec2-ssh", op="set_entry", ...)`로 PR을 연다.
2. prod도 정책상 승인 대기라고 단정하지 않는다. 실제 PR 상태를 폴링한다.
3. `checks`에서 plan/guard의 최종 success를 확인한다.
4. MERGED가 관측되면 `apply_runs`의 `tf-apply` URL을 얻어 `completed/success`까지 확인한다.
5. 최종 보고 직전에 PR 상태를 다시 읽어 중간 check 상태를 최종값으로 남기지 않는다.
6. 최종 보고에는 PR URL과 실제 `tf-apply` run URL을 모두 남긴다. PR은 구성 변경의 감사 근거이고 apply run은 live 실행의 감사 근거이므로, 둘 중 하나로 다른 하나를 대체하지 않는다.

## 검증 경계

- `tf-apply success`와 prod app EC2 `running`은 확인할 수 있다.
- apply 뒤 `ops_aws_get_service`의 `security_groups[].ingress`에서 요청 `CIDR:22`(protocol tcp, from_port≤22≤to_port) 규칙이 실재하는지 대조해 SG 반영을 검증한다. 있으면 `✅ SG 규칙 반영 (<cidr>:22)`, 없으면 `❌ SG 규칙 미반영`. SG 규칙 반영은 확인되지만 요청자 client의 실제 접속까지 증명한 것은 아니므로 `접속 가능`으로 단정하지 않고 접속 명령을 안내한다.
- 에이전트 호스트의 TCP probe는 요청자 source CIDR 검증을 대신하지 못한다.
- `ops_aws_get_service(prefix="ops-agent-iac")` 결과에서 요청 환경의 running app 인스턴스만 골라 각 실제 `public_ip`를 SSH 명령에 넣는다. placeholder나 AWS CLI 조회 명령을 사용자에게 남기지 않는다.
- prod PR이 CODEOWNERS 승인 대기에서 멈춰 아직 SG 규칙이 미반영이어도, 사용자가 접속 명령을 함께 요청했다면 `ops_aws_get_service(prefix="ops-agent-iac")`로 현재 prod running app의 실제 공인 IP를 확인해 명령을 미리 완성한다. 단, 타임라인은 ③ 승인 대기에서 끊고 명령 앞에 `승인·apply 완료 후 사용`을 명시한다. 현재 접속 가능하다고 표현하거나 ④·⑤를 미래 상태로 나열하지 않는다. 승인 지연 중 인스턴스 교체 가능성이 있으므로 후속 반영 확인 요청이 오면 IP도 다시 조회한다.
- app 인스턴스가 여러 대면 대표 명령 하나로 축약하지 말고, 각 `instances[].name`과 실제 `public_ip`를 한 쌍으로 열거해 사용자가 원하는 노드를 바로 선택할 수 있게 한다. 예:
  ```
  - `<prefix>-dev-app-0` — `<actual-public-ip-0>`
    ssh -i ~/.ssh/ops-agent-iac ubuntu@<actual-public-ip-0>
  - `<prefix>-dev-app-1` — `<actual-public-ip-1>`
    ssh -i ~/.ssh/ops-agent-iac ubuntu@<actual-public-ip-1>
  ```
  이 예시의 placeholder는 문서 설명용일 뿐이며 실제 사용자 응답에는 반드시 read 도구로 확인한 리터럴 IP를 넣는다.
- dev에서 `expires_at` 없이 적용한 grant는 최종 보고에 `만료 요청이 없어 영구 적용`임을 짧게 명시하고 cron 자동 회수 문구는 넣지 않는다.
- 기존 allowlist 엔트리의 IP를 교체하는 요청은 같은 `entry_key`를 `set_entry`로 갱신한다. apply 뒤 정확한 `<prefix>-<env>-ec2` SG에서 새 `CIDR:22`가 존재하고 이전 `CIDR:22`가 사라졌는지 모두 대조한다. 새 규칙 존재만 확인하고 이전 규칙 제거를 추정하지 않는다.
- IP 교체 요청에서 사용자가 `만료는 동일하게 2시간`처럼 상대 기간을 다시 지정하면, 기존 절대 만료 시각을 유지한다는 뜻으로 해석하지 않는다. 갱신 요청을 받은 현재 UTC 기준으로 새 `expires_at`을 계산한다. 사용자가 `기존 만료 시각 유지`라고 명시한 경우에만 기존 절대 시각을 재사용한다.
- IP 교체 최종 보고의 ⑤에는 `새 IP 반영 · 이전 IP 제거 확인`을 함께 적어 두 조건을 독립 검증했음을 드러낸다.
- 같은 CIDR이 반대 환경의 EC2 SG나 monitoring·Bastion·기타 프로젝트 SG에도 존재할 수 있다. 중복 여부와 반영 판정은 환경별 tfvars 및 정확한 `<prefix>-<env>-ec2` SG를 기준으로 한다. 다른 SG의 동일 CIDR을 이번 변경의 증거나 충돌·범위 누출로 간주하지 않으며, 해당 규칙의 변경 전 상태나 PR diff에 인과 근거가 없으면 이번 요청 때문에 생겼다고 암시하지 않는다. 특히 요청 CIDR이 monitoring SG에 이미 보여도 대상 prod/dev EC2 SG에 없으면 SSH 반영 완료가 아니다. 요청 환경의 완료 판정과 접속 명령에는 요청 환경의 EC2 SG 및 running app 인스턴스만 포함한다. 반대 환경이나 비대상 SG에서 우연히 발견한 동일 CIDR은 사용자가 영향 범위·중복 규칙을 묻지 않은 한 최종 보고에 굳이 노출하지 않는다. 이는 대상 환경의 현재 미반영 상태를 흐리고 불필요한 범위 누출 오해를 만들 수 있다.

## 만료 검토

- prod 승인·apply 지연으로 실사용 시간이 줄 수 있으므로 apply 후 현재 UTC와 `expires_at`을 도구로 다시 읽어 대조한다.
- 최종 보고에서는 요청 시점 기준 만료 시각과 실제 apply 완료 뒤 남은 접근 시간을 구분한다. apply 지연이 있었다면 `2시간 사용 가능`이라고 다시 표현하지 말고, 절대 UTC 만료 시각을 정본으로 안내한다.
- 이미 만료됐거나 남은 시간이 거의 없으면 임의 연장하지 않는다. 실제 상태를 알리고 새 만료 시각을 요청한다.
- 최종 보고에 절대 UTC 만료 시각과 cron 자동 회수를 명시한다. 현지 시각을 함께 표기하려면 암산하지 말고 시간 도구로 변환한다.

## 권장 verdict

SG ingress에서 요청 `CIDR:22` 규칙을 확인했고 요청자 client 접속은 미검증일 때:

`✅ apply 완료 — SG 규칙 반영 확인, 요청자 PC에서 아래 명령으로 접속`

타임라인의 ⑤:

`✅ SG 규칙 반영 (<cidr>:22) · 요청자 PC 접속 확인 권장`

`security_groups[].ingress`에 요청 규칙이 없으면 ⑤는 `❌ SG 규칙 미반영`.
