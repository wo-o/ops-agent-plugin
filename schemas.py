"""도구 JSON 스키마(read toolset + write: tfvars/code PR · ansible · 통지 제어).

원본 ops 플러그인에서 그대로 유지한 관례:
  · name = 동사 + 대상 + 경계 (ops_explain_alert, ops_aws_get_alb_target_health, ...)
  · 파라미터는 자유 텍스트보다 enum / 구조화 객체를 선호한다 (service는 텍스트가 아니라 enum)
  · description은 언제 호출하고 언제 호출하지 말아야 하는지를 정확히 말한다
  · read 도구에는 write surface가 전혀 없다 — write 스키마는 change/ansible 섹션과
    맨 아래 monitoring 섹션(silence · PD lifecycle · PD page)뿐이다
"""

from __future__ import annotations

# 앱 플릿은 tag:Role=app(+Name=<prefix>-* 스코프)으로 조회한다. prefix는 선택(기본 OPS_PROJECT_PREFIX).
PREFIX = {
    "type": "string",
    "description": "Project name prefix (default: OPS_PROJECT_PREFIX). Resources are the app fleet: tag Role=app scoped to Name=<prefix>-*.",
}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


# ---------------------------------------------------------------- 헬스 / 알람
OPS_GET_SERVICE_HEALTH = {
    "name": "ops_get_service_health",
    "description": "Read-only health summary for one service: ALB target health and instance states, all looked up by the Name tag. Use to answer 'is service X healthy?'. Never mutates.",
    "parameters": _obj({"prefix": PREFIX}, []),
}

OPS_EXPLAIN_ALERT = {
    "name": "ops_explain_alert",
    "description": "Explain a Grafana alert by rule name/id: rule state, firing instances, and which named metric/log query backs it. Read-only context for triage, never a remediation action.",
    "parameters": _obj(
        {
            "alert_id": {
                "type": "string",
                "description": "Grafana alert rule name (e.g. cpu-high) or rule UID.",
            },
            "time_window": {"type": "string", "default": "1h"},
        },
        ["alert_id"],
    ),
}

# ---------------------------------------------------------------- named 쿼리
OPS_QUERY_METRICS = {
    "name": "ops_query_metrics",
    "description": "Run a NAMED, pre-vetted Prometheus query (cpu_utilization | memory_used_pct | disk_usage_pct) with validated labels — free-form PromQL is not accepted. Returns series + dashboard URL. Read-only.",
    "parameters": _obj(
        {
            "query_name": {
                "type": "string",
                "enum": ["cpu_utilization", "memory_used_pct", "disk_usage_pct"],
                "description": "A key from the metrics catalog (data/named-queries.yaml).",
            },
            "labels": {
                "type": "object",
                "description": 'Label values to substitute (e.g. {"service": "demo-app"}).',
            },
            "range_hours": {"type": "integer", "default": 1, "maximum": 24},
        },
        ["query_name", "labels"],
    ),
}

OPS_QUERY_LOGS = {
    "name": "ops_query_logs",
    "description": 'Run a NAMED Loki log query over the app logs (job="app"): 5xx/ERROR lines by default, or lines containing a literal pattern (substring, not a regex). Returns capped log lines. Read-only.',
    "parameters": _obj(
        {
            "pattern": {
                "type": "string",
                "description": "Optional literal substring to match instead of the default 5xx/ERROR query.",
            },
            "range_hours": {"type": "integer", "default": 1, "maximum": 24},
        },
        [],
    ),
}

# ---------------------------------------------------------------- AWS (읽기 전용)
OPS_AWS_DESCRIBE_SERVICE = {
    "name": "ops_aws_get_service",
    "description": "Read-only AWS describe of everything for a service env: EC2 instances, bastion (bastion_instances[] — public_ip is the SSH tunnel entrypoint for RDS access guidance; never leave a <bastion_public_ip> placeholder in a final answer), EBS volumes, ALB target groups, load balancers (incl. dns_name — the CNAME content for DNS records), security-group ingress CIDR rules (security_groups[].ingress — use to self-verify an ec2_ssh_allowlist/db-access change landed as CIDR:port before reporting it open), and RDS databases (rds_instances[] — id/status/endpoint/port/publicly_accessible; empty list means no DB for this env, i.e. destroyed or not yet provisioned — use to answer 'does the RDS/DB endpoint exist?'). Never mutates; no free-form CLI.",
    "parameters": _obj({"prefix": PREFIX}, []),
}

