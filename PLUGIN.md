# ops — 실습용 Hermes 플러그인 (read + 봇 write)

IaC 실습(ops-agent-iac) 전체에서 에이전트가 **인프라를 조회(read)** 하고, **변경을
가드레일 안에서 제안·조치(write)** 하게 해주는 플러그인. 운영 환경용 원본 ops
플러그인에서 실습에 필요한 것만 떼어 실습 환경(Name 태그 · 공유 모니터링 스택 ·
GitHub App)에 재타깃한 축소판이다.

toolset은 **권한 경계(read/write)**로 나뉜다. read 1종 + write 2종:

- **ops-read** (15) — 조회 전부(read-only): AWS(서비스 헬스·ALB 타깃·비용·미사용 리소스),
  관측 스택(Prometheus 메트릭·Loki 로그·Grafana 알람), GitHub(PR 상태·워크플로 run·
  리포 파일 읽기), Cloudflare(DNS 레코드·WAF 룰), PagerDuty(인시던트·온콜). 자격증명이 없는 서비스의
  도구는 check gate가 알아서 숨긴다 — 서비스별로 toolset을 쪼갤 이유가 없다.
- **ops-github-write** (2) — 첫 번째 쓰기 경로: 봇 이름으로 PR을 연다. apply는
  안 한다. (a) `ops_github_open_tfvars_pr` — surface tfvars 한 파일 변경(dev·prod).
  (b) `ops_github_open_code_pr` — **dev 한정** IaC 코드 PR(2-1-dev/·modules/·
  ansible/, 2026-07-20 개방; `.github/`·`scripts/`·`2-0-setup/`·`2-2-prod/`는
  도구가 거부). 자동 머지 vs 사람 승인은 이 도구가 아니라 repo의
  **CODEOWNERS + ruleset**이 정한다 — dev는 surface·코드 경로 모두 무소유라 guard
  통과 시 자동 머지, prod는 전부 소유라 code-owner 리뷰 대기(코드는 dev→main 승격
  PR로만 도달). guard(plan + 비용 백스톱 + ansible syntax) check는 필수. 코드 PR
  전 현재 내용 확인용 `ops_github_read_file`은 ops-read에 있다.
- **ops-ansible-write** (1) — 두 번째 쓰기 경로: **PR 없이** bounded remediation
  playbook을 즉시 실행한다(인시던트 대응의 런타임 조치 — 롤링 재시작·디스크
  확장·보안 패치). 단 이 호스트에서 직접 SSH로 돌리지 않고 IaC 리포의 `ansible-ops.yml`
  워크플로를 GitHub App으로 **workflow_dispatch**한다(실제 실행은 VPC 안 self-hosted
  러너). tfvars로 못 되돌리는 서버 상태 전용. 안전은 카탈로그 봉쇄(고정 playbook 키)·
  params 검증·`--limit env_dev|env_prod` fleet 스코프·`dry_run`(--check)으로 잡고, 장애
  주입처럼 서비스를 해치는 playbook은 카탈로그에 없다. change 경로와 같은 GitHub App 자격증명
  (`OPS_GITHUB_*`)이 있어야 노출된다.

스킬 3개도 함께 등록된다:

- **ops-operating** — read 질문 라우팅 runbook(헬스·알람·메트릭·비용·PR 상태 등).
- **ops-change** — 사용자 요청 기반으로 surface를 변경할 때의 판단 runbook. read
  도구로 현재 상태를 확인하고 알맞은 surface를 정한 뒤 `ops_github_open_tfvars_pr`로
  PR을 연다. 기존 surface로 안 되는 신규 인프라는 dev면 코드 PR
  (`ops_github_open_code_pr`, "dev 코드 PR" 절), prod면 승격 PR 안내.
  자동 머지 vs 사람 승인은 인프라 repo의 CODEOWNERS(소유 surface=code-owner 리뷰)가 정한다.
