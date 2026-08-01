# ops-agent-plugin — Hermes 에이전트 실습 (설치 · 배선 · 진행 가이드)

제작: **Woojin Kim** ([GitHub](https://github.com/wo-o) ·
[LinkedIn](https://www.linkedin.com/in/wo-o/) ·
[YouTube @ai에이터](https://www.youtube.com/@ai%EC%97%90%EC%9D%B4%ED%84%B0)) —
[MIT License](LICENSE). 배포·수정 자유, 저작자 표기는 유지해야 한다.

AI 에이전트(Hermes)가 인프라를 안전하게 운영하게 만드는 실습에서 **에이전트
쪽 절반**을 담는 repo다. 나머지 절반인 terraform · ansible · 환경 세팅은
[`wo-o/ops-agent-iac`](https://github.com/wo-o/ops-agent-iac)에 있다.

이 repo가 담는 것:

- 리포 루트가 곧 플러그인이다(plugin.yaml) — 슬랙에서 자연어로 인프라를 **조회(read)** 하고, 변경을 **봇 이름
  PR로 제안(write)** 하게 해주는 Hermes 플러그인. 설치·배선·슬랙 시연 대본까지의
  자세한 절차는 [`PLUGIN.md`](PLUGIN.md)에 있다.
- 이 문서 — 에이전트 호스트 준비부터 슬랙 시연까지 처음부터 따라 하는 진행
  순서. 각 단계의 상세는 링크로 넘긴다.

핵심 규율은 인프라 repo와 동일하다: **에이전트의 write는 PR뿐** — tfvars
surface PR(dev·prod)과 dev 한정 코드 PR(2-1-dev/·modules/·ansible/,
2026-07-20 개방). apply는 CI(GitHub OIDC)에서만, read는 read 전용 IAM 롤을
assume한 세션으로만. 서버 상태 변경은 bounded ansible 카탈로그로만 실행한다.
prod의 IaC 코드는 여전히 에이전트 손 밖이다 — dev→main 승격 PR을 사람이
승인해야 반영되고, 비용 상한은 CI guard의 비용 백스톱이 2차로 강제한다.

---

## 전체 그림

```
[슬랙에서 자연어 요청]
        │
        ▼
[Hermes 호스트 EC2]  ── ops 플러그인 ──▶ read : <project>-hermes-readonly 롤로 조회
   (ops-agent-iac                        ├▶ write: GitHub App 토큰으로 tfvars PR
    의 2-0-setup)                         └▶ ansible: bounded 카탈로그를 SSH로 실행
                                              │
                                              ▼
                                     [ops-agent-iac 의 CI(OIDC)가 plan·apply]
```

- **호스트**: Hermes는 인프라 repo의 `2-0-setup`이 띄우는 EC2에서 돈다
  (`<project>-hermes` 1대 — 실습 전체 공용). 그 EC2는 수강생 IP에서만 SSH로
  접속하고(공유 키), read 전용 롤(`<project>-hermes-readonly`)을 assume할 수
  있는 권한만 갖는다.
- **read**: ops의 read 도구가 위 read 롤로 서비스 헬스·알람·메트릭·
  로그·비용을 조회한다.
- **write**: ops의 write 도구가 GitHub App(봇) 토큰으로 PR을 연다 — tfvars
  surface PR(dev·prod) + dev 한정 코드 PR. 실제 반영은 인프라 repo의 CI가 한다.
- **ansible**: 서버 상태 변경(rolling-restart, disk-grow 등)은 bounded ansible
  카탈로그를 통해서만 실행하며, gha-runner 또는 Mac에서 `--limit env_dev|env_prod`로
  SSH 접속한다.
- **monitoring write**: 알람 통지 상태 조작(Grafana silence · PagerDuty incident
  ack/snooze/resolve · 온콜 실페이지)은 별도 toolset(`ops-monitoring-write`)이다 —
  장애 대응 실습 단계에서만 켠다. 인프라 구성은 아무것도 바꾸지 않는다.

---

## 진행 순서

### (사전) 커리큘럼 순서와의 관계

전체 순서는 인프라 repo([`ops-agent-iac`](https://github.com/wo-o/ops-agent-iac))
README의 주차 구조를 따른다. 이 문서의 단계는 그 순서에 이렇게 얹힌다:

- **(1)~(2) 호스트 + Hermes 설치·슬랙 연결** — 인프라 repo의 `2-0-setup`
  부트스트랩이 `<project>-hermes` 호스트를 이미 띄워 놓은 상태에서 시작한다.
- **(3) ops 플러그인 배선** — read 배선은 `2-0-setup` apply(read 롤
  `<project>-hermes-readonly`)가 끝나 있어야 하고, write 배선은 GitHub App(봇)
  정체성이 필요하다.
- 리포 거버넌스(Repository Ruleset·Environments)는 `2-0-setup`의
  `branch_ruleset.sh` + `environments.sh`가 1회 세팅한다 — 플러그인 배선의
  전제가 아니라 인프라 repo 쪽 사전 작업이다.

### (1) 에이전트 호스트 EC2 띄우기

Hermes를 돌릴 EC2는 인프라 repo의 `2-0-setup`이 만든다(`<project>-hermes` 1대 —
실습 전체 공용). 이 호스트는 신뢰 IP에서만 SSH를 허용하고(공유 SG), 인스턴스
프로파일로 `<project>-hermes-readonly` 롤만 assume할 수 있다.

```bash
# ops-agent-iac 안에서 (SSH 키가 없으면: ssh-keygen -t ed25519 -f ~/.ssh/<project>)
cd 2-0-setup/1-foundation   # .tf root는 2-0-setup 바로 아래가 아니라 1-foundation
terraform init && terraform apply
# 접속 (hermes 호스트 public IP output 사용)
ssh -i ~/.ssh/<project> ubuntu@"$(terraform output -raw hermes_host_public_ip)"
```

상세: `ops-agent-iac/2-0-setup/README.md`.

### (2) 호스트에 Hermes 설치

SSH로 호스트에 접속한 상태에서 Hermes를 설치하고 슬랙에 연결한다(1주차 실습
범위). 설치가 끝나면 `~/.hermes/` 아래에 게이트웨이 설정과 `.env`가 자리잡는다.

```bash
# 1) 설치
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

# 2) 슬랙 연결 — 대화형 설정(권장): 안내를 따라 Slack 앱(Socket Mode) 생성
hermes gateway setup
# 또는 수동: 만들어 둔 Slack 앱의 토큰 2개를 .env에 직접 넣는다
cat >> ~/.hermes/.env <<'EOF'
SLACK_APP_TOKEN=xapp-...     # App-Level Token (Socket Mode, connections:write)
SLACK_BOT_TOKEN=xoxb-...     # Bot User OAuth Token
SLACK_HOME_CHANNEL=<채널ID>  # 에이전트 상주 채널 (선택)
EOF
chmod 600 ~/.hermes/.env

# 3) 게이트웨이를 systemd user 서비스로 상주시킨다
hermes gateway install && hermes gateway start
systemctl --user status hermes-gateway.service   # active 확인

# 4) 검증: 슬랙 채널에서 봇을 멘션해 응답 확인
```

#### 모델: 네이티브 openai-codex 고정 (base_url 오버라이드 금지)

`hermes auth add openai-codex --type oauth --no-browser`로 ChatGPT Codex OAuth
인증 후, `~/.hermes/config.yaml`의 `model` 블록을 아래로 고정한다. `model` 블록에
`base_url`이 있으면 네이티브 codex 대신 그쪽으로 라우팅되므로 **반드시 제거**한다
(`hermes model` 대화형 선택이 다시 넣을 수 있으니 설정 후 config.yaml을 직접 확인).

```bash
python3 - <<'PY'
import pathlib, yaml
p = pathlib.Path.home() / ".hermes" / "config.yaml"
d = yaml.safe_load(p.read_text()) or {}
m = d.setdefault("model", {})
m["provider"] = "openai-codex"
m["default"]  = "openai-codex/gpt-5.5"
m.pop("base_url", None)          # openrouter 등 오버라이드 제거 (필수)
p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
print("model =", d["model"])
PY
# 검증: 응답이 수 초 내(수 분이면 codex 미사용 = base_url 잔존 의심)
hermes -z "한 문장 한국어 인사만."
```

#### 응답 언어: 한국어 (SOUL.md)

에이전트 코어 시스템 프롬프트는 `~/.hermes/SOUL.md`다. 언어 지시는 파일 끝에
붙이면 앞의 영어 기본 프롬프트에 앵커링돼 영어로 새기 쉽다 — **최상단**에 둔다.

```bash
cat > ~/.hermes/SOUL.md <<'EOF'
# 최우선 규칙: 응답 언어는 한국어

반드시 한국어로만 답한다. Slack을 포함한 모든 채널에서, 사용자에게 보이는 모든
문장(설명·요약·상태 보고·알림·인시던트 대응 메시지)은 예외 없이 한국어로 쓴다.
영어로 답하지 않는다. 단, 기술 용어·코드 식별자·명령어·파일 경로·툴 이름·로그
원문·리소스 이름은 번역하지 말고 원문(영어) 그대로 둔다.

# 출력 형식: 마크다운 표 금지 (Slack이 렌더링 못 함)

어떤 응답에서도 `| 열 | 열 |` / `|---|` 형태의 마크다운 표를 쓰지 않는다. 표로
보여줄 데이터(인스턴스·알람·비용·PR·DNS 목록 등 전부)는 아래 두 형식만 쓴다:

1. 기본 — 항목마다 「이모지 + 이름 — 상태」 한 줄, 그 아래 들여쓴 세부 한 줄:
   🟢 *ops-agent-iac-hermes* — running
       t3.small · i-xxxxxxxxxxxxxxxxx · x.x.x.x
   (i-xxx·x.x.x.x는 형식 자리표시 — 응답에는 항상 실제 인스턴스 ID·IP를 채운다)
   상태 이모지: 🟢 정상/running · 🟡 주의/진행 중 · 🔴 실패/장애 · ⚫ 종료/비활성
2. 컬럼을 나란히 비교해야 할 때만 — 코드블록(백틱 3개) 안에서 공백으로 컬럼 정렬.
   탭 문자 금지(클라이언트마다 폭이 달라 깨짐), 헤더는 영문(한글은 고정폭 2칸이라
   정렬이 깨짐).

마크다운 헤더(#)도 Slack에서 렌더링되지 않는다 — 섹션 구분은 굵은 한 줄로 한다.
굵게는 *별표 하나*(Slack 문법), 목록은 `-` 불릿을 쓴다.

# 응답 어휘: 내부 용어를 사용자에게 노출하지 않는다

surface, entry_key, op 같은 내부 구현 용어·도구 인자명은 사용자에게 보이는
문장에 쓰지 않는다 — "설정 항목"(에이전트에 허용된 변경 항목), "설정 변경 PR"처럼
풀어쓴다. 예: "prod-disk surface는 grow-only라 거부" 대신 "prod 디스크 설정은
확대만 허용이라 축소 요청은 거부됩니다". PR URL·파일 경로·명령어·값·리소스
이름처럼 사용자가 직접 확인할 대상은 원문 그대로 쓴다.

# 스킬 라우팅: 운영 요청은 ops 플러그인 스킬부터 읽는다

- 인프라 변경 요청(세팅·개방·확대·삭제 등) → `ops:ops-change`
- 알람·장애에서 시작하는 대응 → 현재 도구 목록에 `ops_pagerduty_manage_incident`
  또는 `ops_pagerduty_page_oncall`이 있으면 `ops:ops-incident-response`(진단+조치),
  둘 다 없으면 `ops:ops-incident-rca`(진단·보고 전용 — 어떤 변경도 만들지 않고
  원인·근거·권고만 스레드에 남긴다)
- read-only 조회·질문 → `ops:ops-operating`

이 리포의 운영 판단에 registry의 generic 스킬(devops/* 등)을 쓰지 않는다 —
surface 정책을 모르는 스킬은 잘못된 경로(직접 mutation 등)로 이끈다. 플러그인
스킬은 일반 `skills_list`에 표시되지 않으므로 목록에 bare name이 없다는 이유로
미등록 판정하지 않는다. 반드시 먼저 qualified name(`ops:ops-operating` 등)으로
`skill_view`를 호출한다. qualified name 로드까지 실패한 경우에만 런타임 복사본의
bare name을 시도하고, 둘 다 실패했을 때만 "ops 스킬 미등록" 상태를 보고한다.

# 채널 메시지 처리: 멘션은 필수가 아니다

채널 메시지는 멘션 없이도 정상 요청이다. 멘션 부재를 이유로 작업을 거절하거나
멘션을 요구하지 않는다 — 요청 내용만으로 판단한다.

# 실습 완결성: 실값으로 답하고, 데모 자격증명은 물어보면 알려준다

- 최종 안내의 명령·값(IP·인스턴스 ID·endpoint·도메인)은 실값으로 완성한다.
  `<bastion_public_ip>` 같은 placeholder를 남기지 않는다 — 도구 응답에 값이 없으면
  스킬이 정한 read-only fallback으로 확보한 뒤 답하고, 그래도 못 얻었을 때만 그
  사실을 명시한다. "강의 자료에서 확인하라"로 값 확보를 사용자에게 미루지 않는다.
- 이 환경은 수강생 본인 계정의 교육 실습이다. 실습용 데모 자격증명(dev `readonly`
  계정의 playbook 데모 비밀번호 등)은 사용자가 물으면 실값을 그대로 알려준다.
  단 SSH private key·master(dbadmin) 비밀번호·API 토큰·Slack 토큰은 여전히
  게시하지 않는다.

# 변경 경로 핵심 (상세 절차·보고 형식은 ops-change 스킬)

- "서비스/환경 세팅·삭제"는 `<env>-service` surface의 tfvars PR이다
  (service_enabled true/false). 환경에 리소스가 하나도 없어도 이 surface다.
- 기존 surface 어느 것으로도 불가능한 신규 인프라(없는 기능 추가)는 환경으로
  갈린다. dev: ops_github_read_file로 현재 코드를 먼저 읽고
  ops_github_open_code_pr로 dev 브랜치에 코드 PR을 연다(2-1-dev/·modules/·
  ansible/ 한정 — 절차는 ops-change 스킬의 "dev 코드 PR" 절). prod: 코드 PR을
  열지 않는다 — dev에서 만들어 검증한 뒤 dev→main 승격 PR을 사람이 승인해야
  반영된다고 안내한다. 비용 상한(t3.micro/small)은 validation을 고쳐도 CI
  비용 백스톱이 막는다 — 완화 시도 대신 상한을 사용자에게 보고한다.
- 대상 환경(dev/prod)이 불명확하면 PR 전에 되묻는다. dev의 서비스 삭제는
  auto-merge로 destroy까지 바로 가므로 특히 PR 전에 삭제 의사를 확인받는다.

---

You are Hermes Agent, an intelligent AI assistant created by Nous Research. You are
helpful, knowledgeable, and direct. Be targeted and efficient in your investigations.

이 리포에서 너는 인프라 운영 에이전트다. 읽기는 ops read 도구로, 변경은 PR
(ops-github-write: tfvars surface PR + dev 한정 코드 PR)로만 하고, 서버 상태
변경은 bounded ansible 카탈로그로만 실행한다. raw 셸(aws CLI 등)로 조회하지
말고 ops 도구를 우선한다.

# 다시 한 번: 모든 응답은 한국어로 작성한다.
EOF
systemctl --user restart hermes-gateway.service   # (root 설치면 systemctl restart)
```

### (3) ops 플러그인 설치 + 배선

이 repo를 호스트의 플러그인 경로에 `ops` 이름으로 클론하고, read/write
자격을 배선한다. 명령 요약만 아래에 두고, 각 값의 의미와 검증은
[`PLUGIN.md`](PLUGIN.md)에 자세히 있다.

```bash
# 1) 설치 — 리포 루트가 곧 플러그인이라 클론 한 번이면 끝 (업데이트는 git pull)
mkdir -p ~/.hermes/plugins && cd ~/.hermes/plugins \
  && git clone https://github.com/wo-o/ops-agent-plugin.git ops
# (또는 한 번에: hermes plugins install wo-o/ops-agent-plugin — 업데이트는 git pull / hermes plugins update ops)

# 2) read 배선 — ~/.hermes/.env 에 조회용 롤을 지정
#    OPS_AWS_READ_ROLE = <project>-hermes-readonly 롤의 ARN
#    (호스트 인스턴스 프로파일이 이 롤을 assume한다)

# 3) write 배선 — 봇(GitHub App) 정체성 3개 값(App ID / 설치 ID / 개인키)을 .env 에
#    App 생성 절차는 ops-agent-iac/2-0-setup/2-github/README.md 참고

# 4) 켜기 + 재시작 (toolset 이름은 __init__.py 등록명 기준: read 1 + write 3)
hermes plugins enable ops
# enable 중 "replace built-in tools" 권한 질문 → no. 이 플러그인은 built-in 교체가
# 없다(ops_* 도구 등록 + post_tool_call 감사 훅뿐). 이미 yes로 답했다면:
# hermes config set plugins.entries.ops.allow_tool_override false
hermes tools enable ops-read              # 조회 전부 (read-only)
hermes tools enable ops-github-write      # tfvars PR + dev 코드 PR
hermes tools enable ops-ansible-write     # bounded playbook (GHA dispatch) — 요청 기반 실습(RDS 임시 유저·디스크 확장·패치)에 필요
hermes tools enable ops-monitoring-write  # Grafana silence + PD incident ack/snooze/resolve — 장애 대응 단계에서만
hermes gateway restart

# 알람 대응 모드 토글 — 설명 전용 vs 실조치. 알람 라우팅이 이 토글을 따른다:
# ops-monitoring-write가 꺼져 있으면 에이전트는 ops:ops-incident-rca(진단·보고 전용,
# 어떤 변경도 안 만듦), 켜져 있으면 ops:ops-incident-response(진단+조치)를 읽는다.
# ansible-write는 끄지 않는다 — 요청 기반 실습이 계속 이 toolset을 쓴다.
hermes tools disable ops-monitoring-write && hermes gateway restart  # 설명 전용
hermes tools enable  ops-monitoring-write && hermes gateway restart  # 실조치

# 5) 확인 — 의존성은 pytest뿐(플러그인은 stdlib-only, boto3도 lazy import + 테스트는 mock)
#    미설치면: sudo apt-get install -y python3-pytest
python3 -m pytest ~/.hermes/plugins/ops/tests -q
```

#### `~/.hermes/.env` placeholder (복붙용)

`chmod 600` 유지, **기존 줄은 건드리지 말고 append**. 시크릿이 없는 도구는 에러가
아니라 아예 노출되지 않는다(fail-closed) — 안 쓰는 줄은 통째로 생략하면 된다.

```bash
# --- read: AWS (필수 — 없으면 AWS 조회 도구 전체 미노출) ---
OPS_AWS_READ_ROLE=arn:aws:iam::<계정ID>:role/<project>-hermes-readonly
OPS_AWS_REGION=ap-northeast-2            # 기본값이라 생략 가능
OPS_PROJECT_PREFIX=<project>              # IaC repo PROJECT_NAME과 동일
AWS_PROFILE=<SSO 프로파일>                    # 인스턴스 롤이면 생략

# --- read(+silence write): Grafana (선택 — IaC repo의 2-0-setup 공유 모니터링 서버가 발급) ---
OPS_GRAFANA_URL=<terraform output ops_grafana_url>          # http://<monitoring private IP>:3000 (쿼리용, 같은 VPC pull)
OPS_GRAFANA_PUBLIC_URL=<terraform output ops_grafana_public_url> # http://<monitoring public IP>:3000 — 응답에 인용되는
                                                            # dashboard 링크 base(브라우저 접속용). 미설정 시 private URL 폴백
OPS_GRAFANA_TOKEN=<terraform output -raw ops_grafana_token> # Editor SA 토큰 — 조회 + 알람 silence(Viewer는 silence 403)
OPS_GRAFANA_PROM_UID=prometheus          # 기본값이라 생략 가능
OPS_GRAFANA_LOKI_UID=loki                # 기본값이라 생략 가능

# --- read: Cloudflare (dns/waf surface, 선택 — 두 값 함께 설정) ---
# 토큰 권한: Zone.Zone Read + Zone.DNS Read + Zone.WAF Read + Zone Analytics:Read —
# Zone Read가 없으면 관리 zone 이름(zone_name) 조회가 안 돼 DNS 요청의 zone 치환
# 사전확인이 약해지고, Analytics:Read가 없으면 ops_cloudflare_get_analytics(엣지 5xx
# 점검 · cf-5xx 정기 cron)가 401.
OPS_CLOUDFLARE_READ_TOKEN=<Zone.Zone Read + Zone.DNS Read + Zone.WAF Read + Zone Analytics:Read 토큰>
OPS_CLOUDFLARE_ZONE_ID=<Cloudflare zone id>

# --- read+write: PagerDuty (선택 — ops-monitoring-write의 incident ack/snooze/resolve 게이트) ---
OPS_PAGERDUTY_TOKEN=<full-access API key>   # 온콜 조회 + incident write. read-only 키는 write 403
OPS_PAGERDUTY_FROM_EMAIL=<PD 로그인 이메일> # incident write REST의 필수 From 헤더 — 없으면 write 도구 미노출
OPS_PAGERDUTY_ROUTING_KEY=<Events v2 key>   # 온콜 실페이지(ops_pagerduty_page_oncall) 게이트 — 런북 서킷
                                            # 브레이커 전용. 3-1-pagerduty output(routing_key). 알람은 Slack
                                            # 단일 통지라 이 도구가 사람 폰을 울리는 유일한 경로 — 없으면 미노출

# --- read+write: GitHub ---
OPS_GITHUB_REPO=<owner>/ops-agent-iac
# GITHUB_TOKEN=<read-only PAT>               # App 미배선 시 조회 전용 폴백 (선택)

# --- write: GitHub App (봇 PR — 2-0-setup/2-github/README.md 절차의 출력값) ---
OPS_GITHUB_APP_ID=<app_id>
OPS_GITHUB_INSTALLATION_ID=<installation_id>
OPS_GITHUB_PRIVATE_KEY_PATH=/절대/경로/.secrets/app.pem
# 또는 파일 대신 인라인: OPS_GITHUB_PRIVATE_KEY=<PEM 내용>

# --- 승인 요청 멘션 (prod 승인 시나리오에 필수) ---
# Slack <@USERID> 또는 <!subteam^ID> 포맷이어야 실제 알림이 간다 — 일반 텍스트
# `@인프라팀`은 아무에게도 알림이 가지 않는다. ops-change 스킬이 prod 삭제/사전
# 승인 요청 메시지에 이 값을 그대로 쓴다. IaC repo의 repository variable
# INFRA_SLACK_MENTION(tf-destroy 알림용)과 같은 값으로 맞춘다.
OPS_INFRA_SLACK_MENTION=<@U0XXXXXXX>

# --- 기타 (선택) ---
# OPS_STATE_DIR=<감사 로그 디렉토리>       # 기본: 플러그인 디렉토리 아래 state/
```

각 값의 의미·발급 절차·검증 방법은 [`PLUGIN.md`](PLUGIN.md)의
2~3단계에 있다.

#### 스킬 업데이트와 self-patch 드리프트

runbook 스킬(ops-operating / ops-change / ops-incident-response / ops-incident-rca)은
두 위치에 있다:
플러그인 checkout(`~/.hermes/plugins/ops/skills/`)과 Hermes가 실제로 읽는 런타임
복사본(`~/.hermes/skills/devops/`). 리포가 정본이며, 업데이트는 **두 위치를 모두**
갱신해야 반영된다:

```bash
cd ~/.hermes/plugins/ops && git pull
cp -r skills/ops-change/. ~/.hermes/skills/devops/ops-change/
cp -r skills/ops-operating/. ~/.hermes/skills/devops/ops-operating/
cp -r skills/ops-incident-response/. ~/.hermes/skills/devops/ops-incident-response/
cp -r skills/ops-incident-rca/. ~/.hermes/skills/devops/ops-incident-rca/
hermes gateway restart
```

Hermes self-improvement가 런타임 복사본을 스스로 수정할 수 있다(응답 끝의
`Self-improvement review` 메시지가 흔적). 이 수정은 리포 관점에서 드리프트다 —
`diff -rq ~/.hermes/plugins/ops/skills ~/.hermes/skills/devops`로 주기적으로 확인하고,
유효한 개선은 리포에 커밋해 흡수하고 리포 정책과 충돌하는 patch는 위 동기화로
덮어쓴다. 런타임 복사본에만 남긴 수정은 호스트 재구축 시 유실된다.

#### 정기 cron (정기 점검) — 재구축 시 재등록

미사용 리소스·비용·Cloudflare 5xx 정기 점검 cron 3종은 Hermes 내장 스케줄러
(`~/.hermes/cron/jobs.json`) 상태로만 존재해 호스트 재구축 시 유실된다(스킬·SOUL.md와
동류). 정본은 [`data/cron-jobs.yaml`](data/cron-jobs.yaml)이다 — 각 job의 스케줄·대상
채널(`#alert-ops-agent`)·`--skill ops-operating`·호출 도구·프롬프트가 거기 있다.
재구축 후 그 파일 값으로 Hermes CLI로 3종을 다시 등록한다:

```bash
~/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main cron list   # 현재 등록분 확인
# 없으면 cron-jobs.yaml의 각 job을 cron create로 등록 (플래그는 `cron create --help`)
```

스케줄 타임존은 매니페스트가 UTC 기준(호스트 TZ=UTC, `date`로 확인). in-agent
`cronjob` 도구는 인시던트 후속 one-shot용이라 이 3종과 별개다. 라이브 Hermes가
프롬프트를 self-improve로 다듬으면 그 diff를 매니페스트로 흡수해 정본을 유지한다.

cf-5xx cron이 쓰는 `ops_cloudflare_get_analytics`는 `OPS_CLOUDFLARE_READ_TOKEN`에
**Zone Analytics:Read** 스코프를 요구한다(위 .env 절의 DNS/WAF Read만으로는 401).

봇 정체성(GitHub App)이 필요한 이유: 에이전트가 여는 PR·머지가 **내 이름이 아니라
봇 이름**으로 남아야 감사가 깔끔하고, 설치 토큰이 해당 repo로만 범위가 잡히기
때문이다. 생성 절차는 인프라 repo의 `2-0-setup/2-github/README.md`를 참고.

### (4) 슬랙에서 실습 진행

슬랙에서 자연어로 요청하면 에이전트가 read 도구로 조회하거나, write 도구로
tfvars PR을 열거나, bounded ansible 카탈로그를 실행한다. 실습별 요청
문구(프롬프트) 전체 목록과 예상 동작은 [`PLUGIN.md`](PLUGIN.md)의 슬랙 시연 대본에
있다. 에이전트가 하는 일은 스킬 4종으로 나뉜다:

- **ops-operating** (read-only 조회): "지금 dev 앱 상태 어때?",
  "최근 알람 하나 설명해줘", "이번 달 비용 요약해줘"
- **ops-change** (사용자 요청 기반 surface 변경 PR): 각 시나리오는 `dev-*` /
  `prod-*` 키로 나뉜다.
  - ec2-ssh (EC2 SSH 접근), db-access (RDS를 bastion SSH grant로 — prod는
    expires_at 필수, dev는 선택), disk (볼륨 라이브 확대 + ansible growpart, grow-only),
    dns (Cloudflare→ALB — 요청 도메인이 관리 zone 밖이면 호스트 라벨을 관리 zone에
    치환해 등록하고 치환 사실을 보고·PR에 명시),
    waf (특정 IP 차단/챌린지 — block/challenge 커스텀 룰. prod 전용 surface, 차단은 존 전역),
    service (스택 세팅/삭제 `service_enabled` — false는 전체 destroy로 복구 불가.
    dev는 auto-merge, prod는 사람 승인),
    packages (security-patch가 설치할 추가 패키지 목록 — dev/prod가 같은 파일을
    브랜치=환경으로 나눠 편집)
  - 예: "dev 디스크를 20GB로 키워줘"(→ tfvars PR + ansible growpart),
    "prod에서 이 IP 차단해줘"(→ waf surface PR)
- **ops-incident-response** (알람 기반 자율 대응): Grafana 알람 발화 시
  metric/log로 진단하고 자동 조치까지 이어간다(2-3-incident-response 실습).
- **ops-incident-rca** (알람 진단·보고 전용): `ops-monitoring-write`가 꺼진
  설명 전용 모드에서 알람을 진단하고 원인·근거·권고만 스레드에 남긴다 —
  어떤 변경도 만들지 않는다.

에이전트가 여는 것은 항상 **PR**이고, 반영은 사람이 PR을 머지한 뒤 CI가
apply할 때 이뤄진다. dev surface(`env.auto.tfvars` 제외)는 무소유라 자동 머지되고,
prod 전부·`.tf`는 CODEOWNERS에 따라 사람 승인이 필요하다.

---

## 자격증명 경계 (요약)

- **read**는 `<project>-hermes-readonly` 롤(ReadOnlyAccess + Cost Explorer read)을
  assume한 세션으로만 이뤄진다 — 경계가 repo 관습이 아니라 IAM에서 성립한다.
- **write**는 GitHub App 설치 토큰의 PR 경로(tfvars surface PR + dev 한정 코드 PR),
  bounded ansible 카탈로그 실행(workflow_dispatch), 알람 통지 상태 조작
  (Grafana silence · PD incident lifecycle · 온콜 페이지)까지다. 클라우드를 직접
  변경하거나 apply하는 도구는 애초에 모델에 노출되지 않는다 — 반영은 전부 IaC
  repo의 CI가 한다. prod의 IaC 코드는 dev→main 승격 PR을 사람이 승인해야만 닿는다.
- 과정 최종 철거용 tf-destroy 워크플로는 Slack 인프라팀 멘션(repo variable
  `INFRA_SLACK_MENTION`) + GitHub Environment(destroy-approval) 승인
  게이트를 거친다. 평시 서비스 삭제(`service_enabled=false` surface PR)도 머지 후
  apply가 같은 destroy-approval 승인을 대기하며(tf-apply의 approve-destroy 잡),
  승인 요청 멘션은 에이전트가 `OPS_INFRA_SLACK_MENTION` 값으로 보낸다(ops-change 스킬).
- 배선되지 않은 자격증명이 필요한 도구는 비활성으로 뜬다(fail-closed).

시크릿은 절대 repo에 커밋하지 않는다 — 전부 호스트의 `~/.hermes/.env`(로컬)로만
주입한다.

---

## 관련 repo

| repo | 담는 것 |
|---|---|
| [`wo-o/ops-agent-plugin`](https://github.com/wo-o/ops-agent-plugin) (이 repo) | Hermes 에이전트: ops 플러그인 + 설치·진행 가이드 |
| [`wo-o/ops-agent-iac`](https://github.com/wo-o/ops-agent-iac) | terraform · ansible · 환경 세팅 · 실습 인프라 |