OPS_AWS_GET_ALB_TARGET_HEALTH = {
    "name": "ops_aws_get_alb_target_health",
    "description": "Read-only ALB target-group health for a service env (target groups resolved by the Name tag). Use during rolling restarts to watch targets drain and register.",
    "parameters": _obj({"prefix": PREFIX}, []),
}

OPS_AWS_GET_COST_SUMMARY = {
    "name": "ops_aws_get_cost_summary",
    "description": "Read-only Cost Explorer amortized-cost summary for a trailing window (whole student account, grouped by a dimension). Use for 'what did this cost this week?'.",
    "parameters": _obj(
        {
            "period_days": {"type": "integer", "default": 7, "maximum": 90},
            "group_by": {
                "type": "string",
                "enum": ["SERVICE", "USAGE_TYPE"],
                "default": "SERVICE",
            },
        },
        [],
    ),
}

OPS_AWS_FIND_UNUSED_CANDIDATES = {
    "name": "ops_aws_find_unused_candidates",
    "description": "Report-only unused-resource CANDIDATES (unattached EBS, unassociated EIP, orphan project-prefixed SGs, Service-tagged snapshots, never-used project IAM roles). Excludes keep-tagged resources. NEVER deletes — idle != unused; a human confirms and cleanup goes through a PR.",
    "parameters": _obj({}, []),
}

# ---------------------------------------------------------------- GitHub (읽기 전용)
OPS_CHECK_PR_STATUS = {
    "name": "ops_github_get_pr_status",
    "description": "Read a repo PR state by URL and, after merge, return tf-apply run URLs matched to its merge commit. Poll the returned apply run with ops_github_get_workflow_run until success before claiming a change is live. For an OPEN PR also returns review_requests (pending reviewer assignments) and reviews (submitted reviews): both empty means CODEOWNERS never assigned a reviewer (invalid owner or a CODEOWNERS fix postdating the PR) — report that as 리뷰어 미지정, not as approval-wait. Read-only — never merges, comments, or edits.",
    "parameters": _obj(
        {
            "pr_url": {
                "type": "string",
                "description": "https://github.com/<owner>/<repo>/pull/<n>",
            }
        },
        ["pr_url"],
    ),
}

OPS_CHECK_WORKFLOW_RUN = {
    "name": "ops_github_get_workflow_run",
    "description": "Read the status/conclusion of a GitHub Actions run (plan/apply/destroy) by URL, plus head_sha/head_branch (the commit the run executed against). For a multi-step action (config PR merged, then an ansible run), only trust a run whose head_sha matches the merge commit — a run started before the merge landed used stale config. Read-only — never re-runs or cancels.",
    "parameters": _obj(
        {
            "run_url": {
                "type": "string",
                "description": "https://github.com/<owner>/<repo>/actions/runs/<id>",
            }
        },
        ["run_url"],
    ),
}


# ---------------------------------------------------------------- PagerDuty (읽기 전용)
OPS_PAGERDUTY_LIST_INCIDENTS = {
    "name": "ops_pagerduty_list_incidents",
    "description": "Read-only list of PagerDuty incidents in a trailing window (status, urgency, service, assignees). Use to answer 'what is firing / who is on it?'. NEVER acks, resolves, or triggers — incident lifecycle stays in PagerDuty; infra fixes go through the IaC PR path.",
    "parameters": _obj(
        {
            "status": {
                "type": "string",
                "enum": ["all", "triggered", "acknowledged", "resolved"],
                "default": "all",
                "description": "Filter by incident status.",
            },
            "since_hours": {"type": "integer", "default": 24, "maximum": 720},
            "limit": {"type": "integer", "default": 25, "maximum": 100},
        },
        [],
    ),
}

