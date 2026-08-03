"""ansible toolset — 두 번째 write 경로: 범위가 제한된 remediation을 GHA로 실행한다.

설계 배경: tfvars PR(tools_change)이 인프라 값을 바꾸는 경로라면, 서버 런타임 상태를
되돌리는 조치(2-3 incident-response: "메모리 알람 -> 롤링 재시작", "볼륨 확대 뒤 파일
시스템 확장")는 ansible 실행 경로가 담당한다.

tfvars PR과 달리 이 경로는 사람 리뷰를 기다리지 않고 즉시 실행되지만, ansible을 이
호스트에서 직접 SSH로 돌리지는 않는다 — IaC 리포의 `ansible-ops.yml` GitHub Actions
workflow를 workflow_dispatch로 트리거한다. 실제 ansible-playbook은 VPC 안의 self-hosted
러너(label: ansible, 2-0-setup/1-foundation/runner.tf)에서 돈다. 이유:

  · 실행 주체 일치 — 러너는 VPC 내부라 플릿에 SSH로 붙는다(공유 SG 안). 이 플러그인
    호스트는 VPC 밖이라 직접 SSH가 SG에 막힌다.
  · 자격증명 — DB_PASSWORD 등 secret은 GHA가 주입한다(로컬엔 없다).
  · 감사 — 실행 로그·신원이 GHA run에 남고 ops_github_get_workflow_run으로 조회된다.
  · write 위임 원칙 — tfvars 경로와 동일하게 "이 호스트는 클라우드를 직접 바꾸지 않고
    GitHub를 거친다"를 지킨다. dispatch는 PR/머지를 거치지 않아 즉시성은 유지된다.

안전은 이렇게 잡는다:
  · 등록 봉쇄 — 에이전트는 workflow의 playbook input에 아무 값이나 못 준다. 빌트인
    조치 카탈로그(_PLAYBOOKS)의 키이거나, dev 매니페스트(ansible/playbooks.yml)에
    등록된 이름이어야 한다. "파일만 있으면 실행"이 아니라 "등록된 것만 실행"이다.
    에이전트가 dev 코드 PR로 새 조치 playbook을 추가할 수 있으나(파일+등록 함께),
    그것은 dev 전용이고 prod는 dev→main 승격 PR(사람 승인)로만 도달한다. 장애 주입용
    playbook은 등록하지 않는다 — 조치용만.
  · 환경 스코프 — environment(dev|prod)를 workflow에 넘기고, workflow가 --limit
    env_<env>로 그 환경 플릿에만 실행한다. playbook 자체도 serial:1 + 첫 실패 중단.
  · 인자 봉쇄 — params로 넘길 수 있는 변수는 카탈로그가 선언한 키·enum만.
  · dry_run — dry_run=true면 workflow에 dry_run을 넘겨 --check로 실행(아무것도 안 바꿈).

핸들러는 dispatch를 트리거하고 방금 생성된 run의 URL을 돌려준다. 성공/실패 확정은
에이전트가 followup 지시대로 ops_github_get_workflow_run으로 폴링해서 판정한다
(tfvars PR 경로가 apply run을 폴링하는 것과 동일 패턴). 감사는 post_tool_call 훅이 남긴다.
"""

from __future__ import annotations

import base64
import re
import secrets
import string
import time
from typing import Any

try:  # yaml은 프레임워크 의존성이라 런타임에 존재한다(settings.py와 동일 패턴)
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from ._compat import fail, ok
from . import settings
from .clients import github_app, http_request

# IaC 리포의 ansible workflow 파일명(.github/workflows/ 기준). workflow_dispatch로
# environment/playbook/dry_run input을 받아 self-hosted 러너에서 실행한다.
_WORKFLOW_FILE = "ansible-ops.yml"

# dev 조치용 플레이북 등록부. 에이전트가 코드 PR로 ansible/에 추가한 플레이북은 여기
# 등록돼야 dispatch가 허용된다("파일만 있으면 실행"이 아니라 "등록된 것만 실행").
# 빌트인 7종은 _PLAYBOOKS가 관리하므로 여기 없다. 등록 플레이북은 dev 전용.
_MANIFEST_PATH = "ansible/playbooks.yml"
# 등록 이름 문자셋 — workflow가 ansible/<name>.yml 로 실행하므로 경로 조작을 막는다.
_PB_NAME_RE = re.compile(r"[a-z][a-z0-9-]{2,48}")


