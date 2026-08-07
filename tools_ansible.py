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
  · 등록 봉쇄 — 에이전트는 workflow의 playbook input에 아무 값이나 못 준다. 실행
    allowlist는 환경 정본 브랜치의 등록부(ansible/playbooks.yml) 하나다 — 강사 제공
    조치와 에이전트 추가 조치 구분 없이 전부(2026-08-07 통합, 구 빌트인 카탈로그
    _PLAYBOOKS 대체). dev 실행=dev 등록분, prod 실행=main 등록분. "파일만 있으면
    실행"이 아니라 "등록된 것만 실행"이다. 에이전트가 dev 코드 PR로 새 조치
    playbook을 추가할 수 있고(파일+등록 함께), prod 실행 허용은 dev→main 승격 PR
    (사람 승인 = main 등록)로만 도달한다. 장애 주입용 playbook은 등록하지 않는다 —
    조치용만.
  · 인자 봉쇄 — params로 넘길 수 있는 변수는 스펙(ansible/specs/<name>.yml)이 선언한
    키·타입·범위만. 스펙은 dev 브랜치에서도 사람 소유(CODEOWNERS)라 파라미터 조치의
    입력 계약(비용 한도 choices, SQL 보간 값 pattern)은 사람이 승인한 데이터다. 검증은
    여기(빠른 실패)와 workflow의 generic 엔진(사람 소유 .github/, 최종 게이트)이
    이중으로 한다. 스펙 없는 플레이북 = 파라미터 불허.
  · 환경 스코프 — environment(dev|prod)를 workflow에 넘기고, workflow가 --limit
    env_<env>로 그 환경 플릿에만 실행한다. playbook 자체도 serial:1 + 첫 실패 중단.
    환경 한정(예: rds-readonly-user dev 전용)은 스펙 envs 필드가 선언한다.
  · dry_run — dry_run=true면 workflow에 dry_run을 넘겨 --check로 실행(아무것도 안 바꿈).