OPS_PAGERDUTY_GET_ONCALL = {
    "name": "ops_pagerduty_get_oncall",
    "description": "Read-only current on-call lookup: who is on call right now, per escalation policy and level, with schedule window. Use for 'who do I page / hand off to?'. Never modifies schedules or overrides.",
    "parameters": _obj({}, []),
}

# ---------------------------------------------------------------- Cloudflare (읽기 전용)
OPS_CLOUDFLARE_LIST_DNS_RECORDS = {
    "name": "ops_cloudflare_list_dns_records",
    "description": "Read-only DNS records of the Cloudflare zone (name, type, content, proxied, ttl). Use to check what a record currently points at. NEVER mutates — DNS changes go through ops_github_open_tfvars_pr (surface dev-dns/prod-dns).",
    "parameters": _obj(
        {
            "name_contains": {
                "type": "string",
                "description": "Optional case-insensitive substring filter on the record name.",
            },
            "type": {
                "type": "string",
                "enum": ["A", "AAAA", "CNAME", "TXT", "MX"],
                "description": "Optional record-type filter.",
            },
        },
        [],
    ),
}

OPS_CLOUDFLARE_LIST_WAF_RULES = {
    "name": "ops_cloudflare_list_waf_rules",
    "description": "Read-only WAF custom rules of the Cloudflare zone (http_request_firewall_custom phase: description, expression, action, enabled). Use to check what the WAF currently blocks. NEVER mutates — WAF changes go through ops_github_open_tfvars_pr (surface waf — zone-wide).",
    "parameters": _obj({}, []),
}

OPS_CLOUDFLARE_GET_ANALYTICS = {
    "name": "ops_cloudflare_get_analytics",
    "description": "Read-only Cloudflare edge HTTP analytics for the zone over a trailing window (httpRequests1hGroups): total_requests, per-class counts (2xx/3xx/4xx/5xx), and rate_5xx_pct. Use to answer 'what is the Cloudflare 5xx rate?' — status is edge response (what Cloudflare returned to clients), so a 5xx spike signals origin failures. Never mutates.",
    "parameters": _obj(
        {
            "period_hours": {"type": "integer", "default": 24, "maximum": 168},
        },
        [],
    ),
}