def _env_ref(env: str) -> str:
    """dispatch ref = 브랜치=환경 매핑(2026-07-20 dev IaC 개방으로 ansible에도 적용).

    dev 브랜치는 에이전트가 ansible/ 플레이북·패키지 목록을 수정할 수 있으므로,
    dev 환경 실행은 dev 브랜치를 체크아웃해야 그 변경이 반영된다. prod는 main
    (승격 머지로만 도달). ref=main 고정이던 시절에는 dev 수정이 dispatch에 보이지
    않는 갭이 있었다."""
    return "dev" if env == "dev" else "main"


def _fetch_registered_playbooks(repo: str) -> dict[str, str]:
    """dev 브랜치 ansible/playbooks.yml 매니페스트의 등록 플레이북(name -> desc).

    에이전트가 dev 코드 PR로 추가한 조치용 플레이북을 dispatch 허용 목록으로 읽는다.
    dev 전용이므로 항상 dev ref에서 읽는다. 부재·파싱 실패·yaml 미탑재 시 빈 dict —
    등록 안 된 이름은 unknown으로 거부된다(workflow가 최종 게이트). 이름 문자셋을
    검증해 경로 조작(../, 절대경로)을 걸러낸다."""
    if yaml is None:
        return {}
    try:
        r = http_request(
            "GET",
            f"{github_app._API}/repos/{repo}/contents/{_MANIFEST_PATH}",
            headers=github_app._headers(),
            params={"ref": "dev"},
        )
        if r.status_code >= 400:
            return {}
        payload = r.json() or {}
        raw = base64.b64decode(payload["content"]).decode("utf-8", "replace")
        data = yaml.safe_load(raw) or {}
    except Exception:
        return {}
    out: dict[str, str] = {}
    for entry in data.get("playbooks") or []:
        name = (entry or {}).get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and _PB_NAME_RE.fullmatch(name):
            out[name] = str(entry.get("desc") or "")
    return out


