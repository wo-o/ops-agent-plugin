"""change toolset — write 경로는 PR뿐이다(봇 신원). apply는 언제나 CI.

설계(plugin-design §4 change): 핸들러는 클라우드에 절대 apply하지 않는다.
  - tfvars surface PR: 등록된 surface 파일 딱 하나를 STRUCTURED args로 수정.
  - dev 코드 PR(2026-07-20 개방): dev 브랜치의 modules/·2-1-dev/·ansible/ 한정으로
    IaC 코드 자체를 수정/추가한다(없는 기능 추가). dev CODEOWNERS가 이 경로들의
    소유를 해제해 guard 통과 시 auto-merge되고, prod 반영은 dev→main 승격 PR에서
    사람이 승인한다. 비용 상한은 CI guard의 비용 백스톱(.github/은 사람 소유)이
    2차로 강제한다.
CI `guard` job(plan + 비용 백스톱 + ansible syntax)이 권위 있는 두 번째 검사다.

도구:
  ops_github_open_tfvars_pr  — map entry를 set/remove하거나 scalar를 set한 뒤 PR을 연다
  ops_github_read_file       — 리포 파일 읽기(read-only) — 코드 PR 전 현재 내용 확인용
  ops_github_open_code_pr    — dev 한정 IaC 코드 PR(파일 전체 내용 교체/추가)
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import time
from typing import Any

from ._compat import fail, ok
from . import settings
from .clients import github_app, http_request

# 에이전트 write surface: dev/prod 두 환경 × A서비스 시나리오. 키="<env>-<type>".
# 각 spec의 "dir"이 대상 디렉토리(flat — <dir>/<file>). module variables.tf의
# validation과 동일 경계를 tool 레벨에서도 1차로 반영한다.
# 브랜치=환경 매핑: dev surface의 정본·PR base는 dev 브랜치, prod는 main이다
# (dev 브랜치 머지=2-1-dev apply, main 머지=2-2-prod apply).
_ENV_DIRS = {"dev": "2-1-dev", "prod": "2-2-prod"}
_ENV_BASES = {"dev": "dev", "prod": "main"}
_SURFACE_TYPES: dict[str, dict[str, Any]] = {
    # 2-01 서비스 프로비저닝/삭제 게이트 + 사이즈(service.auto.tfvars).
    # service_enabled=true로 스택을 통째로 올리고(1 세팅), false로 통째로 내린다
    # (9 삭제 — 복구 불가). dev는 다른 surface처럼 무소유=auto-merge, prod만 사람 승인.
    "service": {
        "file": "service.auto.tfvars",
        "var": "service_enabled",
        "kind": "hcl_service",
    },
    # 2-02 EC2 SSH 접근
    "ec2-ssh": {
        "file": "ec2-ssh.auto.tfvars.json",
        "var": "ec2_ssh_allowlist",
        "kind": "json_map",
    },
    # 2-03 RDS 접근(bastion SSH grant)
    "db-access": {
        "file": "db-access.auto.tfvars.json",
        "var": "db_grants",
        "kind": "json_map",
    },
    # 2-04 데이터 볼륨(라이브 확대 + ansible growpart, grow-only)
    "disk": {
        "file": "disk.auto.tfvars",
        "var": "data_volume_size_gb",
        "kind": "hcl_int",
        "min": 1,
        "max": 100,
        "grow_only": True,
    },
    # 2-05 DNS(ALB 연결)
    "dns": {"file": "dns.auto.tfvars.json", "var": "dns_records", "kind": "json_map"},
}
_SURFACES: dict[str, dict[str, Any]] = {
    f"{env}-{stype}": {**spec, "dir": d, "base": _ENV_BASES[env]}
    for env, d in _ENV_DIRS.items()
    for stype, spec in _SURFACE_TYPES.items()
}
# 2-08 보안 패치 추가 패키지 surface — env 디렉토리가 아니라 ansible/의 vars 파일
# 하나를 편집한다(env 무관: security-patch가 실행되는 플릿에 공통 적용). 정본은
# main — 러너가 main을 체크아웃해 security-patch.yml의 vars_files로 로드한다.
# (2026-07-16 e2e C1-I9: 파일·플레이북은 있었지만 surface 미배선이었다.)
# 2-06 WAF 커스텀 룰(특정 IP 차단/챌린지) — 존 전역 단일 surface, prod 전용
# (2026-07-20 결정: WAF 수정은 prod에서만 — 구 2-4-waf root를 2-2-prod로 통합).
# Cloudflare가 존당 엔트리포인트 룰셋을 1개만 허용해 env별 룰셋이 불가능하다
# (2026-07-16 e2e C1-I8). 차단 요청은 이 surface 하나로 모이고 차단 효과는 존
# 전역(dev 호스트 포함). 정본은 main, 무소유=auto-merge. 무료 존은 per-IP rate
# limit(advanced engine)은 못 만들지만 커스텀 방화벽 룰의 ip.src 차단은 된다.
_SURFACES["waf"] = {
    "file": "waf.auto.tfvars.json",
    "var": "waf_rules",
    "kind": "json_map",
    "dir": "2-2-prod",
    "base": "main",
}
# 2-08 packages surface — 브랜치=환경 매핑을 ansible에도 적용한다(2026-07-20 dev
# IaC 개방): dev 몫은 dev 브랜치(dev CODEOWNERS가 ansible/ 소유 해제 = auto),
# prod 몫은 main(이 파일만 main에서도 무소유 = auto). security-patch 실행은
# ansible-ops dispatch가 env에 맞는 ref(dev→dev, prod→main)를 체크아웃해 그
# 브랜치의 목록을 로드한다 — surface base와 실행 ref가 일치해야 한다.
for _env, _pkg_base in _ENV_BASES.items():
    _SURFACES[f"{_env}-packages"] = {
        "file": "ansible/patch-extra-packages.yml",
        "var": "extra_packages",
        "kind": "yaml_pkg_list",
        "dir": "",
        "base": _pkg_base,
    }
# 하위호환 별칭 — 2026-07-20 packages -> dev-/prod-packages split 전에 시작돼 재시작으로
# resume된 pending 세션이 옛 이름 "packages"를 부를 때 prod 몫(base=main)으로 받는다.
# 스키마 enum에는 없어(schemas.py) 신규 호출은 이 이름을 못 쓴다 — 의도된 fallback이며
# test_change.py / test_code_pr.py가 이 동작을 검증한다. (지우지 말 것 — 그 테스트가 깨진다.)
_SURFACES["packages"] = _SURFACES["prod-packages"]

_INSTANCE_TYPES = {"t3.micro", "t3.small"}
_DB_CLASSES = {"db.t3.micro", "db.t3.small"}


def check_change_requirements() -> bool:
    return github_app.check_change()


def _repo() -> str:
    return settings.github_repo() or ""


def _env_dir(key: str, spec: dict) -> str:
    return spec.get("dir", key)


def _usage(surface: str, spec: dict, args: dict, env_dir: str) -> str | None:
    """변경 반영 후 사용자에게 알려줄 '접근·사용·확인 방법'. surface 유형(var)별로
    결정적으로 구성한다(LLM이 명령을 지어내지 않게). env_dir로 dev/prod를 구분한다."""
    var = spec["var"]
    entry = args.get("entry") if isinstance(args.get("entry"), dict) else {}
    val = args.get("value")

    if var == "service_enabled":  # hcl_service surface
        v = val if isinstance(val, dict) else {}
        gate = (
            "guard 통과 시 자동 머지"
            if env_dir.startswith("2-1-dev")
            else "사람 승인 후 머지"
        )
        if v.get("service_enabled") is False:
            return (
                f"서비스 전체 내림({env_dir}) — service_enabled=false면 EC2/RDS/Bastion/ALB가 "
                f"destroy된다(복구 불가). {gate}, tf-apply가 삭제를 수행. 확인은 "
                f"`ops_aws_get_service({env_dir})`가 빈 상태인지."
            )
        return (
            f"서비스 프로비저닝({env_dir}) — service_enabled=true. {gate}·apply 후 "
            f"EC2/RDS/Bastion/ALB가 뜬다. 확인 `ops_aws_get_service({env_dir})`."
        )
    if var == "ec2_ssh_allowlist":
        return (
            f"EC2 SSH 접근 열림({entry.get('cidr')} → 22). 접속 "
            f"`ssh -i ~/.ssh/ops-agent-iac ubuntu@<app-ip>` — app-ip는 "
            f"`ops_aws_get_service({env_dir})`로 확인. expires_at을 넣었으면 access-expiry "
            f"cron이 자동 회수, 수동 회수는 같은 entry_key로 remove_entry."
        )
    if var == "db_grants":
        ep = f"엔드포인트는 {env_dir} terraform output db_endpoint"
        if env_dir.startswith("2-1-dev"):
            return (
                f"RDS 접근 열림 — bastion SSH grant({entry.get('cidr')} → 22, 만료 "
                f"{entry.get('expires_at') or '없음(상시)'}). bastion 접속 후 "
                f"`psql -h <db-endpoint> -U readonly -d appdb` — 모듈이 부팅 시 만든 상시 "
                f"readonly 계정(SELECT 전용, 비밀번호는 강의 자료). {ep}. 만료를 넣었으면 "
                f"access-expiry cron이 네트워크 grant를 회수."
            )
        return (
            f"RDS 접근 열림 — bastion SSH grant({entry.get('cidr')} → 22, 만료 "
            f"{entry.get('expires_at')}). 이건 네트워크 경로만 연다 — DB 계정은 "
            f"`ops_run_ansible_playbook(rds-temp-user, prod, {{temp_user, valid_until, grant_mode}})`로 "
            f"요청자별 임시 role을 발급받아 그 계정으로 접속(dbadmin 공유 금지). bastion 접속 후 "
            f"`psql -h <db-endpoint> -U <temp_user> -d appdb`, {ep}. 만료 시 access-expiry "
            f"cron이 네트워크 grant를 회수, 임시 role은 VALID UNTIL로 로그인 거부."
        )
    if var == "data_volume_size_gb":
        return (
            f"데이터 볼륨 {val}GB로 확장. terraform이 EBS를 라이브 확대 → ansible "
            f"disk-grow가 파일시스템 확장 → 서버에서 `df -h /data`로 확인. 축소 불가(grow-only)."
        )
    if var == "dns_records":
        return (
            "DNS 레코드 추가(ALB 연결). 확인 `dig <name>` 또는 "
            "`ops_cloudflare_list_dns_records`. 반영은 CLOUDFLARE_API_TOKEN이 있을 "
            "때만(없으면 plan까지, fail-closed)."
        )
    if var == "extra_packages":
        val_list = val if isinstance(val, list) else []
        env_name = "dev" if surface.startswith("dev-") else "prod"
        return (
            f"보안 패치 추가 패키지 목록 갱신({', '.join(sorted(set(val_list))) or '비움'}, "
            f"{env_name} 몫 — 브랜치=환경: dev는 dev 브랜치, prod는 main). "
            "머지 후 다음 security-patch 실행에서 함께 설치된다 — 즉시 설치하려면 "
            f"ops_run_ansible_playbook(security-patch, {env_name})을 이어서 실행"
            "(dispatch가 env에 맞는 브랜치를 체크아웃한다). "
            "설치 확인은 run 로그의 extra_pkgs 카운트."
        )
    if var == "waf_rules":
        scope = f" (path {entry.get('path')})" if entry.get("path") else ""
        return (
            f"WAF 커스텀 룰 추가(IP {entry.get('ip')} → {entry.get('action', 'block')}{scope}). "
            "존 전역 차단 — dev/prod 모두에 적용된다(존 공유, 룰셋은 2-2-prod root 소유 — 변경은 prod 설정으로만). "
            "확인 `ops_cloudflare_list_waf_rules`. 반영은 CLOUDFLARE_API_TOKEN 있을 때(fail-closed). "
            "무료 존은 rate limit(advanced engine)은 안 되지만 커스텀 룰의 ip.src 차단은 된다."
        )
    return None


# expires_at 형식(ISO8601 UTC, 예: 2026-07-15T00:00:00Z). variables.tf validation과
# 동일 기준. 형식이 깨지면 access-expiry cron이 회수하지 못해 영구 grant가 된다.
_EXPIRES_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# EC2 Security Group rule description은 AWS가 허용하는 ASCII 문자와 255자 이하여야
# 한다. Terraform apply에서 실패하기 전에 플러그인에서 차단한다.
_AWS_SG_DESCRIPTION_RE = re.compile(r"^[A-Za-z0-9 ._:/()#,@\[\]+=\-&;{}!$*]{0,255}$")

# WAF path 허용 문자 — Cloudflare 표현식에 `uri.path eq "<path>"`로 보간되므로
# 큰따옴표·공백·연산자를 막아 표현식 인젝션을 차단한다(variables.tf와 동일).
_WAF_PATH_RE = re.compile(r"^/[A-Za-z0-9/_.~-]*$")


def _validate_waf_ip(ip: str) -> str | None:
    """WAF 커스텀 룰 대상 IP 검증 — 단일 IP 또는 CIDR 허용(rate limit이 아니라 방화벽
    매칭이라 /24 이상 제약은 두지 않는다). 단 /0(전 세계)은 사이트 전체 차단이라 거부."""
    if not ip:
        return "waf rule requires 'ip' (a source IP or CIDR to block)"
    try:
        if "/" in ip:
            net = ipaddress.ip_network(ip, strict=False)
            if net.prefixlen == 0:
                return "refusing /0 (0.0.0.0/0 or ::/0) — would block the whole site"
        else:
            ipaddress.ip_address(ip)
    except ValueError as e:
        return f"invalid ip/CIDR: {ip} ({e})"
    return None


def _validate_cidr(cidr: str) -> str | None:
    try:
        # strict=True: IaC repo variables.tf의 validation(plan → guard 게이트)과 동일
        # 기준. 여기서 통과시키면 guard에서 반드시 죽는 PR이 만들어지므로 일찍 거부한다.
        net = ipaddress.ip_network(cidr, strict=True)
    except ValueError as e:
        return f"invalid CIDR: {cidr} ({e})"
    # 이 실습의 allowlist/grant는 IPv4 전용이다. IPv6 광대역(2400::/24 등)이 /24 하한
    # 검사를 우회해 넓게 열리는 것을 막는다.
    if net.version != 4:
        return f"only IPv4 CIDRs are supported here, got {cidr}"
    if net.prefixlen == 0:
        return "refusing 0.0.0.0/0 (open to the world)"
    if net.prefixlen < 24:
        return f"IPv4 CIDR prefix must be between /24 and /32, got /{net.prefixlen}"
    return None


def _fetch_current(path: str, ref: str = "main") -> dict | None:
    """현재 surface를 파싱한 결과(JSON)를 반환하거나, 파일이 없으면 None을 반환한다.

    ref는 surface의 base 브랜치(브랜치=환경 매핑: dev surface=dev, prod=main) —
    반대 브랜치에서 읽으면 stale 값 기준으로 diff를 만들게 된다."""
    import base64

    # github_app 모듈에는 httpx client 컨텍스트매니저가 없다. open_multi_file_pr과
    # 동일하게 공유 http_request 헬퍼 + _headers()(installation token)로 읽는다.
    r = http_request(
        "GET",
        f"{github_app._API}/repos/{_repo()}/contents/{path}?ref={ref}",
        headers=github_app._headers(),
    )
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise github_app.GitHubAppError(f"read {path} -> {r.status_code}")
    raw = base64.b64decode(r.json()["content"]).decode()
    return json.loads(raw) if path.endswith(".json") else {"_raw": raw}


def _noop_ok(
    surface: str, spec: dict, args: dict, env_dir: str, path: str, summary: str
) -> str:
    """base 브랜치가 이미 원하는 내용과 동일하면 PR을 열지 않고 no-op으로 보고한다.

    배경: 같은 변경이 다른 PR(사람 hotfix 등)로 먼저 머지되면 봇 PR은 빈 diff가
    되고, auto-merge/tf-apply의 paths 필터에 아예 안 걸린다 — '머지됐는데 apply가
    없는' 혼란(2026-07-16 dev-ec2-ssh PR #85)의 원인. 빈 diff PR은 열지 않는다."""
    base = spec.get("base", "main")
    usage = _usage(surface, spec, args, env_dir)
    followup = (
        f"NO PR was opened: branch '{base}' already contains exactly this change "
        "(someone else landed it first). Do NOT open, merge, or close any PR and do "
        "NOT report the 5-line PR timeline. Instead VERIFY the change is actually "
        "LIVE with a read tool (ops_aws_get_service / dig / list tools) — the branch "
        "can be ahead of the cloud if the last tf-apply failed. If live state "
        "matches, report '이미 반영됨' with that evidence. If it does NOT match, "
        "check the latest tf-apply run (ops_github_get_workflow_run): a failed apply "
        f"means {base}↔cloud drift — report the failing run instead of retrying "
        "this PR."
    )
    return ok(
        no_op=True,
        summary=f"{summary} — 이미 {base} 브랜치에 동일 내용이 있어 PR을 열지 않음",
        file=path,
        usage=usage,
        followup=followup,
    )