# ---------------------------------------------------------------- change (쓰기)
# write 경로 1/2: 앱의 봇 신원으로 범위가 제한된 단일 파일 tfvars PR을 연다.
# 내용은 자유 텍스트가 아니라 이 STRUCTURED args로 만들어지고,
# CI `guard` job이 결과를 다시 검사한다. .tf는 절대 수정되지 않는다; 여기서는
# 아무것도 apply하지 않는다 — 이 review-intent 도구는 사람이 머지해야 apply된다.
OPS_OPEN_TFVARS_PR = {
    "name": "ops_github_open_tfvars_pr",
    "description": (
        "Open a bounded pull request that changes EXACTLY ONE agent-writable *.tfvars file "
        "(e.g. allow an SSH source IP, grow a volume, scale a fleet, add a temporary DB access "
        "grant, provision an S3 bucket). This is the PRIMARY IaC write path — prefer it "
        "whenever a surface fits. If NO existing surface fits: on DEV the agent may author "
        "the feature itself with ops_github_open_code_pr (dev branch only); on PROD reply "
        "that prod needs a human-approved dev→main promotion PR. The bot opens the PR; "
        "whether it AUTO-MERGES or waits for a HUMAN is decided by CODEOWNERS on the repo, NOT "
        "by this tool — unowned surfaces auto-merge once the guard check passes, owned surfaces "
        "(all prod-*) wait for a code-owner review. Do NOT use THIS tool for .tf module "
        "edits (that is ops_github_open_code_pr, dev only) or any direct cloud mutation. If the request is ambiguous or missing a required detail "
        "(e.g. which IP, a name/purpose, an expiry, the target value), ASK the user one or two "
        "concrete questions FIRST instead of inventing values — never guess destructive/irreversible "
        "details. After opening, obey the 'followup' field in the result: verify the final state "
        "(poll until merged+applied) before reporting success, and relay the 'usage' field."
    ),
    "parameters": _obj(
        {
            "surface": {
                "type": "string",
                "enum": [
                    "dev-service",
                    "dev-ec2-ssh",
                    "dev-db-access",
                    "dev-disk",
                    "dev-dns",
                    "prod-service",
                    "prod-ec2-ssh",
                    "prod-db-access",
                    "prod-disk",
                    "prod-dns",
                    "waf",
                    "dev-packages",
                    "prod-packages",
                ],
                "description": "Which agent-writable surface to change. Env-keyed <env>-<type>: env is dev (2-1-dev, unowned=auto-merge) or prod (2-2-prod, owned=human review); type is service/ec2-ssh/db-access/disk/dns/packages. Each tfvars surface edits exactly one *.auto.tfvars in that env root. The service surface (service_enabled — false destroys the whole stack) auto-merges on dev like the other dev surfaces; on prod it waits for human review. 'dev-packages'/'prod-packages' both edit ansible/patch-extra-packages.yml but on DIFFERENT branches (branch=environment: dev branch vs main) — extra packages installed by the NEXT security-patch run on that env (the dispatch checks out the matching branch); both are unowned (auto-merge). ONE ZONE-LEVEL surface is NOT env-keyed: 'waf' (2-2-prod/waf.auto.tfvars.json — the Cloudflare zone allows ONE custom-firewall ruleset, so blocking an IP applies zone-wide to dev AND prod; use for any '<env> WAF' request; unowned, base=main).",
            },
            "op": {
                "type": "string",
                "enum": ["set_entry", "remove_entry", "set_value"],
                "description": 'set_entry/remove_entry for map surfaces (ec2-ssh, db-access, dns, waf); set_value for the scalar/list surfaces (disk: int GiB; service: partial-update object; dev-/prod-packages: FULL list of extra package names, e.g. ["fail2ban", "auditd"]).',
            },
            "entry_key": {
                "type": "string",
                "description": "Map entry key, e.g. 'office-ssh' (map surfaces).",
            },
            "entry": {
                "type": "object",
                "description": (
                    "Map entry object for set_entry. Shape depends on the surface type: "
                    'ec2-ssh -> {"cidr":"203.0.113.10/32","expires_at":"2026-07-15T00:00:00Z","description":"office"} (IPv4 CIDR prefix /24-/32; single-IP /32 allowed; description must use AWS-supported ASCII characters; expires_at OPTIONAL, omit=permanent, set=auto-revoked by access-expiry cron); '
                    'db-access -> {"cidr":"1.2.3.4/32","expires_at":"2026-07-14T00:00:00Z","description":"..."} (cidr required; expires_at always set on prod: user-given duration, or default 1h when unspecified — do not ask, apply 1h and tell the requester it auto-deletes in 1 hour and to ask for more time if needed; optional on dev; opens bastion SSH for RDS access); '
                    'dns -> {"type":"A","name":"status.example.com","content":"203.0.113.10","proxied":false} (field is content, NOT value); '
                    'waf -> {"ip":"1.2.3.4","action":"block","path":"/optional"} (block/challenge a source IP; action one of block|managed_challenge|js_challenge, default block — the free Cloudflare zone cannot do per-IP rate limits, so "rate limit this IP" requests become a block rule).'
                ),
            },
            "value": {
                "description": 'Scalar value (set_value). disk surface: data_volume_size_gb int, 1..100 (GiB), grow-only. service surface: partial-update object, e.g. {"service_enabled": true, "ec2_instance_type": "t3.small"} or {"service_enabled": false} (false = destroy the whole stack — destructive; auto-merges on dev, human-reviewed on prod — proceed without re-asking when the request names the env and destroy intent explicitly; confirm with the user first only when either is ambiguous).'
            },
            "reason": {
                "type": "string",
                "description": "The originating request in plain words, written in KOREAN; goes in the PR body.",
            },
            "correlation_id": {
                "type": "string",
                "description": "Optional id to tie the branch/PR back to a Slack thread.",
            },
        },
        ["surface", "op"],
    ),
}