# 조치용 playbook 카탈로그. 키 = workflow의 playbook input enum과 정확히 일치해야 한다.
# 값은 설명 + 허용 params(-e 변수) 스펙(현재는 없음). 장애 주입·광범위 playbook은 등록 안 함.
_PLAYBOOKS: dict[str, dict[str, Any]] = {
    "rolling-restart": {
        "desc": "ALB TG에서 드레인 → 앱 서비스 재시작 → /healthz 확인 → TG 복귀 (메모리 릭·행 완화)",
        "params": {},
    },
    "disk-grow": {
        "desc": "볼륨 확대(tfvars) 뒤 파일시스템 확장 (디스크 임계 조치)",
        "params": {},
    },
    "security-patch": {
        "desc": "serial 롤링 보안 패치(+필요 시 재부팅, +추가 패키지 설정)",
        "params": {},
    },
    # 인스턴스 타입 롤링 변경. terraform 경로(ec2_instance_type tfvars)는 두 대를
    # 병렬 in-place stop/start해 동시 순단 — 이 playbook은 TG 드레인 → stop →
    # modify → start → healthy 복귀를 serial:1로 수행한다. instance_type은 비용 상한
    # allowlist(variables.tf와 동일)로 enum 봉쇄. 실행 성공 후 같은 값으로
    # ec2_instance_type tfvars PR을 머지해 상태를 수렴시켜야 한다 — 실제 타입이
    # 이미 새 값이라 plan은 no-op, 생략하면 다음 apply가 두 대를 동시에 되돌린다.
    "instance-resize": {
        "desc": "인스턴스 타입 롤링 변경(무중단) — TG 드레인 → stop → 타입 변경 → start → healthy 복귀 (serial 1). 완료 후 같은 값으로 ec2_instance_type tfvars PR 머지 필수(상태 수렴)",
        "params": {
            "instance_type": {"enum": ["t3.micro", "t3.small"]},
        },
    },
    # up==0 no-data(미배포) 케이스의 조치. node_exporter·promtail은 user_data에 없어
    # 프로비저닝·app_version bump(blue-green 교체)마다 사라진다. incident-response
    # runbook(STEP 0 no-data 판정)이 이 키를 지시하는데 카탈로그에 없어 에이전트가
    # 에스컬레이션만 가능했던 것을 배선(2026-07-19 slack-rca, C3-I1 후속 누락).
    "monitoring-agents": {
        "desc": "node_exporter + promtail (재)설치 — 새로 뜬/교체된 fleet의 scrape down(up==0 no-data) 조치",
        "params": {},
    },
    # 2-03 prod: 만료 있는 임시 DB role 발급/DROP(bastion 경유 psql). workflow가
    # 이미 같은 input을 지원한다 — 카탈로그 부재로 시나리오 3 prod가 막혔던 것을
    # 배선(2026-07-16 e2e C1-I6, 사용자 결정). 값은 전부 pattern/enum으로 봉쇄 —
    # temp_user·valid_until은 psql SQL로 보간되므로 자유 문자열 금지.
    # temp_password는 params가 아니다 — state=present dispatch 시 핸들러가 생성해
    # workflow input으로 넘기고 응답에 담는다(2026-07-20 slack-rca: 러너 생성+마스킹
    # 방식은 요청자 전달 경로가 없어 자격증명 dead end).
    "rds-temp-user": {
        "desc": "만료(VALID UNTIL) 있는 임시 RDS role 생성/삭제 — bastion 경유, 만료 시 postgres가 로그인 거부, role DROP은 state=absent 재실행",
        "params": {
            "temp_user": {"pattern": r"^[a-z][a-z0-9_]{2,30}$"},
            "valid_until": {"pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"},
            "grant_mode": {"enum": ["readonly", "readwrite"]},
            "state": {"enum": ["present", "absent"]},
        },
    },
    # dev 상시 readonly 계정 생성(멱등, bastion 경유 psql). 구 bastion user_data
    # 부트스트랩 대체(2026-08-01 사용자 결정) — dev 서비스 세팅 apply 성공 후
    # 이 playbook을 dispatch한다. 비밀번호는 강의 자료 데모 고정값(러너 쪽 기본값)
    # 이라 params·자격증명 반환이 없다. prod는 상시 계정 금지(rds-temp-user만) —
    # envs로 dev에 한정하고 workflow도 dev 외를 거부한다.
    "rds-readonly-user": {
        "desc": "dev 상시 readonly RDS 계정 생성(멱등) — dev 서비스 세팅 후 실행, 비밀번호는 강의 자료 데모 값, dev 전용",
        "params": {},
        "envs": ("dev",),
    },
}

_ENVS = ("dev", "prod")

# dispatch 직후 새 run이 인덱싱될 때까지의 짧은 폴링 상한. run URL을 바로 돌려주려는
# 목적일 뿐 — 실행 완료를 기다리지 않는다(그건 에이전트가 별도로 폴링한다).
_RUN_LOOKUP_TRIES = 5
_RUN_LOOKUP_INTERVAL_S = 2.0


def check_ansible_requirements() -> bool:
    """visibility gate: ansible 조치는 GitHub App으로 workflow_dispatch를 트리거하므로
    change 경로와 동일한 자격증명(App id/key/installation + repo)이 있어야 노출한다.
    App installation은 actions:write 권한이 있어야 dispatch가 통한다."""
    return github_app.check_change() and bool(settings.github_repo())


def _repo() -> str:
    return settings.github_repo() or ""


def _list_runs(repo: str, ref: str, event: str) -> list[dict]:
    """이 workflow의 특정 이벤트 run 목록(최신순, 해당 ref). 실패 시 빈 리스트."""
    r = http_request(
        "GET",
        f"{github_app._API}/repos/{repo}/actions/workflows/{_WORKFLOW_FILE}/runs",
        headers=github_app._headers(),
        params={"event": event, "branch": ref, "per_page": 10},
    )
    if r.status_code >= 400:
        return []
    return r.json().get("workflow_runs") or []