- **ops-incident-response** — 2-3-incident-response 시나리오 전용. Grafana 알람이 Slack
  채널로 오면 그걸 트리거로 metric/log로 진단하고 알람 종류별 대응을 라우팅한다:
  메모리→롤링 재시작(ansible), 디스크→볼륨 확대(tfvars) + growpart(ansible),
  5xx→WAF rate limit(tfvars), 인증 실패→SSH allowlist 축소(tfvars). 알람이 flapping하면
  자동 대응을 멈추고 온콜을 부르는 서킷 브레이커까지. tfvars PR과 bounded ansible 두
  경로만 쓴다.

> 이 문서 하나로 "슬랙에서 자연어 → 에이전트가 인프라를 읽고, 변경을 봇 PR로
> 연다"까지 처음부터 따라 할 수 있다.

---

## 0. 사전 조건

- Slack에 연결된 Hermes 호스트 (Day 1에서 구성). `hermes gateway status`로 확인.
- ops-agent-iac 리포의 `2-0-setup`이 apply되어 있을 것 — read 롤
  (`<project>-hermes-readonly`)과 write용 OIDC 롤(plan=ReadOnly / apply=PowerUser)이
  여기서 생성된다. (루트 README 참고)
- 호스트에서 AWS 자격증명 해석 가능 (SSO 프로파일 또는 인스턴스 롤).

---

## 1. 플러그인 설치

```bash
# 리포 루트가 곧 플러그인 — Hermes 플러그인 디렉토리에 ops 이름으로 클론
mkdir -p ~/.hermes/plugins && cd ~/.hermes/plugins \
  && git clone https://github.com/wo-o/ops-agent-plugin.git ops
# (또는 한 번에: hermes plugins install wo-o/ops-agent-plugin — 업데이트는 git pull / hermes plugins update ops)
```

파이썬 의존성은 Hermes 에이전트 venv에 있어야 한다 (대개 이미 있음):

```bash
~/.hermes/hermes-agent/venv/bin/python -m pip install boto3 pyjwt cryptography
```

- HTTP 호출은 표준 라이브러리(urllib)만 쓴다 — httpx 등 외부 HTTP 클라이언트 불필요.
- `boto3` = AWS read (없으면 AWS 도구 미노출).
- `pyjwt`+`cryptography` = GitHub App JWT 서명(write 도구) — GitHub 공식 문서가 쓰는 방식.
- 없으면 해당 도구는 그냥 노출되지 않을 뿐(에러 아님) — 있으면 켜진다.

---

## 2. read 배선 (AWS·Grafana·GitHub·PagerDuty·Cloudflare 조회)

`~/.hermes/.env` (chmod 600)에 추가. **기존 줄은 건드리지 말고 append**:

```
OPS_AWS_READ_ROLE=arn:aws:iam::<계정ID>:role/<project>-hermes-readonly
                                           # 기본 프로젝트 이름이면 ops-agent-iac-hermes-readonly
OPS_AWS_REGION=ap-northeast-2          # 기본값이라 생략 가능
OPS_PROJECT_PREFIX=ops-agent-iac      # IaC 리포 PROJECT_NAME과 동일 (기본값이라 생략 가능)
AWS_PROFILE=<SSO 프로파일>                  # 인스턴스 롤이면 생략
OPS_GRAFANA_URL=<terraform output ops_grafana_url>       # 모니터링 스택 안 띄웠으면 생략
OPS_GRAFANA_TOKEN=<terraform output -raw ops_grafana_token>  # 생략 가능
OPS_GRAFANA_PROM_UID=prometheus         # datasource UID override — 기본값이라 생략 가능
OPS_GRAFANA_LOKI_UID=loki               # datasource UID override — 기본값이라 생략 가능
OPS_GITHUB_REPO=<owner>/ops-agent-iac
OPS_PAGERDUTY_TOKEN=<PD read-only REST API 키>  # 생략 가능 — 없으면 PD 도구 미노출; UI 수동 발급
OPS_CLOUDFLARE_READ_TOKEN=<read 전용 zone 토큰>   # 생략 가능 — Zone.DNS Read + Zone.WAF Read
OPS_CLOUDFLARE_ZONE_ID=<zone id>          # OPS_CLOUDFLARE_READ_TOKEN과 함께 설정
```