핸들러는 dispatch를 트리거하고 방금 생성된 run의 URL을 돌려준다. 성공/실패 확정은
에이전트가 followup 지시대로 ops_github_get_workflow_run으로 폴링해서 판정한다
(tfvars PR 경로가 apply run을 폴링하는 것과 동일 패턴). 감사는 post_tool_call 훅이 남긴다.
"""

from __future__ import annotations

import base64
import json
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

# 조치용 플레이북 등록부 — 실행 allowlist의 단일 정본. 여기 등록된 이름만 dispatch가
# 허용된다("파일만 있으면 실행"이 아니라 "등록된 것만 실행" — 강사 제공·에이전트 추가
# 구분 없음, 2026-08-07 통합). allowlist는 환경 정본 브랜치의 사본이다(dev 실행=dev
# 등록분, prod 실행=main 등록분 — 승격 PR 승인 = prod 허용).
_MANIFEST_PATH = "ansible/playbooks.yml"
# 플레이북별 파라미터 스펙 디렉토리 — dev 브랜치에서도 사람 소유(CODEOWNERS).
_SPEC_DIR = "ansible/specs"
# 등록 이름 문자셋 — workflow가 ansible/<name>.yml 로 실행하므로 경로 조작을 막는다.
_PB_NAME_RE = re.compile(r"[a-z][a-z0-9-]{2,48}")
# 파라미터 이름 문자셋(ansible 변수명) — 스펙 항목의 형식 오류를 걸러낸다.
_PARAM_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,40}")


def _env_ref(env: str) -> str:
    """dispatch ref = 브랜치=환경 매핑(2026-07-20 dev IaC 개방으로 ansible에도 적용).

    dev 브랜치는 에이전트가 ansible/ 플레이북·패키지 목록을 수정할 수 있으므로,
    dev 환경 실행은 dev 브랜치를 체크아웃해야 그 변경이 반영된다. prod는 main
    (승격 머지로만 도달). ref=main 고정이던 시절에는 dev 수정이 dispatch에 보이지
    않는 갭이 있었다."""
    return "dev" if env == "dev" else "main"


def _fetch_repo_yaml(repo: str, ref: str, path: str) -> Any:
    """ref 브랜치의 YAML 파일을 contents API로 읽어 파싱한다.

    부재·조회 실패·파싱 실패·yaml 미탑재 시 None — 호출부가 빈 값으로 처리한다."""
    if yaml is None:
        return None
    try:
        r = http_request(
            "GET",
            f"{github_app._API}/repos/{repo}/contents/{path}",
            headers=github_app._headers(),
            params={"ref": ref},
        )
        if r.status_code >= 400:
            return None
        payload = r.json() or {}
        raw = base64.b64decode(payload["content"]).decode("utf-8", "replace")
        return yaml.safe_load(raw)
    except Exception:
        return None


def _fetch_registered_playbooks(repo: str, ref: str) -> dict[str, str]:
    """ref 브랜치 ansible/playbooks.yml 등록부의 플레이북(name -> desc).

    실행 allowlist의 단일 정본 — 환경 정본 브랜치의 사본이므로 _env_ref(env)를
    ref로 받는다(dev 실행=dev 등록분, prod 실행=main 등록분 — 승격 PR 승인 =
    prod 허용). 조회 실패 시 빈 dict — 모든 이름이 unknown으로 거부된다
    (workflow가 최종 게이트, dispatch 자체도 같은 API를 쓰므로 API 장애면 어차피
    실행 불가). 이름 문자셋을 검증해 경로 조작(../, 절대경로)을 걸러낸다."""
    data = _fetch_repo_yaml(repo, ref, _MANIFEST_PATH)
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for entry in data.get("playbooks") or []:
        name = (entry or {}).get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and _PB_NAME_RE.fullmatch(name):
            out[name] = str(entry.get("desc") or "")
    return out


def _fetch_spec(repo: str, ref: str, name: str) -> dict:
    """ref 브랜치 ansible/specs/<name>.yml 파라미터 스펙(부재 시 {} = 파라미터 불허).

    스펙은 dev 브랜치에서도 사람 소유(CODEOWNERS)라 에이전트가 못 고친다 —
    파라미터 조치의 입력 계약(키·타입·범위, 비용 한도 choices, SQL 보간 값
    pattern)은 사람이 승인한 데이터다. 여기서는 envs(환경 한정)·params 선언·
    generated(플러그인이 값 생성) 필드를 소비한다. derived(db_host 등 러너 조회
    값)는 workflow 엔진 몫이라 무시한다."""
    data = _fetch_repo_yaml(repo, ref, f"{_SPEC_DIR}/{name}.yml")
    return data if isinstance(data, dict) else {}


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


def _branch_head_sha(repo: str, ref: str) -> str | None:
    """현재 브랜치 tip commit sha(조회 실패 시 None). disk-grow 중복 억제에서
    CI가 이 머지에 대해 이미 돌린 자동 run인지 head_sha로 상관하는 데 쓴다."""
    r = http_request(
        "GET",
        f"{github_app._API}/repos/{repo}/git/refs/heads/{ref}",
        headers=github_app._headers(),
    )
    if r.status_code >= 400:
        return None
    return (r.json().get("object") or {}).get("sha")


def run_ansible_playbook(args: dict, **kwargs: Any) -> str:
    """카탈로그 remediation playbook 하나를 한 환경(dev|prod) 플릿에 실행한다 —
    IaC 리포의 ansible-ops workflow를 workflow_dispatch로 트리거해서.

    args:
      playbook     등록부(ansible/playbooks.yml, 환경 정본 브랜치) 이름
      environment  "dev" | "prod" — workflow가 --limit env_<environment>로 스코프
      params       스펙(ansible/specs/<name>.yml)이 선언한 -e 변수 (선택; 타입·범위 검증)
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
        env = args.get("environment")
        if env not in _ENVS:
            return fail(
                f"environment must be one of {list(_ENVS)} (got {env!r})",
                "scope the action to a single environment",
            )
        repo = _repo()
        ref = _env_ref(env)
        # 실행 allowlist는 환경 정본 브랜치의 등록부 하나다(2026-08-07 통합 — 구
        # 빌트인 카탈로그 대체). dev 실행=dev 등록분, prod 실행=main 등록분,
        # 승격 PR 승인 = prod 허용.
        registered = _fetch_registered_playbooks(repo, ref)
        if key not in registered:
            listing = ", ".join(sorted(registered)) or "(조회 실패 또는 비어 있음)"
            return fail(
                f"unknown playbook '{key}' (env={env})",
                f"{ref} 등록부(ansible/playbooks.yml): {listing}. 새 플레이북은 dev 코드 "
                "PR로 ansible/<name>.yml + 등록부 항목을 함께 추가한 뒤 dispatch. "
                "파라미터가 필요하면 ansible/specs/<name>.yml 스펙도 추가해야 하며 "
                "specs/는 dev에서도 사람 소유라 그 PR은 사람 승인이 필요하다. "
                "prod 실행은 dev→main 승격 PR을 사람이 승인(=main 등록)해야 허용된다",
            )

        # 파라미터 스펙(사람 소유 데이터) — envs 한정과 params 선언을 여기서 소비한다.
        spec = _fetch_spec(repo, ref, key)
        allowed_envs = tuple(spec.get("envs") or _ENVS)
        if env not in allowed_envs:
            return fail(
                f"playbook '{key}' is restricted to {list(allowed_envs)} (got {env!r})",
                "스펙(ansible/specs)의 envs가 환경을 한정한다 — 예: rds-readonly-user는 "
                "dev 전용(prod 상시 계정은 rds-temp-user만)",
            )

        # --- params(-e 변수) 검증(스펙이 선언한 키·타입·범위만) --------------
        params = args.get("params") or {}
        if not isinstance(params, dict):
            return fail("params must be an object", "")
        decls: dict[str, dict] = {}
        for p in spec.get("params") or []:
            if isinstance(p, dict) and _PARAM_NAME_RE.fullmatch(
                str(p.get("name") or "")
            ):
                decls[p["name"]] = p
        for pk, pv in params.items():
            d = decls.get(pk)
            if d is None:
                return fail(
                    f"param '{pk}' not allowed for playbook '{key}'",
                    f"allowed: {', '.join(decls) or '(none)'} — "
                    f"스펙(ansible/specs/{key}.yml)이 선언한 키만 받는다",
                )
            ptype = d.get("type") or "string"
            if ptype == "choice":
                if pv not in (d.get("choices") or []):
                    return fail(
                        f"param '{pk}' must be one of {d.get('choices')} (got {pv!r})",
                        "",
                    )
            elif ptype == "int":
                if not isinstance(pv, int) or isinstance(pv, bool):
                    return fail(f"param '{pk}' must be an int (got {pv!r})", "")
                lo, hi = d.get("min"), d.get("max")
                if (lo is not None and pv < lo) or (hi is not None and pv > hi):
                    return fail(
                        f"param '{pk}' must be within {lo}..{hi} (got {pv})", ""
                    )
            else:
                if not isinstance(pv, str):
                    return fail(f"param '{pk}' must be a string (got {pv!r})", "")
                # pattern 파라미터는 SQL/셸로 보간되는 값(temp_user 등)의 인젝션 봉쇄다.
                pat = d.get("pattern")
                if pat and not re.fullmatch(pat, pv):
                    return fail(f"param '{pk}' must match {pat} (got {pv!r})", "")

        # generated: true 파라미터(자격증명)는 미지정 시 여기서 생성해 dispatch에
        # 넘기고 응답에도 담는다 — 요청자에게 전달되는 유일한 경로(Actions 로그는
        # 스펙 secret 마스킹, 러너 생성 방식은 전달 경로가 없다 — 2026-07-20
        # slack-rca). dispatch input은 run event payload에 남지만 본인 계정 +
        # 시한부 role이라 위협모델상 수용. 영숫자만 써서 SQL 보간·pattern에 안전.
        generated: dict[str, str] = {}
        alphabet = string.ascii_letters + string.digits
        for pk, d in decls.items():
            if d.get("generated") and pk not in params:
                value = "".join(secrets.choice(alphabet) for _ in range(24))
                params[pk] = value
                generated[pk] = value

        missing = [
            pk for pk, d in decls.items() if d.get("required") and pk not in params
        ]
        if missing:
            return fail(
                f"required param(s) missing for '{key}': {', '.join(missing)}",
                f"스펙(ansible/specs/{key}.yml)의 required 파라미터를 params로 전달한다",
            )

        dry_run = bool(args.get("dry_run"))

        # workflow_dispatch inputs. 파라미터는 params JSON input 하나로 넘긴다 —
        # workflow의 generic 엔진이 같은 스펙으로 재검증 후 -e @extra-vars.json 실행.
        inputs: dict[str, str] = {
            "environment": env,
            "playbook": key,
            "dry_run": "true" if dry_run else "false",
        }
        if params:
            inputs["params"] = json.dumps(
                params, separators=(",", ":"), ensure_ascii=False
            )

        # dispatch는 run id를 돌려주지 않는다 → 트리거 전 최신 run id를 기록해두고
        # 트리거 후 새 run을 찾는다. ref는 브랜치=환경(dev→dev, prod→main) —
        # allowlist·스펙 조회에 이미 쓴 값과 동일하다.

        # dedup: 같은 playbook@env의 실행이 이미 queued/in_progress면 새로 dispatch하지
        # 않고 그 run을 돌려준다 — 알람이 반복 발화하는 동안 세션마다 재dispatch해 큐가
        # 쌓이는 것을 막는다(2026-07-20 GH 장애 중 11건 적체 실측). 워크플로 run-name이
        # "ansible-ops: <playbook> @ <env>"라서 제목으로 식별한다. 파라미터 있는 호출은
        # 값이 달라 같은 작업이라 볼 수 없고(자격증명 생성 포함) 재사용하지 않는다.
        # dry_run 요청도 실실행과 섞지 않는다.
        if not params and not dry_run:
            dedup_titles = {f"ansible-ops: {key} @ {env}"}
            candidates = list(_list_dispatch_runs(repo, ref))
            head_sha = None
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
                # 자동 run(~2분)이 에이전트의 dispatch 시도보다 먼저 끝나면
                # queued/in_progress 창을 벗어나 아래 dedup을 통과, 에이전트가 이미
                # 커진 볼륨에 growpart no-op을 중복 dispatch했다(F3, 2026-08-03 C2:
                # 자동 run 22:59:12 완료 → 에이전트 dispatch 22:59:25, 13초 차이).
                # 현재 머지 head에 대해 이미 success로 끝난 자동 run도 재사용하도록
                # head_sha로 상관한다.
                head_sha = _branch_head_sha(repo, ref)
            for prior in candidates:
                if prior.get("display_title") not in dedup_titles:
                    continue
                status = prior.get("status")
                in_flight = status in ("queued", "in_progress")
                # disk-grow 한정: 현재 머지 head에 대해 이미 success로 끝난 자동 run은
                # 재실행이 no-op이므로 재사용한다(위 race). head_sha 미확보면 스킵.
                already_done = (
                    key == "disk-grow"
                    and status == "completed"
                    and prior.get("conclusion") == "success"
                    and head_sha is not None
                    and prior.get("head_sha") == head_sha
                )
                if in_flight or already_done:
                    reused_status = status if in_flight else "completed(success)"
                    note = (
                        f"an identical {key} run for {env} is already {reused_status} "
                        f"for this merge — reusing it instead of dispatching a duplicate. "
                    )
                    note += (
                        "Read its result and verify with a read tool."
                        if already_done
                        else "Poll this run to completion, then verify with a read tool."
                    )
                    return ok(
                        dispatched=False,
                        reused_run=True,
                        run_url=prior.get("html_url"),
                        run_id=prior.get("id"),
                        head_sha=prior.get("head_sha"),
                        ref=ref,
                        note=note,
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
        # generated 파라미터(자격증명)는 응답에 담는다 — 이 값이 요청자에게 전달되는
        # 유일한 경로다(Actions 로그는 스펙 secret 마스킹). 에이전트는 최종 접속
        # 안내에 포함한다. temp_password 키는 기존 소비처 호환용으로도 노출한다.
        extra_out: dict[str, Any] = {}
        if generated:
            extra_out["generated_params"] = generated
            if "temp_password" in generated:
                extra_out["temp_password"] = generated["temp_password"]
        if run is None:
            # dispatch는 수락됐지만 run이 아직 인덱싱 전 — Actions 페이지를 준다.
            return ok(
                playbook=key,
                environment=env,
                dry_run=dry_run,
                summary=registered[key] or f"등록 플레이북 {key}",
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
            summary=registered[key] or f"등록 플레이북 {key}",
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