def _list_dispatch_runs(repo: str, ref: str) -> list[dict]:
    """이 workflow의 workflow_dispatch run 목록(최신순, 해당 ref). 실패 시 빈 리스트."""
    return _list_runs(repo, ref, "workflow_dispatch")


def _latest_run_id(repo: str, ref: str) -> int:
    """현재 workflow_dispatch run 중 가장 큰 id(없으면 0). dispatch 전후 비교용."""
    return max((int(x["id"]) for x in _list_dispatch_runs(repo, ref)), default=0)


def _find_new_run(repo: str, after_id: int, ref: str) -> dict | None:
    """dispatch로 새로 생긴 run(id > after_id) 하나를 짧게 폴링해서 찾는다.
    GitHub이 run을 인덱싱하는 데 몇 초 걸릴 수 있어 여러 번 시도한다."""
    for _ in range(_RUN_LOOKUP_TRIES):
        newer = [x for x in _list_dispatch_runs(repo, ref) if int(x["id"]) > after_id]
        if newer:
            return max(newer, key=lambda x: int(x["id"]))
        time.sleep(_RUN_LOOKUP_INTERVAL_S)
    return None


def run_ansible_playbook(args: dict, **kwargs: Any) -> str:
    """카탈로그 remediation playbook 하나를 한 환경(dev|prod) 플릿에 실행한다 —
    IaC 리포의 ansible-ops workflow를 workflow_dispatch로 트리거해서.

    args:
      playbook     _PLAYBOOKS 키 (예: "rolling-restart", "disk-grow")
      environment  "dev" | "prod" — workflow가 --limit env_<environment>로 스코프
      params       카탈로그가 허용한 -e 변수 (선택; enum 검증)
      dry_run      true면 workflow에 dry_run=true를 넘겨 --check (아무것도 바꾸지 않음)
      reason       조치 맥락(로깅·보고용 자유 텍스트)
    """
    try:
        if not check_ansible_requirements():
            return fail(
                "ansible tools unavailable",
                "Set OPS_GITHUB_APP_ID / _PRIVATE_KEY_PATH / _INSTALLATION_ID and OPS_GITHUB_REPO "
                "(the IaC repo carrying .github/workflows/ansible-ops.yml). The GitHub App "
                "installation needs actions:write to dispatch the workflow.",
            )
        key = args.get("playbook")
        spec = _PLAYBOOKS.get(key)
        if not spec:
            # 빌트인이 아니면 dev 매니페스트 등록분에서 찾는다(에이전트가 코드 PR로
            # 추가한 조치용 플레이북). 등록분은 dev 전용 + params 없음(주입 방지).
            registered = _fetch_registered_playbooks(_repo())
            if key in registered:
                spec = {
                    "desc": registered[key] or f"dev 등록 플레이북 {key}",
                    "params": {},
                    "envs": ("dev",),
                }
            else:
                extra = (
                    f"; dev 등록: {', '.join(sorted(registered))}" if registered else ""
                )
                return fail(
                    f"unknown playbook '{key}'",
                    f"builtin: {', '.join(sorted(_PLAYBOOKS))}{extra}. "
                    "새 플레이북은 dev 코드 PR로 ansible/<name>.yml + ansible/playbooks.yml "
                    "등록을 함께 추가한 뒤 dispatch",
                )

        env = args.get("environment")
        if env not in _ENVS:
            return fail(
                f"environment must be one of {list(_ENVS)} (got {env!r})",
                "scope the action to a single environment",
            )
        # 카탈로그가 환경을 한정한 playbook(예: rds-readonly-user는 dev 전용 —
        # prod 상시 계정 금지 계약).
        allowed_envs = spec.get("envs", _ENVS)
        if env not in allowed_envs:
            return fail(
                f"playbook '{key}' is restricted to {list(allowed_envs)} (got {env!r})",
                "prod standing accounts are not allowed — use rds-temp-user for prod DB access",
            )

        # --- params(-e 변수) 검증(카탈로그가 허용한 키·enum만) --------------
        params = args.get("params") or {}
        if not isinstance(params, dict):
            return fail("params must be an object", "")
        allowed = spec.get("params", {})
        for pk, pv in params.items():
            pspec = allowed.get(pk)
            if pspec is None:
                return fail(
                    f"param '{pk}' not allowed for playbook '{key}'",
                    f"allowed: {', '.join(allowed) or '(none)'}",
                )
            if "enum" in pspec and pv not in pspec["enum"]:
                return fail(
                    f"param '{pk}' must be one of {pspec['enum']} (got {pv!r})",
                    "",
                )
            # pattern 파라미터는 SQL/셸로 보간되는 값(temp_user 등)의 인젝션 봉쇄다.
            if "pattern" in pspec and not re.fullmatch(pspec["pattern"], str(pv)):
                return fail(
                    f"param '{pk}' must match {pspec['pattern']} (got {pv!r})",
                    "",
                )

        dry_run = bool(args.get("dry_run"))
        repo = _repo()

        # workflow_dispatch inputs. workflow의 input은 문자열이므로 bool은 소문자화한다.
        inputs: dict[str, str] = {
            "environment": env,
            "playbook": key,
            "dry_run": "true" if dry_run else "false",
        }
        for pk, pv in params.items():
            inputs[pk] = str(pv)

        # rds-temp-user(state=present): 비밀번호는 여기서 생성해 dispatch input으로
        # 넘기고 응답에도 담는다 — 시나리오 정본이 "자격증명은 응답으로만"인데, 러너
        # 생성+add-mask 방식은 요청자에게 돌려줄 경로가 없다(2026-07-20 slack-rca).
        # dispatch input은 run event payload에 남지만 본인 계정 + VALID UNTIL 시한부
        # role이라 위협모델상 수용. 영숫자만 써서 SQL/셸 보간과 -e 문자열에 안전.
        temp_password = None
        if key == "rds-temp-user" and params.get("state", "present") == "present":
            alphabet = string.ascii_letters + string.digits
            temp_password = "".join(secrets.choice(alphabet) for _ in range(24))
            inputs["temp_password"] = temp_password

        # dispatch는 run id를 돌려주지 않는다 → 트리거 전 최신 run id를 기록해두고
        # 트리거 후 새 run을 찾는다. ref는 브랜치=환경(dev→dev, prod→main) —
        # dev에서 에이전트가 수정한 플레이북·패키지 목록이 실행에 반영되게 한다.
        ref = _env_ref(env)

        # dedup: 같은 playbook@env의 실행이 이미 queued/in_progress면 새로 dispatch하지
        # 않고 그 run을 돌려준다 — 알람이 반복 발화하는 동안 세션마다 재dispatch해 큐가
        # 쌓이는 것을 막는다(2026-07-20 GH 장애 중 11건 적체 실측). 워크플로 run-name이
        # "ansible-ops: <playbook> @ <env>"라서 제목으로 식별한다. rds-temp-user는 호출마다
        # 자격증명이 달라 재사용 불가, dry_run 요청도 실실행과 섞지 않는다.
        if key != "rds-temp-user" and not dry_run:
            dedup_titles = {f"ansible-ops: {key} @ {env}"}
            candidates = list(_list_dispatch_runs(repo, ref))
            # disk-grow는 tfvars 머지가 자동으로 growpart를 돌린다: dev는 봇 auto-merge가
            # push를 억제해 auto-merge.yml이 "disk-grow @ <env>"로 dispatch(제목 일치 →
            # 기존 dedup이 잡음), prod는 admin-merge가 진짜 push라 push 트리거 run
            # "auto-disk-grow @ push"가 뜬다. 후자는 event=push라 _list_dispatch_runs에
            # 안 잡혀, 에이전트의 disk-grow dispatch가 중복으로 나가 이미 커진 볼륨에
            # growpart no-op(changed=0)을 돌렸다(2026-07-20 C2-I2). push run도 같은
            # 작업이므로 재사용한다 — 에이전트가 실제로 확대한 run을 보고하게 된다.
            if key == "disk-grow":
                dedup_titles.add("ansible-ops: auto-disk-grow @ push")
                candidates += _list_runs(repo, ref, "push")
            for prior in candidates:
                if prior.get("display_title") in dedup_titles and prior.get(
                    "status"
                ) in ("queued", "in_progress"):
                    return ok(
                        dispatched=False,
                        reused_run=True,
                        run_url=prior.get("html_url"),
                        run_id=prior.get("id"),
                        head_sha=prior.get("head_sha"),
                        ref=ref,
                        note=(
                            f"an identical {key} run for {env} is already "
                            f"{prior.get('status')} — reusing it instead of dispatching a "
                            "duplicate. Poll this run to completion, then verify with a read tool."
                        ),
                    )

        before_id = _latest_run_id(repo, ref)

        r = http_request(
            "POST",
            f"{github_app._API}/repos/{repo}/actions/workflows/{_WORKFLOW_FILE}/dispatches",
            headers=github_app._headers(),
            body={"ref": ref, "inputs": inputs},
        )
        if r.status_code == 404:
            return fail(
                f"workflow '{_WORKFLOW_FILE}' not found on {repo}@{ref}",
                "the IaC repo must carry .github/workflows/ansible-ops.yml on that branch, "
                "and the App installation must be able to see it",
            )
        if r.status_code >= 400:
            return fail(
                f"workflow_dispatch failed -> {r.status_code}",
                "the App installation needs actions:write; verify environment/playbook match the "
                "workflow's inputs",
                status=r.status_code,
                body=r.text[:400],
            )

        run = _find_new_run(repo, before_id, ref)
        actions_url = f"https://github.com/{repo}/actions/workflows/{_WORKFLOW_FILE}"
        followup = (
            "This dispatched the ansible-ops workflow — ansible runs on the in-VPC self-hosted "
            "runner, NOT here, and (unless dry_run) it applies to LIVE hosts. It is NOT done yet: "
            "poll ops_github_get_workflow_run(run_url) every ~15s until conclusion is 'success'. "
            "If the run status is 'waiting', it is paused for environment approval (deployment "
            "protection rule) — tell the requester a human must approve it in the Actions UI "
            "('Review deployments' on the run page), include the run URL, and keep polling; "
            "do NOT treat the wait as a failure or re-dispatch. "
            "THEN verify with a read tool before reporting: for a restart, poll "
            "ops_aws_get_alb_target_health until targets are back in-service and re-check the "
            "alert with ops_explain_alert; for disk-grow, verify the actual filesystem size "
            "(ops_query_metrics disk usage / the run's df output) — success is the volume sitting "
            "at the target size, NOT the ansible 'changed' count. A disk-grow with changed=0 is "
            "STILL success: the volume was already grown (often by the merge's auto-disk-grow @ "
            "push run) — report it as done, do not re-dispatch or call it a no-op failure. "
            "If the run concludes 'failure', report it with the run URL — "
            "do NOT claim the alert is resolved. If this action follows a config PR merge "
            "(security-patch after an extra_packages change, disk-grow after a volume resize), "
            "confirm the run's head_sha matches the merge commit before trusting it — a run that "
            "started before the merge landed picked up stale config/inventory; dispatch again and "
            "poll the run whose head_sha equals the merge commit."
        )
        # temp_password가 있으면 응답에 담는다 — 이 값이 요청자에게 전달되는 유일한
        # 경로다(Actions 로그는 마스킹됨). 에이전트는 이를 최종 접속 안내에 포함한다.
        extra_out = {"temp_password": temp_password} if temp_password else {}
        if run is None:
            # dispatch는 수락됐지만 run이 아직 인덱싱 전 — Actions 페이지를 준다.
            return ok(
                playbook=key,
                environment=env,
                dry_run=dry_run,
                summary=spec["desc"],
                dispatched=True,
                run_url=None,
                actions_url=actions_url,
                note="dispatch accepted; the run is not yet visible — open actions_url to find it",
                followup=followup,
                **extra_out,
            )
        return ok(
            playbook=key,
            environment=env,
            dry_run=dry_run,
            summary=spec["desc"],
            dispatched=True,
            run_url=run["html_url"],
            run_id=run["id"],
            head_sha=run.get("head_sha"),
            ref=ref,
            followup=followup,
            **extra_out,
        )
    except github_app.GitHubAppError as e:
        return fail(str(e), "check the App installation + permissions (actions:write)")
    except Exception as e:  # 핸들러 밖으로 절대 raise하지 않는다
        return fail(f"run_ansible_playbook failed: {e}", "")