Grafana 값 두 개는 IaC 리포가 발급한다 — `2-0-setup`(공유 모니터링 서버) apply 후
`terraform output`. PagerDuty의 service/escalation policy도 `2-0-setup`이 IaC로 만든다
(단, read-only API 키 자체는 PD가 API 발급을 지원하지 않아 UI 수동).
절차는 인프라 리포의 `2-0-setup/README.md` 참고.

GitHub 조회 도구(PR/런 상태)는 아래 3단계의 App 토큰을 쓴다 — 별도 PAT는 필요 없다.
App을 배선하지 않고 조회만 쓰려면 read-only PAT를 `GITHUB_TOKEN`에 넣는 폴백도 있다.

자격증명 경계(설계 핵심): 플러그인은 AWS 키를 직접 들지 않는다. 로컬 credential로
`OPS_AWS_READ_ROLE`을 **assume한 세션만** 쓴다 — 도구가 뭘 하든 read 경계가
IAM에서 성립한다. 자격증명이 없는 도구는 모델에 아예 노출되지 않는다(check_fn 게이트).

---

## 3. write 배선 (봇 이름 = GitHub App)

에이전트가 PR을 **봇 이름**으로 열려면 GitHub App이 필요하다. App 생성은 브라우저
클릭 한 번이 필수라 헬퍼로 자동화돼 있다:

```bash
# 헬퍼는 이 repo가 아니라 인프라 repo(ops-agent-iac)의 2-0-setup/2-github/에 있다 — 거기서 실행
cd <ops-agent-iac 경로>
python3 2-0-setup/2-github/create_github_app.py   # 브라우저 1클릭 → .secrets/app.pem + app.json 저장
# 출력된 설치 링크로 가서 자기 ops-agent-iac repo 선택 → Install
python3 2-0-setup/2-github/print_install_id.py    # installation_id 확인
```