def open_tfvars_pr(args: dict, **kwargs: Any) -> str:
    """봇 신원으로 에이전트가 쓸 수 있는 tfvars 파일 딱 하나를 바꾸는 PR을 연다.

    auto vs human은 repo의 CODEOWNERS가 정한다(무소유 surface=자동 머지, 소유
    surface=code-owner 리뷰 대기).

    args:
      surface  _SURFACES 키 중 하나 (예: "dev-ec2-ssh", "prod-dns")
      op       "set_entry" | "remove_entry" (json_map) | "set_value" (hcl_*)
      entry_key, entry   set_entry/remove_entry용
      value              set_value용
      reason   자유 텍스트 요청 맥락(PR 본문에 들어간다)

    브랜치는 `ops/agent-<surface>-<uniq>` (유니크 접미사로 충돌 방지). auto 머지
    여부는 이 도구가 아니라 CI(auto-merge.yml이 네이티브 auto-merge를 켜고,
    CODEOWNERS+ruleset이 게이트)가 정한다.
    """
    try:
        if not check_change_requirements():
            return fail(
                "change tools unavailable",
                "Set OPS_GITHUB_APP_ID / _PRIVATE_KEY_PATH / _INSTALLATION_ID / _REPO and install PyJWT.",
            )
        surface = args.get("surface")
        spec = _SURFACES.get(surface)
        if not spec:
            return fail(
                f"unknown surface '{surface}'",
                f"one of: {', '.join(sorted(_SURFACES))}",
            )
        op = args.get("op")
        reason = (args.get("reason") or "").strip() or "(사유 미기재)"
        var, path_file = spec["var"], spec["file"]
        env_dir = _env_dir(surface, spec)
        # dir가 빈 surface(packages 등)는 file이 리포 루트 기준 전체 경로다.
        path = f"{env_dir}/{path_file}" if env_dir else path_file
        # 브랜치=환경 매핑: dev surface는 dev 브랜치 기준으로 읽고 dev로 PR을 연다.
        base = spec.get("base", "main")

        # --- 구조화된 args로 새 파일 내용을 만든다 --------------------
        if spec["kind"] == "json_map":
            current = _fetch_current(path, base) or {var: {}}
            m = dict(current.get(var, {}))
            if op == "remove_entry":
                key = args.get("entry_key")
                if key not in m:
                    return fail(f"entry '{key}' not present", "nothing to remove")
                removed = m.pop(key)
                summary = f"{var}에서 `{key}` 제거"
                detail = (
                    f"**삭제** — `{var}`에서 entry `{key}`를 제거합니다. 지워지는 내용:\n"
                    f"```json\n{json.dumps({key: removed}, indent=2, ensure_ascii=False)}\n```"
                )
            elif op == "set_entry":
                key = args.get("entry_key")
                entry = args.get("entry")
                if not key or not isinstance(entry, dict):
                    return fail("set_entry needs entry_key + entry object", "")
                # 가볍지만 가치 있는 가드 (최종 판정은 CI guard 몫)
                if "cidr" in entry:
                    err = _validate_cidr(str(entry["cidr"]))
                    if err:
                        return fail(
                            err,
                            "pick a bounded IPv4 CIDR (/24-/32; /32 is one IP; not 0.0.0.0/0)",
                        )
                if var == "ec2_ssh_allowlist":
                    duplicate_key = next(
                        (
                            existing_key
                            for existing_key, existing_entry in m.items()
                            if existing_key != key
                            and existing_entry.get("cidr") == entry.get("cidr")
                        ),
                        None,
                    )
                    if duplicate_key:
                        return fail(
                            f"duplicate EC2 SSH CIDR {entry.get('cidr')} already exists under entry '{duplicate_key}'",
                            f"reuse or update entry_key '{duplicate_key}' instead of adding another SSH rule for the same CIDR",
                        )
                    sg_description = f"SSH {key} {entry.get('description') or ''}"
                    if not _AWS_SG_DESCRIPTION_RE.fullmatch(sg_description):
                        return fail(
                            "EC2 security group description has unsupported characters or exceeds 255 characters",
                            "use AWS-supported ASCII characters only (letters, numbers, spaces, . _ - : / () # , @ [] + = & ; {} ! $ *)",
                        )
                if var == "db_grants":
                    # 같은 CIDR을 다른 key로 두 번 넣으면 module의 db_grants validation이
                    # 중복 CIDR을 거부해 apply 전에 plan(guard)이 실패한다 — ec2-ssh/DNS와
                    # 같은 패턴으로 플러그인에서 먼저 막는다(2026-07-16 e2e #135: 만료 전
                    # 기존 grant가 살아있는데 같은 IP를 새 key로 재요청해 guard가 막았다).
                    duplicate_key = next(
                        (
                            existing_key
                            for existing_key, existing_entry in m.items()
                            if existing_key != key
                            and existing_entry.get("cidr") == entry.get("cidr")
                        ),
                        None,
                    )
                    if duplicate_key:
                        return fail(
                            f"duplicate DB grant CIDR {entry.get('cidr')} already exists under entry '{duplicate_key}'",
                            f"reuse or update entry_key '{duplicate_key}' (e.g. extend its expires_at) instead of adding another grant for the same CIDR",
                        )
                # expires_at을 준 grant(ec2-ssh/db-access)는 형식을 검사한다. 깨진
                # 형식은 access-expiry cron이 회수하지 못해 영구 grant가 되기 때문.
                exp = (entry.get("expires_at") or "").strip()
                if exp and not _EXPIRES_AT_RE.match(exp):
                    return fail(
                        f"expires_at must be ISO8601 UTC, got '{exp}'",
                        "e.g. 2026-07-15T00:00:00Z — malformed values are never auto-revoked",
                    )
                if var == "db_grants" and surface.startswith("prod-"):
                    # prod만 만료 필수(access-expiry cron이 회수). dev는 상시 접근이라
                    # 만료 생략 가능(비면 영구 grant) — module validation과 동일 기준.
                    if not entry.get("expires_at"):
                        return fail(
                            "prod db-access grants require expires_at (ISO8601 UTC)",
                            "add expires_at, e.g. 2026-07-15T00:00:00Z (access-expiry cron revokes it)",
                        )
                if var == "dns_records":
                    for req in ("name", "type", "content"):
                        if not (entry.get(req) or "").strip():
                            return fail(
                                f"dns record requires '{req}'",
                                "fields: name, type (A/CNAME/TXT/...), content (the record value, NOT 'value')",
                            )
                    # 같은 name+type의 두 번째 레코드는 Cloudflare가 apply에서
                    # 거부한다(CNAME 중복 등) — 머지 후 apply 실패 = 브랜치↔클라우드
                    # drift. ec2-ssh의 중복 CIDR 가드와 같은 패턴으로 막는다
                    # (2026-07-16 PR #102: 게이트웨이 재시작 후 재개된 요청이 같은
                    # 레코드를 다른 key로 다시 추가해 dev apply가 계속 실패).
                    duplicate_key = next(
                        (
                            existing_key
                            for existing_key, existing_entry in m.items()
                            if existing_key != key
                            and existing_entry.get("name") == entry.get("name")
                            and existing_entry.get("type") == entry.get("type")
                        ),
                        None,
                    )
                    if duplicate_key:
                        return fail(
                            f"duplicate DNS record {entry.get('type')} {entry.get('name')} already exists under entry '{duplicate_key}'",
                            f"reuse or update entry_key '{duplicate_key}' instead of adding a second record for the same name+type",
                        )
                if var == "waf_rules":
                    wip = (entry.get("ip") or "").strip()
                    err = _validate_waf_ip(wip)
                    if err:
                        return fail(
                            err,
                            "WAF 커스텀 룰은 특정 소스 IP(단일 또는 CIDR)를 막는다(무료 존 가능). "
                            'e.g. {"ip":"203.0.113.45","action":"block"}',
                        )
                    action = entry.get("action", "block")
                    if action not in ("block", "managed_challenge", "js_challenge"):
                        return fail(
                            "waf rule action must be block, managed_challenge, or js_challenge",
                            'e.g. {"ip":"203.0.113.45","action":"block"}',
                        )
                    wpath = entry.get("path") or ""
                    if wpath and not _WAF_PATH_RE.match(wpath):
                        return fail(
                            "waf rule 'path' must start with / and contain only [A-Za-z0-9/_.~-]",
                            "path를 주면 그 IP의 그 경로로 좁힌다(표현식 인젝션 방지). 안 주면 IP 전체.",
                        )
                summary = f"{var}에 `{key}` 설정"
                if key in m and m[key] == entry:
                    return _noop_ok(surface, spec, args, env_dir, path, summary)
                # PR 본문용 생성 vs 변경 구분 — 리뷰어가 diff 없이도 무엇이 새로
                # 생기고 무엇이 덮어써지는지 알 수 있게 before/after를 남긴다.
                prev_entry = m.get(key)
                if prev_entry is None:
                    detail = (
                        f"**생성** — `{var}`에 새 entry `{key}`를 추가합니다:\n"
                        f"```json\n{json.dumps({key: entry}, indent=2, ensure_ascii=False)}\n```"
                    )
                else:
                    detail = (
                        f"**변경** — `{var}`의 기존 entry `{key}`를 덮어씁니다.\n"
                        f"변경 전:\n```json\n{json.dumps({key: prev_entry}, indent=2, ensure_ascii=False)}\n```\n"
                        f"변경 후:\n```json\n{json.dumps({key: entry}, indent=2, ensure_ascii=False)}\n```"
                    )
                m[key] = entry
            else:
                return fail(
                    f"op '{op}' invalid for a map surface",
                    "use set_entry or remove_entry",
                )
            content = json.dumps({var: m}, indent=2) + "\n"
        elif spec["kind"] == "hcl_int":
            if op != "set_value":
                return fail(f"op '{op}' invalid for a scalar surface", "use set_value")
            value = args.get("value")
            lo = spec.get("min", 0)
            hi = spec.get("max", 2)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not (lo <= value <= hi)
            ):
                return fail(
                    f"value must be an int {lo}..{hi}",
                    f"this surface ({var}) caps at {lo}..{hi}",
                )
            summary = f"{var} = {value} 설정"
            cur = _fetch_current(path, base)
            prev_val = None
            if cur and "_raw" in cur:
                m = re.search(rf"{re.escape(var)}\s*=\s*(\d+)", cur["_raw"])
                if m:
                    prev_val = int(m.group(1))
            if spec.get("grow_only") and prev_val is not None:
                if value < prev_val:
                    return fail(
                        f"{var} {value} < current {prev_val} — shrink refused",
                        "grow-only: 볼륨은 키우기만 가능(축소=데이터 유실), "
                        "현재 크기 이상으로 요청하라",
                    )
                if value == prev_val:
                    return _noop_ok(surface, spec, args, env_dir, path, summary)
            if prev_val is None:
                detail = (
                    f"**생성** — `{var}`를 `{value}`로 새로 설정합니다(기존 값 없음)."
                )
            else:
                detail = f"**변경** — `{var}`: `{prev_val}` → `{value}`" + (
                    " (grow-only 설정 — 축소 불가)" if spec.get("grow_only") else ""
                )
            content = f"{var} = {value}\n"
        elif spec["kind"] == "hcl_service":
            # service.auto.tfvars: service_enabled(bool) + 사이즈 2개. value는 부분
            # 갱신용 dict(주는 키만 바꾸고 나머지는 현재값 유지). 삭제(=false)는 복구
            # 불가지만 dev surface는 무소유라 auto-merge, prod만 CODEOWNERS 승인.
            if op != "set_value":
                return fail(
                    f"op '{op}' invalid for the service surface", "use set_value"
                )
            value = args.get("value")
            if not isinstance(value, dict) or not value:
                return fail(
                    "service surface needs a value object",
                    'e.g. {"service_enabled": false} (삭제) 또는 '
                    '{"service_enabled": true, "ec2_instance_type": "t3.small"}',
                )
            # 현재값 파싱(없으면 기본값). 주는 키만 덮어쓴다.
            cur = _fetch_current(path, base)
            raw = cur["_raw"] if cur and "_raw" in cur else ""
            svc = {
                "service_enabled": True,
                "ec2_instance_type": "t3.micro",
                "db_instance_class": "db.t3.micro",
            }
            mb = re.search(r"service_enabled\s*=\s*(true|false)", raw)
            if mb:
                svc["service_enabled"] = mb.group(1) == "true"
            me = re.search(r'ec2_instance_type\s*=\s*"([^"]+)"', raw)
            if me:
                svc["ec2_instance_type"] = me.group(1)
            md = re.search(r'db_instance_class\s*=\s*"([^"]+)"', raw)
            if md:
                svc["db_instance_class"] = md.group(1)
            before = dict(svc)

            unknown = set(value) - set(svc)
            if unknown:
                return fail(
                    f"unknown service field(s): {', '.join(sorted(unknown))}",
                    "allowed: service_enabled, ec2_instance_type, db_instance_class",
                )
            if "service_enabled" in value:
                if not isinstance(value["service_enabled"], bool):
                    return fail("service_enabled must be a boolean", "true 또는 false")
                svc["service_enabled"] = value["service_enabled"]
            if "ec2_instance_type" in value:
                if value["ec2_instance_type"] not in _INSTANCE_TYPES:
                    return fail(
                        f"ec2_instance_type must be one of {sorted(_INSTANCE_TYPES)}",
                        "",
                    )
                svc["ec2_instance_type"] = value["ec2_instance_type"]
            if "db_instance_class" in value:
                if value["db_instance_class"] not in _DB_CLASSES:
                    return fail(
                        f"db_instance_class must be one of {sorted(_DB_CLASSES)}", ""
                    )
                svc["db_instance_class"] = value["db_instance_class"]

            summary = (
                "서비스 프로비저닝 (service_enabled=true)"
                if svc["service_enabled"]
                else "서비스 전체 철거 (service_enabled=false)"
            )
            # 파일이 없을 때(raw="") 기본값 그대로 쓰는 것은 실제 변경이므로 no-op이 아니다.
            if raw and svc == before:
                return _noop_ok(surface, spec, args, env_dir, path, summary)
            if not raw:
                detail = (
                    "**생성** — surface 파일이 없어 새로 만듭니다. 설정값:\n"
                    + "".join(f"- `{k}` = `{v}`\n" for k, v in svc.items())
                )
            else:
                detail = "**변경** — 바뀌는 필드:\n" + "".join(
                    f"- `{k}`: `{before[k]}` → `{svc[k]}`\n"
                    for k in svc
                    if before[k] != svc[k]
                )
            if before["service_enabled"] and not svc["service_enabled"]:
                detail += (
                    "\n⚠️ **되돌릴 수 없는 변경** — `service_enabled=false`는 이 환경의 "
                    "EC2/RDS/Bastion/ALB를 전부 destroy합니다(복구 불가, 데이터 유실 가능)."
                )
            content = (
                f"service_enabled   = {'true' if svc['service_enabled'] else 'false'}\n"
                f'ec2_instance_type = "{svc["ec2_instance_type"]}"\n'
                f'db_instance_class = "{svc["db_instance_class"]}"\n'
            )
        elif spec["kind"] == "yaml_pkg_list":
            # ansible vars 파일(extra_packages: [...]) 전체 교체. 패키지명은 apt로
            # 넘어가므로 데비안 패키지명 문자만 허용(셸/ansible 보간 인젝션 봉쇄).
            if op != "set_value":
                return fail(
                    f"op '{op}' invalid for a package-list surface", "use set_value"
                )
            value = args.get("value")
            if not isinstance(value, list) or not all(
                isinstance(p, str) and p for p in value
            ):
                return fail(
                    "value must be a list of package names",
                    'e.g. ["fail2ban", "auditd"] — the FULL desired list (replaces the current one)',
                )
            bad = [p for p in value if not re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", p)]
            if bad:
                return fail(
                    f"invalid package name(s): {', '.join(bad)}",
                    "Debian package names only: lowercase letters, digits, . + -",
                )
            pkgs = sorted(set(value))
            cur = _fetch_current(path, base)
            raw = cur["_raw"] if cur and "_raw" in cur else ""
            current_pkgs = sorted(
                set(re.findall(r"^\s*-\s*([A-Za-z0-9.+-]+)", raw, re.M))
            )
            summary = (
                f"{var} = [{', '.join(pkgs)}]"
                if pkgs
                else f"{var} 비움(추가 패키지 없음)"
            )
            if raw and current_pkgs == pkgs:
                return _noop_ok(surface, spec, args, env_dir, path, summary)
            final_list = ", ".join(f"`{p}`" for p in pkgs) or "(비움)"
            if not raw:
                detail = f"**생성** — `{var}` 목록을 새로 만듭니다: {final_list}"
            else:
                added = [p for p in pkgs if p not in current_pkgs]
                dropped = [p for p in current_pkgs if p not in pkgs]
                parts = []
                if added:
                    parts.append("추가: " + ", ".join(f"`{p}`" for p in added))
                if dropped:
                    parts.append("제거: " + ", ".join(f"`{p}`" for p in dropped))
                detail = (
                    f"**변경** — `{var}` 목록 갱신 ({' · '.join(parts)})\n"
                    f"최종 목록: {final_list}"
                )
            header = (
                "# 보안 패치 시 추가로 설치할 패키지 목록(2-08 surface). prod 플러그인이 편집한다.\n"
                "# 예: 보안 데몬(fail2ban), 감사 툴(auditd), CVE 대응 패키지 등.\n"
            )
            if pkgs:
                content = (
                    header + "extra_packages:\n" + "".join(f"  - {p}\n" for p in pkgs)
                )
            else:
                content = header + "extra_packages: []\n"
        else:
            return fail("unhandled surface kind", "")

        # --- PR을 연다(봇 신원) ----------------------------------------
        # cid는 브랜치명의 사람 가독용 슬러그. correlation_id/reason이 같아도(같은
        # 종류 요청 반복) 브랜치가 충돌하지 않도록 항상 유니크 접미사를 붙인다 —
        # 안 그러면 같은 브랜치를 재사용해 base drift로 CONFLICTING PR이 열린다.
        slug = (
            re.sub(r"[^a-z0-9]+", "-", (args.get("correlation_id") or reason).lower())[
                :16
            ].strip("-")
            or "change"
        )
        uniq = f"{int(time.time()) % 100000}-{secrets.token_hex(2)}"
        cid = f"{slug}-{uniq}"
        branch = f"ops/agent-{surface}-{cid}"
        # dir가 빈 surface(packages 등)는 scope가 비지 않게 surface 이름을 쓴다.
        title = f"ops({env_dir or surface}): {summary}"
        usage = _usage(surface, spec, args, env_dir)
        # PR 본문 — 리뷰어(사람)가 raw diff 없이도 무엇이 생성/변경/삭제되는지,
        # 머지되면 무슨 일이 일어나는지 알 수 있게 상세 설명을 싣는다.
        body = (
            f"ops 플러그인 write 도구로 생성된 PR입니다.\n\n"
            f"- 요청: {reason}\n"
            f"- 변경: {summary} (`{path}`)\n"
            f"- Surface: {surface} (agent-writable tfvars; `.tf`는 건드리지 않음)\n"
            f"- Base: `{base}` 브랜치 (브랜치=환경 — dev 머지는 2-1-dev apply, "
            "main 머지는 2-2-prod apply)\n"
            f"- 롤백: 이 PR을 `git revert` 하거나 반대 방향의 변경 PR을 엽니다.\n"
            "- Apply: 자동/사람 리뷰 여부는 CODEOWNERS가 결정합니다 — unowned surface는 "
            "guard 체크 통과 후 auto-merge, owned surface는 code-owner 리뷰 대기.\n"
            f"\n### 변경 상세\n\n{detail}\n"
            + (f"\n### 머지·apply 후\n\n{usage}\n" if usage else "")
        )
        pr = github_app.open_multi_file_pr(
            _repo(), {path: content}, branch, title, body, base=base
        )
        followup = (
            # auto vs human은 CODEOWNERS가 정한다. 에이전트는 최종 상태를 확인하고 보고한다.
            "Do NOT assume a human will review — most surfaces auto-merge. VERIFY THE FINAL "
            "STATE before replying: poll ops_github_get_pr_status(pr_url) every ~15s until "
            "state is MERGED (unowned surface, usually <60s); then find the apply run and poll "
            "ops_github_get_workflow_run until it succeeds; then confirm the change is live "
            "(re-query with a read tool). "
            "REPORT AS THE ops-change STATUS TIMELINE (see the ops-change skill). FIRST LINE "
            "= a one-line verdict: current state + what happens next, e.g. "
            "'✅ 반영 완료 — 아래 접속 명령 바로 사용 가능' or "
            "'🕓 승인 대기 — code owner 승인 시 apply까지 자동 진행'. Then the fixed lines "
            "① PR 열림 / ② guard(plan) / ③ 자동 머지 / ④ apply / ⑤ 반영 확인, each "
            "marked ✅ done · 🕓 pending/in-progress · ❌ failed, then the 🔗 PR link. Do NOT "
            "free-write prose. Fill ② from the `checks` field of ops_github_get_pr_status "
            "(the guard check-run conclusion); mark 🕓 미확인 ONLY when checks is empty or "
            "checks_lookup_error is set. For a dev auto-merge, all five resolve to ✅. If it "
            "stays OPEN past ~2min, this surface is CODEOWNERS-owned (human review) — but "
            "BEFORE claiming 승인 대기, check `review_requests` AND `reviews` in the same "
            "status response (submitting a review removes that person from review_requests, "
            "so an approved-but-unmerged PR also has empty review_requests): if BOTH are "
            "empty (and reviews_lookup_error is not set), no reviewer was ever assigned "
            "(the CODEOWNERS owner is invalid — needs write access — or a CODEOWNERS fix "
            "on the base branch postdates this PR), so mark ③ as 🕓 리뷰어 미지정 and say "
            "the fix: correct the owner / grant access, then update the PR branch or assign "
            "a reviewer manually. If reviews shows APPROVED but the PR is still OPEN, mark "
            "③ as 🕓 승인 완료·머지 대기. If reviews shows CHANGES_REQUESTED, mark ③ as "
            "❌ 변경 요청됨 and point to the PR link for the review comments — do not retry "
            "or open a replacement PR unless asked. Otherwise mark ③ "
            "as 🕓 승인 대기 (@code-owner) and OMIT lines ④·⑤ entirely: do not print pending "
            "future steps; instead close the timeline with one indented line "
            "'이후 apply·반영 확인은 승인 후 자동 진행'. If guard or apply fails, mark that "
            "line ❌ with a one-line reason. "
            "AFTER the timeline, relay the `usage` field (how to access/use/verify what was "
            "created); add a `⏱ <time> cron 자동 회수` line ONLY when the grant has an "
            "expires_at (prod access) — dev grants have no expiry, omit it. Fill placeholders "
            "like <bucket>/<db-endpoint>/<app-ip> with real values via read tools when known."
        )
        return ok(
            pr_url=pr["html_url"],
            pr_number=pr["number"],
            author=pr["author"],
            summary=summary,
            file=path,
            usage=usage,
            followup=followup,
        )
    except github_app.GitHubAppError as e:
        if "422" in str(e):
            remediation = (
                "422 usually means a branch/PR for this change already exists "
                "(same surface + correlation id) — check open PRs before retrying, "
                "or pass a new correlation_id"
            )
        else:
            remediation = "check the app installation + permissions (contents/pull_requests: write)"
        return fail(str(e), remediation)
    except Exception as e:  # 핸들러 밖으로 절대 raise하지 않는다
        return fail(f"open_tfvars_pr failed: {e}", "")


# ---------------------------------------------------------------- dev 코드 PR (2026-07-20 개방)
# dev 브랜치 한정으로 에이전트가 IaC 코드(기능 추가)를 직접 수정할 수 있다.
# 경계는 세 겹: (1) 이 allowlist — dev CODEOWNERS가 소유를 해제한 경로와 정확히
# 일치해야 한다(.github/·scripts/·2-0-setup/·2-2-prod/는 dev에서도 사람 소유),
# (2) CI guard(plan + 비용 백스톱 + ansible syntax), (3) prod 반영은 dev→main
# 승격 PR — main CODEOWNERS가 적용돼 사람 승인 필수.
_CODE_PR_BASE = "dev"
_CODE_PR_MAX_FILES = 10
_CODE_PR_MAX_BYTES = 200_000  # 파일당 — 랩의 .tf/.yml에 충분, 폭주 방지
_CODE_PR_PATH_RE = re.compile(r"^(2-1-dev|modules|ansible)/[A-Za-z0-9._/-]+$")
# 전용 surface가 있는 파일은 코드 PR로 건드리지 않는다(경로 이원화 방지).
_CODE_PR_DENY = {"ansible/patch-extra-packages.yml"}
_READ_FILE_REFS = ("dev", "main")


def _code_pr_path_error(path: str) -> str | None:
    if ".." in path or path.startswith("/"):
        return f"path traversal not allowed: {path}"
    if path in _CODE_PR_DENY:
        return (
            f"'{path}' has a dedicated surface — use ops_github_open_tfvars_pr "
            "(surface dev-packages/prod-packages) instead"
        )
    if not _CODE_PR_PATH_RE.match(path):
        return (
            f"path '{path}' is outside the dev-writable set — only 2-1-dev/, modules/, "
            "ansible/ are agent-writable (dev branch). .github/, scripts/, 2-0-setup/, "
            "2-2-prod/ stay human-owned"
        )
    return None


def read_repo_file(args: dict, **kwargs: Any) -> str:
    """IaC 리포 파일 하나를 읽는다(read-only). 코드 PR 전에 현재 내용을 확인해
    전체 내용 교체(open_code_pr) 시 기존 코드를 유실하지 않게 한다."""
    import base64

    try:
        if not check_change_requirements():
            return fail(
                "github tools unavailable",
                "Set OPS_GITHUB_APP_ID / _PRIVATE_KEY_PATH / _INSTALLATION_ID / _REPO and install PyJWT.",
            )
        path = (args.get("path") or "").strip()
        if not path or ".." in path or path.startswith("/"):
            return fail(f"invalid path: {path!r}", "repo-relative path, no traversal")
        ref = (args.get("ref") or _CODE_PR_BASE).strip()
        if ref not in _READ_FILE_REFS:
            return fail(
                f"ref must be one of {list(_READ_FILE_REFS)} (got {ref!r})",
                "브랜치=환경: dev 코드는 dev, prod/승격 확인은 main",
            )
        r = http_request(
            "GET",
            f"{github_app._API}/repos/{_repo()}/contents/{path}?ref={ref}",
            headers=github_app._headers(),
        )
        if r.status_code == 404:
            return ok(exists=False, path=path, ref=ref, content=None)
        if r.status_code >= 400:
            return fail(f"read {path}@{ref} -> {r.status_code}", r.text[:200])
        payload = r.json()
        if isinstance(payload, list):
            names = [x.get("name") for x in payload]
            return ok(exists=True, path=path, ref=ref, is_dir=True, entries=names)
        content = base64.b64decode(payload["content"]).decode("utf-8", "replace")
        truncated = len(content) > _CODE_PR_MAX_BYTES
        return ok(
            exists=True,
            path=path,
            ref=ref,
            content=content[:_CODE_PR_MAX_BYTES],
            truncated=truncated,
        )
    except github_app.GitHubAppError as e:
        return fail(str(e), "check the app installation (contents: read)")
    except Exception as e:  # 핸들러 밖으로 절대 raise하지 않는다
        return fail(f"read_repo_file failed: {e}", "")


def open_code_pr(args: dict, **kwargs: Any) -> str:
    """dev 브랜치 한정 IaC 코드 PR을 연다(파일 전체 내용 생성/교체).

    args:
      files    {repo-relative path: 파일 전체 새 내용} — 2-1-dev/·modules/·ansible/만
      title    한 줄 요약(커밋·PR 제목에 쓰인다)
      reason   요청 맥락(한국어, PR 본문)
      correlation_id  선택 — Slack 스레드 연결용
    """
    try:
        if not check_change_requirements():
            return fail(
                "change tools unavailable",
                "Set OPS_GITHUB_APP_ID / _PRIVATE_KEY_PATH / _INSTALLATION_ID / _REPO and install PyJWT.",
            )
        files = args.get("files")
        if not isinstance(files, dict) or not files:
            return fail(
                "files must be a non-empty object {path: full new content}",
                "read current content first with ops_github_read_file(path, ref='dev') — "
                "content REPLACES the whole file",
            )
        if len(files) > _CODE_PR_MAX_FILES:
            return fail(
                f"too many files ({len(files)} > {_CODE_PR_MAX_FILES})",
                "keep the change minimal — one feature per PR",
            )
        for path, content in files.items():
            err = _code_pr_path_error(str(path))
            if err:
                return fail(err, "dev-writable: 2-1-dev/, modules/, ansible/ only")
            if not isinstance(content, str) or not content.strip():
                return fail(
                    f"content for '{path}' must be a non-empty string (full file content)",
                    "deletions are not supported — a human removes files",
                )
            if len(content.encode()) > _CODE_PR_MAX_BYTES:
                return fail(
                    f"content for '{path}' exceeds {_CODE_PR_MAX_BYTES} bytes", ""
                )
        title_arg = (args.get("title") or "").strip()
        if not title_arg:
            return fail("title is required (one-line summary)", "")
        reason = (args.get("reason") or "").strip() or "(사유 미기재)"

        slug = (
            re.sub(
                r"[^a-z0-9]+", "-", (args.get("correlation_id") or title_arg).lower()
            )[:16].strip("-")
            or "code"
        )
        uniq = f"{int(time.time()) % 100000}-{secrets.token_hex(2)}"
        branch = f"ops/agent-code-{slug}-{uniq}"
        title = f"feat(dev): {title_arg}"
        touched = sorted(files)
        needs_apply = any(p.startswith(("2-1-dev/", "modules/")) for p in touched)
        body = (
            "ops 플러그인 코드 PR(dev 한정)입니다 — 에이전트가 IaC 코드로 기능을 추가/수정했습니다.\n\n"
            f"- 요청: {reason}\n"
            f"- 파일: {', '.join(f'`{p}`' for p in touched)}\n"
            "- Base: `dev` — dev CODEOWNERS가 이 경로들의 소유를 해제해 guard(plan + "
            "비용 백스톱 + ansible syntax) 통과 시 auto-merge됩니다.\n"
            "- prod 반영: dev→main 승격 PR에서 main CODEOWNERS(사람 승인)가 게이트합니다.\n"
            "- 롤백: 이 PR을 `git revert`한 원복 PR — 단, 인프라는 비대칭일 수 있습니다"
            "(EBS 축소 불가 등). plan을 확인하세요.\n"
        )
        pr = github_app.open_multi_file_pr(
            _repo(),
            {p: files[p] for p in touched},
            branch,
            title,
            body,
            base=_CODE_PR_BASE,
        )
        followup = (
            "DEV-ONLY code PR opened. VERIFY THE FINAL STATE before replying: poll "
            "ops_github_get_pr_status(pr_url) every ~15s until MERGED (auto-merge needs the "
            "guard check: plan + cost backstop + ansible syntax). "
            + (
                "Terraform paths were touched — after merge, find the tf-apply run and poll "
                "ops_github_get_workflow_run until success, then verify the feature is live "
                "with a read tool. "
                if needs_apply
                else "Only ansible/ was touched — NO tf-apply run; the change takes effect on "
                "the next ansible-ops dispatch (ref=dev). "
            )
            + "If guard FAILS on the cost backstop, the change used an instance type outside "
            "t3.micro/small (db.t3.micro/small) — do NOT weaken validation or retry with the "
            "same type; report the limit to the user. If the PR shows an EMPTY diff, close it "
            "and report '이미 반영됨'. For prod: NEVER open code PRs against main — tell the "
            "user prod needs a human-approved dev→main promotion PR."
        )
        return ok(
            pr_url=pr["html_url"],
            pr_number=pr["number"],
            author=pr["author"],
            summary=title_arg,
            files=touched,
            base=_CODE_PR_BASE,
            followup=followup,
        )
    except github_app.GitHubAppError as e:
        if "422" in str(e):
            remediation = (
                "422 usually means a branch/PR for this change already exists — "
                "check open PRs before retrying, or pass a new correlation_id"
            )
        else:
            remediation = "check the app installation + permissions (contents/pull_requests: write)"
        return fail(str(e), remediation)
    except Exception as e:  # 핸들러 밖으로 절대 raise하지 않는다
        return fail(f"open_code_pr failed: {e}", "")