# ------------------------------------------------------- dev 코드 PR (write, 2026-07-20 개방)
OPS_READ_FILE = {
    "name": "ops_github_read_file",
    "description": (
        "Read ONE file from the IaC repo (read-only; ref 'dev' or 'main', default dev). "
        "ALWAYS read the current content of every file you are about to change BEFORE "
        "calling ops_github_open_code_pr — that tool takes FULL file contents and "
        "REPLACES the file, so writing blind loses existing code. Also useful to check "
        "what a module/playbook currently does, or whether a feature already exists. "
        "Reading a directory path returns its entry names."
    ),
    "parameters": _obj(
        {
            "path": {
                "type": "string",
                "description": "Repo-relative path, e.g. 'modules/service/main.tf' or '2-1-dev/variables.tf'.",
            },
            "ref": {
                "type": "string",
                "enum": ["dev", "main"],
                "description": "Branch to read from (branch=environment: dev code lives on dev, prod/promoted code on main). Default dev.",
            },
        },
        ["path"],
    ),
}

OPS_OPEN_CODE_PR = {
    "name": "ops_github_open_code_pr",
    "description": (
        "Open a DEV-ONLY IaC code pull request (base=dev) that creates or fully replaces "
        "files under 2-1-dev/, modules/, or ansible/ — for feature requests NO tfvars "
        "surface covers (e.g. add a new resource, extend modules/service, author a new "
        "playbook). Prefer ops_github_open_tfvars_pr whenever a surface fits. "
        "WORKFLOW: (1) read every target file first with ops_github_read_file (contents "
        "REPLACE the whole file), (2) keep the diff minimal — one feature per PR, match "
        "the surrounding code style, (3) NEVER touch .github/, scripts/, 2-0-setup/, "
        "2-2-prod/, CODEOWNERS (tool rejects them; they stay human-owned even on dev), "
        "(4) NEVER weaken variable validation or cost caps — the CI cost backstop "
        "(instance types t3.micro/small, db.t3.micro/small only) fails guard regardless. "
        "The PR auto-merges once guard (plan + cost backstop + ansible syntax) passes, "
        "because dev CODEOWNERS unowns these paths. PROD: never open code PRs against "
        "main — prod is reached only via a human-approved dev→main promotion PR; say so "
        "when asked for a prod feature. File deletion is not supported (a human deletes)."
    ),
    "parameters": _obj(
        {
            "files": {
                "type": "object",
                "description": "Map of repo-relative path → FULL new file content (string). Allowed roots: 2-1-dev/, modules/, ansible/. Max 10 files.",
            },
            "title": {
                "type": "string",
                "description": "One-line summary of the feature/change (goes into the PR/commit title as 'feat(dev): <title>').",
            },
            "reason": {
                "type": "string",
                "description": "The originating request in plain words, written in KOREAN; goes in the PR body.",
            },
            "correlation_id": {
                "type": "string",
                "description": "Optional id to tie the branch/PR back to a Slack thread.",
            },
        },
        ["files", "title", "reason"],
    ),
}