절차는 [`2-0-setup/2-github/README.md`](https://github.com/wo-o/ops-agent-iac/blob/main/2-0-setup/2-github/README.md).

그 셋을 `~/.hermes/.env`에 추가:

```
OPS_GITHUB_APP_ID=<app_id>
OPS_GITHUB_PRIVATE_KEY_PATH=/절대/경로/.secrets/app.pem
# 또는 파일 대신 인라인: OPS_GITHUB_PRIVATE_KEY=<PEM 내용>
OPS_GITHUB_INSTALLATION_ID=<installation_id>
# OPS_GITHUB_REPO 는 위(2단계)에서 이미 설정
# prod 삭제 등 승인 요청 시 에이전트가 쓸 실멘션 대상 — <@USERID>/<!subteam^ID> 포맷이어야
# 실제 알림이 간다. IaC repo variable INFRA_SLACK_MENTION과 동일 값 권장. 미설정이면
# 텍스트 멘션 폴백(알림 없음).
OPS_INFRA_SLACK_MENTION=<@Slack user ID>
```

권한: App에는 Contents(write)=브랜치·커밋, Pull requests(write)=PR 열기,
Actions(write)=ansible-ops workflow_dispatch + run 상태 조회(ops-ansible-write)가 있다.
create_github_app.py의 manifest가 이 세 권한을 기본 포함한다 — 기존 App을 쓰던 경우엔
Actions(write)를 추가하고 설치를 재승인해야 ansible write 경로가 동작한다(PR 여는 시연엔 불필요).

---

## 4. toolset 켜기 + 재시작

```bash
hermes plugins enable ops
# enable 중 "replace built-in tools" 권한 질문 → no (built-in 교체 없음 —
# ops_* 도구 등록 + post_tool_call 감사 훅뿐)
hermes tools enable ops-read           # 조회 전부 (read 14도구)
hermes tools enable ops-github-write   # tfvars PR + dev 코드 PR (write 2도구)
hermes tools enable ops-ansible-write  # bounded remediation playbook (GHA dispatch)
```

`ops-ansible-write`는 change 경로와 같은 GitHub App 자격증명(`OPS_GITHUB_APP_ID`/
`_PRIVATE_KEY_PATH`/`_INSTALLATION_ID` + `OPS_GITHUB_REPO`)이 있어야 실제로 노출된다 —
없으면 켜도 조용히 미노출이다. ansible-ops.yml을 dispatch할 뿐이라 호스트에 ansible·SSH
키는 필요 없다.

그리고 Slack에 노출되도록 `~/.hermes/config.yaml`의 두 곳에 toolset을 넣는다
(`hermes tools enable`이 cli만 켜는 경우가 있어 slack 목록을 직접 확인):

```yaml
platform_toolsets:
  slack: [ ..., ops-read, ops-github-write, ops-ansible-write ]
known_plugin_toolsets:
  slack: [ ..., ops-read, ops-github-write, ops-ansible-write ]
```

적용하려면 게이트웨이 재시작 (라이브 봇이 수 초 내려갔다 올라옴):

```bash
hermes gateway restart
```

---

## 5. 로드 검증

```bash
hermes plugins list | grep ops            # enabled 여야 함
hermes tools list | grep -E "ops-(read|github-write|ansible-write)"   # 3 toolset ✓ enabled

# 모델 없이 핸들러가 실제로 도는지 (read + write 자격증명 확인)
~/.hermes/hermes-agent/venv/bin/python - <<'PY'
import os, sys; sys.path.insert(0, os.path.expanduser("~/.hermes/plugins"))
# (또는 tests 폴더의 pytest로 계약 검증)
PY
python3 -m pytest ~/.hermes/plugins/ops/tests -q   # 계약 테스트 통과 확인
```

도구가 안 보이면 자격증명 게이트가 닫힌 것이 정상일 수 있다. env를 먼저 확인하고
`HERMES_PLUGINS_DEBUG=1 hermes plugins list`로 로드 에러를 본다.

---

## 6. 슬랙 프롬프트 모음 (그대로 복붙)

봇을 **@멘션**하고 아래 문장을 그대로 보내면 된다. 자연어라 표현은 바꿔도 되지만,
lab 이름·CIDR 같은 값은 그대로 두는 게 안전하다. (오른쪽 화살표 = 호출되는 도구)

### A. 읽기 — AI가 인프라를 조회한다 (아무것도 안 바꿈)

```
@봇 지난 7일 AWS 비용 요약해줘
@봇 어제 대비 비용 늘어난 서비스 있어?
```
→ `ops_aws_get_cost_summary`

```
@봇 dev 서버 상태 어때?
@봇 prod 인스턴스 상태 어때?
```
→ `ops_get_service_health` / `ops_aws_get_alb_target_health`

```
@봇 dev 리소스 describe 해줘
@봇 dev ALB 타깃 헬시해?
```
→ `ops_aws_get_service` / `ops_aws_get_alb_target_health`

```
@봇 미사용 리소스 후보 뭐 있어?
```
→ `ops_aws_find_unused_candidates` (keep 태그 제외, 삭제는 안 함)

```
@봇 앱 플릿 CPU 알람 왜 떴는지 설명해줘
@봇 앱 플릿 인스턴스별 CPU 지금 얼마야?
@봇 앱 플릿 nginx 5xx 로그 있어?
```
→ `ops_explain_alert` / `ops_query_metrics` / `ops_query_logs` (모니터링 스택 + Grafana 필요)

```
@봇 이 PR 상태 봐줘: https://github.com/<owner>/ops-agent-iac/pull/1
@봇 이 Actions 런 어떻게 됐어: https://github.com/<owner>/ops-agent-iac/actions/runs/<id>
```
→ `ops_github_get_pr_status` / `ops_github_get_workflow_run`

### B. 쓰기 — AI가 변경을 봇 PR로 제안한다 (직접 안 바꿈)

각 요청에 봇이 "PR 열었습니다: …/pull/N"으로 답한다. 그 PR을 열면 **author =
`<app-slug>[bot]`**, 파일은 딱 하나, guard(plan) 체크가 자동으로 돈다. 도구는
`ops_github_open_tfvars_pr` — 자동 머지 vs
사람 승인은 CODEOWNERS가 정한다:
- **무소유 dev 경로(auto)** — dev 브랜치의 2-1-dev/(env.auto.tfvars 포함) ·
  modules/ · ansible/: surface tfvars와 코드 PR 모두 guard 통과 시 자동
  머지·apply (2026-07-20 개방).
- **소유 경로(human)** — prod 전부(2-2-prod/**) · main의 `*.tf` ·
  `.github/`·`scripts/`·`2-0-setup/`: code-owner 리뷰 대기. 코드의 prod 반영은
  dev→main 승격 PR로만.

```
@봇 office IP x.x.x.x/24에서 SSH 되게 dev-ec2-ssh 열어줘
@봇 x.x.x.x/32 하나만 SSH 허용해줘 (dev-ec2-ssh)
```
(dev-ec2-ssh · ec2_ssh_allowlist)

```
@봇 dev 데이터 볼륨 20GB로 키워줘                 (auto — dev surface 무소유)
@봇 dev office IP에서 SSH 열어줘                  (auto — dev surface 무소유)
@봇 prod 데이터 볼륨 30GB로 키워줘                (human — prod 소유, 승인 대기)
```
(앞 둘 = 무소유라 자동 머지, prod 전부는 code-owner 리뷰)

```
@봇 내 IP x.x.x.x/24로 dev DB 2시간만 열어줘 (dev-db-access)
```
(dev-db-access · db_grants — 만료 시각 필수)

```
@봇 e2e-demo.example-lab.test A레코드 x.x.x.x 추가해줘 (dev-dns · Cloudflare→ALB)
@봇 /api/* 경로에 특정 IP rate limit WAF 룰 넣어줘 (waf — prod 전용, 존 전역)
```
(dns/waf = tfvars surface. surface에 없는 신규 인프라 요청은 dev면
`ops_github_open_code_pr`로 dev 브랜치에 코드 PR — 예:
`@봇 dev에 앱 업로드용 S3 버킷 추가해줘. 코드로 직접 만들어도 돼` —
prod면 코드 PR 없이 dev→main 승격 PR을 사람이 승인해야 한다고 안내한다)

되돌리기(회수)도 같은 도구:
```
@봇 아까 연 dev-ec2-ssh의 office-ssh 규칙 다시 빼줘
```

디스크 확장·서비스 재시작·보안 패치 같은 런타임 서버 상태 조치는 tfvars가 아니라
`ops-ansible-write`(bounded playbook)로 하거나, 사람이 로컬에서 SSH ansible로 직접
실행한다 — ops-agent-iac 루트 `ansible/` + 각 시나리오 README 참고.

### 명령 승인(Command Approval) 버튼이 뜨면

에이전트가 `ops_*` 도구가 아니라 raw 셸(aws CLI, IMDS curl 등)을 실행하려 하면
Hermes가 슬랙에 승인 버튼(Allow Once / Allow Session / Always Allow / Deny)을
띄운다 — `~/.hermes/config.yaml`의 `approvals.mode: manual`(기본) 동작이다.

- **기본 방침: Deny.** AWS 조회는 ops-read 도구가 정답 경로다. raw 셸 조회는
  이 플러그인이 강제하는 read 경계(assume 롤 + 이름 붙은 쿼리)를 우회한다.
  Deny하면 에이전트는 도구로 되돌아간다.
- **Always Allow는 신중히** — 그 패턴이 `command_allowlist`에 영구 저장되어
  경계가 조용히 넓어진다.
- 무조건 차단할 패턴은 `approvals.deny`에 glob으로 추가한다 (yolo/off보다 우선):

```yaml
approvals:
  mode: manual
  deny:
    - "git push --force*"
```

### C. 가드레일 — 이런 요청은 거부되어야 정상

```
@봇 0.0.0.0/0 전체 열어줘 (dev-ec2-ssh)          # → 거부 (전 세계 오픈 금지)
@봇 10.0.0.0/8 열어줘                            # → 거부 (/24 미만 너무 넓음)
@봇 만료 없이 계속 prod DB 열어줘 (prod-db-access)    # → 거부 (prod는 expires_at 필수 — dev는 만료 생략 허용)
@봇 dev 데이터 볼륨 500GB로 키워줘          # → 거부 (1..100 캡)
```

봇이 요청을 거부하며 사유를 답한다. 설령 통과하더라도 CI `guard`가 2차로 잡는다.

### D. 마무리 멘트 (강조 포인트)

- 승인·머지는 **사람**(dev는 guard가 대신), apply는 **GitHub Actions(OIDC)**. 봇은 PR을 **열기만** 한다.
- 에이전트의 write는 **PR뿐**이다. 클라우드 apply·직접 SSH는 도구 자체가 없다 —
  "없는 도구는 인젝션으로도 못 부른다." `.tf` 수정은 dev 브랜치 한정 코드 PR로만
  가능하고(경로 allowlist가 `.github/`·prod를 거부), prod 코드는 승격 PR에서
  사람이 승인한다. 비용 상한은 에이전트가 못 건드리는 CI 비용 백스톱이 지킨다.

---

## 도구 목록

**read (조회 전용, 16)**

| 도구 | 하는 일 | 자격증명 |
|---|---|---|
| `ops_get_service_health()` | 앱 플릿(Role=app 태그, Name=<prefix>-* 스코프) ALB/EC2 상태 요약 | AWS |
| `ops_explain_alert(alert_id)` | Grafana 알람 룰 상태 + 근거 쿼리 | Grafana |
| `ops_query_metrics(query_name, labels)` | named Prometheus 쿼리 (cpu/mem/disk) | Grafana |
| `ops_query_logs(service, pattern?)` | named Loki 쿼리 (기본 nginx 5xx) | Grafana |
| `ops_aws_get_service()` | 앱 플릿 리소스 describe (EC2·bastion public_ip·EBS·TG·ALB dns_name·RDS endpoint 포함) | AWS |
| `ops_aws_get_alb_target_health(service)` | ALB 타깃 상태 (healthy/unhealthy) | AWS |
| `ops_aws_get_cost_summary(period)` | Cost Explorer 요약 | AWS |
| `ops_aws_find_unused_candidates()` | 미사용 후보 (keep 태그 제외, 삭제 안 함) | AWS |
| `ops_github_get_pr_status(pr_url)` | 리포 PR 상태 | GitHub App |
| `ops_github_get_workflow_run(run_url)` | GHA 런 상태 | GitHub App |
| `ops_pagerduty_list_incidents(status?, since_hours?)` | PD incident 목록 (ack/resolve 없음) | PagerDuty |
| `ops_pagerduty_get_oncall()` | 현재 온콜 (정책/레벨/사람) | PagerDuty |
| `ops_cloudflare_list_dns_records(name_contains?, type?)` | zone DNS 레코드 | Cloudflare |
| `ops_cloudflare_list_waf_rules()` | zone WAF 커스텀 룰 | Cloudflare |
| `ops_cloudflare_get_analytics(period_hours?)` | 엣지 HTTP analytics — 상태 클래스별 카운트 + 5xx 비율 (토큰에 Zone Analytics:Read 필요) | Cloudflare |
| `ops_github_read_file(path, ref?)` | IaC 리포 파일 읽기(dev/main) — 코드 PR 전 현재 내용 확인 | GitHub App |

**write**

| 도구 | toolset | 하는 일 | 자격증명 |
|---|---|---|---|
| `ops_github_open_tfvars_pr(surface, op, ...)` | ops-github-write | 단일 tfvars 파일 변경 PR을 **봇 이름**으로 연다 (브랜치 `ops/agent-*`). auto/human은 CODEOWNERS가 결정 | GitHub App |
| `ops_github_open_code_pr(files, title, reason)` | ops-github-write | **dev 한정** IaC 코드 PR(base=dev, `2-1-dev/`·`modules/`·`ansible/`만 — 그 외 경로는 도구가 거부). surface 밖 기능 추가용 | GitHub App |
| `ops_run_ansible_playbook(playbook, environment, ...)` | ops-ansible-write | **PR 없이** 카탈로그 playbook을 실행 — 단 직접 SSH가 아니라 IaC repo의 `ansible-ops.yml`을 workflow_dispatch로 트리거(실행은 VPC 안 self-hosted 러너, `--limit env_dev\|env_prod`, ref는 브랜치=환경: dev→dev, prod→main) | GitHub App |
| `ops_grafana_silence(action, ...)` | ops-monitoring-write | 알람 통지 mute/unmute — exact-match만, 만료 필수(max 24h), comment 감사 | Grafana |
| `ops_pagerduty_manage_incident(incident_id, action)` | ops-monitoring-write | 기존 incident ack/snooze/resolve — 사람이 요청한 lifecycle만, 자기 페이지 incident 금지 | PagerDuty |
| `ops_pagerduty_page_oncall(summary, dedup_key, ...)` | ops-monitoring-write | **온콜 실페이지** (Events v2 trigger) — 알람은 Slack 단일 통지라 이 도구가 유일한 페이지 경로. 런북 서킷 브레이커 전용, dedup_key로 에피소드당 1 incident | PD routing key |

`ops_github_open_tfvars_pr`는 구조화된 인자로 파일 내용을 **직접 구성**한다(자유 텍스트 아님).
surface: `dev-service · dev-ec2-ssh · dev-db-access · dev-disk · dev-dns · dev-packages`
(+ 동일한 `prod-*` 6종 — packages는 브랜치=환경으로 같은 파일을 env별 브랜치에서 편집)
+ 존 전역 `waf` 하나(2-2-prod root 소유, prod 전용). prod surface는 전부 사람 승인(waf만 예외 — incident 차단이라 auto).
`service_enabled=false`는 스택 전체 destroy로 복구 불가지만, dev-service는 무소유라
guard 통과 시 auto-merge된다.
ansible playbook 카탈로그: `rolling-restart · disk-grow · security-patch`
(장애 주입용 playbook은 카탈로그에 없음).

---

## 감사 로그

모든 도구 호출을 `post_tool_call` 훅이 `state/audit.jsonl`에 append-only로 남긴다
(도구명·args 원문·소요시간·correlation id — 시크릿은 설계상 args로 전달되지 않고,
긴 문자열 값은 500자에서 잘린다). 실습 중
`tail -f ~/.hermes/plugins/ops/state/audit.jsonl`로 "에이전트 층 감사"를 실물로
관찰한다. 위치는 `OPS_STATE_DIR`로 바꿀 수 있다.

## 테스트 (모델·자격증명·boto3 불필요)

```bash
python3 -m pytest ~/.hermes/plugins/ops/tests -q
```

핸들러 계약(JSON 문자열·예외 던지지 않음·누락 파라미터→에러 JSON), Service allowlist,
쿼리 인젝션 가드, write 툴의 구조화 검증(CIDR/expiry/캡/enum), register 배선을
mock으로 검증한다. 이 pytest 자체가 Day 3 "모델 없이 handler 테스트" 수업 재료다.

## 커리큘럼 매핑

- Day 1 3교시 — 플러그인 구조(manifest/register/schema/handler/hook) 시연 실물
- Day 2 1교시 — 설치·연동 실습 (이후 모든 조회 담당)
- Day 2 2~3교시 — Slack 요청 → 봇 tfvars PR (write toolset)
- Day 2 4교시 — 상태 조회·알람·비용·미사용 후보
- Day 3 1교시 — triage의 read 부분 (알람 → explain → metrics/logs → 조치 안내)