# ---------------------------------------------------------------- ansible (write, GHA 트리거)
# 두 번째 write 도구: PR을 거치지 않고 remediation playbook을 실행한다. 단, 이 호스트에서
# 직접 SSH로 돌리지 않고 IaC 리포의 ansible-ops workflow를 workflow_dispatch로 트리거한다
# (실제 ansible은 VPC 안 self-hosted 러너에서 실행). tfvars PR과 달리 사람 리뷰 없이 바로
# 라이브 호스트를 바꾸므로 카탈로그 봉쇄(고정 playbook 키)와 params 검증, --limit fleet
# 스코프, --check(dry_run)로 경계를 잡는다. 모니터링 2-3 incident-response: 알람(메모리·행
# -> 롤링 재시작, 디스크 -> 확장, 패치)에서 tfvars로는 못 되돌리는 런타임 상태 조치에만 쓴다.
OPS_RUN_ANSIBLE_PLAYBOOK = {
    "name": "ops_run_ansible_playbook",
    "description": (
        "Trigger ONE bounded remediation ansible playbook against a single environment's "
        "fleet, for runtime-state fixes a tfvars PR cannot make (rolling restart, filesystem "
        "grow, security patch). This dispatches the IaC repo's ansible-ops GitHub Actions "
        "workflow (ansible runs on an in-VPC self-hosted runner, NOT on this host). Unlike "
        "ops_github_open_tfvars_pr it does NOT open a PR and gets NO human review — the "
        "workflow EXECUTES immediately on live hosts, so use it only for the remediations "
        "below and prefer a tfvars PR whenever an existing surface fits. Pick a playbook KEY "
        "from a fixed catalog (never supply a path): 'rolling-restart' (drain from ALB TG, "
        "restart the app service, wait /healthz, re-register; memory-leak/hang), 'disk-grow' "
        "(expand the filesystem after a disk.auto.tfvars volume grow), 'security-patch' "
        "(serial rolling patch + optional reboot + package surface), 'monitoring-agents' "
        "(install/reinstall node_exporter + promtail on a fleet — the fix when scrape-down "
        "up==0 is no-data because the fleet was freshly provisioned or replaced by an "
        "app_version bump; NOT for soft-hang, use rolling-restart there), 'rds-temp-user' (create "
        "or DROP a time-limited PostgreSQL role via the bastion — this is how you fulfil a "
        "'temporary read-only DB account that auto-expires' request: FIRST open the "
        "<env>-db-access tfvars PR to open the bastion network path, THEN run this with params "
        "temp_user + valid_until (ISO8601 UTC; user-given duration, or default 1h from now when "
        "the request has no duration — do not ask, apply 1h and tell the requester it auto-deletes "
        "in 1 hour and to ask for more time if needed) + grant_mode(readonly|readwrite) + "
        "state(present|absent); the access-expiry cron DROPs it after valid_until), 'rds-readonly-user' "
        "(ensure the standing dev-only readonly PostgreSQL account via the bastion, idempotent — run it "
        "once after a dev service-provisioning apply succeeds and RDS is available; its password is a "
        "fixed demo value kept in the playbook itself so nothing is returned — when the user asks for "
        "it, read ansible/rds-readonly-user.yml (ref=dev) via ops_github_read_file and share the value; "
        "dev ONLY — prod standing accounts are "
        "forbidden, use rds-temp-user for prod). You MUST pass environment "
        "('dev' or 'prod') — it scopes --limit env_<env> and runs serial-1 (halts on first "
        "host failure). Set dry_run=true first if unsure — it maps to --check and changes "
        "nothing. This returns a run_url but does NOT wait for completion: obey the 'followup' "
        "— poll ops_github_get_workflow_run(run_url) until it succeeds, THEN verify with a read "
        "tool that targets are back in-service / the metric dropped before claiming the alert "
        "is resolved. If ambiguous about which environment, ASK before running."
    ),
    "parameters": _obj(
        {
            "playbook": {
                "type": "string",
                "enum": [
                    "rolling-restart",
                    "disk-grow",
                    "security-patch",
                    "monitoring-agents",
                    "rds-temp-user",
                    "rds-readonly-user",
                ],
                "description": "Which catalog playbook to run.",
            },
            "environment": {
                "type": "string",
                "enum": ["dev", "prod"],
                "description": "Target environment fleet (--limit env_<environment>).",
            },
            "params": {
                "type": "object",
                "description": "Optional bounded -e vars (per-playbook enum). Currently none required.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "true => --check (report what would happen, change nothing). Default false.",
            },
            "reason": {
                "type": "string",
                "description": "The originating alarm/request in plain words; for the audit trail.",
            },
            "correlation_id": {
                "type": "string",
                "description": "Optional id to tie the run back to a Slack thread.",
            },
        },
        ["playbook", "environment"],
    ),
}


# ---------------------------------------------------------------- monitoring 쓰기 (범위 제한)
OPS_GRAFANA_SILENCE = {
    "name": "ops_grafana_silence",
    "description": (
        "Manage Grafana alert silences (mute notifications; rule evaluation keeps running). "
        "action=create mutes one alertname (optionally scoped to one instance) for a bounded "
        "duration (max 24h, comment required); action=expire un-silences by silence_id; "
        "action=list shows active/pending silences. Use AFTER a remediation is underway or a "
        "human asked to mute — NEVER to hide an undiagnosed alert. Exact-match only, no regex."
    ),
    "parameters": _obj(
        {
            "action": {
                "type": "string",
                "enum": ["create", "expire", "list"],
                "description": "create = new silence, expire = end one now, list = show active/pending.",
            },
            "alertname": {
                "type": "string",
                "description": "create: exact alert rule name to silence (as shown by ops_explain_alert / rules).",
            },
            "instance": {
                "type": "string",
                "description": "create(optional): scope to one instance label value instead of the whole rule.",
            },
            "duration_minutes": {
                "type": "integer",
                "default": 60,
                "minimum": 1,
                "maximum": 1440,
                "description": "create: auto-expiry in minutes (max 24h). Indefinite silences are not possible.",
            },
            "comment": {
                "type": "string",
                "description": "create: required — why muted + expected recovery (audited).",
            },
            "silence_id": {
                "type": "string",
                "description": "expire: id returned by create or list.",
            },
        },
        ["action"],
    ),
}

OPS_PAGERDUTY_MANAGE_INCIDENT = {
    "name": "ops_pagerduty_manage_incident",
    "description": (
        "Bounded PagerDuty incident lifecycle: acknowledge (stop escalation, keep working), "
        "snooze (re-alert after a bounded delay, max 24h), resolve (incident is fixed and "
        "verified). Only these three actions on an existing incident id — never triggers new "
        "incidents, never edits schedules/services. resolve only AFTER the underlying issue is "
        "verified fixed (metrics back to normal), not to quiet noise — use snooze for that."
    ),
    "parameters": _obj(
        {
            "incident_id": {
                "type": "string",
                "description": "PagerDuty incident id (from ops_pagerduty_list_incidents).",
            },
            "action": {
                "type": "string",
                "enum": ["acknowledge", "snooze", "resolve"],
            },
            "snooze_minutes": {
                "type": "integer",
                "default": 60,
                "minimum": 1,
                "maximum": 1440,
                "description": "snooze only: minutes until it re-alerts.",
            },
        },
        ["incident_id", "action"],
    ),
}

# Grafana 알람은 Slack 단일 통지다(PD 자동 페이지 없음). 온콜 실페이지는 런북이
# 자동 대응을 포기했을 때(§4 서킷 브레이커) 이 도구가 유일한 경로다.
OPS_PAGERDUTY_PAGE_ONCALL = {
    "name": "ops_pagerduty_page_oncall",
    "description": (
        "PAGE the human on-call via PagerDuty (Events API v2 trigger -> escalation "
        "policy). Alarms notify Slack only — nothing pages automatically — so this tool "
        "is the ONLY way a human phone rings. Call it ONLY when the incident-response "
        "runbook gives up on automatic remediation: the circuit breaker fired (same alarm "
        "re-fired 3+ times in ~15min, or a remediation landed but the alarm stayed "
        "firing), the situation has no runbook path (instance fully gone, cause "
        "undiagnosable, irreversible action needed), or a human explicitly asked to be "
        "paged. NEVER page for alarms you are still handling, for repeat notifications "
        "of an episode already paged, or to 'keep someone informed' — that is what the "
        "Slack thread is for. dedup_key is REQUIRED and must identify the episode "
        "(alert UID + active_at): PagerDuty collapses repeated triggers with the same "
        "dedup_key into one open incident, so re-calling never double-pages. After a "
        "successful page you may report '페이지 완료 (dedup_key=...)'. Do NOT "
        "acknowledge/resolve the incident this creates — it belongs to the human now; "
        "the page must keep escalating until they take it."
    ),
    "parameters": _obj(
        {
            "summary": {
                "type": "string",
                "description": "One-line page text the on-call sees first: env + alertname + why automation stopped (max 1024 chars).",
            },
            "dedup_key": {
                "type": "string",
                "description": "Episode identity, e.g. '<alert UID>:<active_at>'. Same key = same incident, no extra page (max 255 chars).",
            },
            "details": {
                "type": "object",
                "description": "Optional structured context: attempted remediations (PR/run URLs), current state, why stopped.",
            },
            "link": {
                "type": "string",
                "description": "Optional Slack thread permalink attached to the incident.",
            },
        },
        ["summary", "dedup_key"],
    ),
}
