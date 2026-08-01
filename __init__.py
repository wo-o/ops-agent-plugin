"""ops — 인프라 운영용 Hermes 플러그인 (read 조회 + bounded write).

얇은 standalone 플러그인 하나. read toolset은 ops-read 하나로 전부 read-only이고, write는
tfvars PR(ops-github-write) · bounded ansible 카탈로그 실행(ops-ansible-write) ·
알람 통지 상태 조작(ops-monitoring-write: Grafana silence + PD incident lifecycle)
세 경로다. auto vs human 머지는
이 도구가 아니라 repo의 CODEOWNERS+ruleset이 정한다.
다섯 개의 read-only 클라이언트로 "lab X의 상태가 어떤가?"에 답한다:

  · AWS       — boto3 세션은 오직 <project>-hermes-readonly 롤
                (OPS_AWS_READ_ROLE)을 assume해서만 생성된다; 앱 플릿은 Role=app 태그(+Name=<project>-* 스코프)로 조회한다
  · Grafana   — 모니터링 스택에서 named Prometheus/Loki 쿼리 + alert-rule 상태 조회
  · GitHub    — lab 리포의 PR / workflow-run 상태 조회(read-only PAT)
  · PagerDuty — incident 목록 / 현재 온콜 조회 (+ write 경로: ack/snooze/resolve)
  · Cloudflare— 2-06 zone의 DNS 레코드 / WAF 커스텀 룰 / 엣지 HTTP analytics(5xx 비율)
                조회(read 전용 zone 토큰)

클라우드를 직접 변경하는 경로는 없다 — 모든 변경(tfvars PR,
emergency action)은 IaC 리포의 CI를 거친다. register(ctx)가 배선하는 것:
read 도구 16개(toolset ops-read 하나) + write 도구 6개(ops-github-write의
tfvars PR·dev 코드 PR, ops-ansible-write의 카탈로그 플레이북,
ops-monitoring-write의 Grafana silence·PD incident·온콜 페이지), post_tool_call
audit 훅(append-only JSONL), 그리고 한국어 runbook skill 네 개(ops-operating,
ops-change, ops-incident-response, ops-incident-rca — 조치 toolset이 꺼진 모드에서
알람을 진단·보고만 하는 explain-only runbook).

자격증명 게이팅에는 check_fn(visibility gate)을 쓴다: 자격증명이 없는 도구는 모델에
아예 노출되지 않는다; 그래도 호출되면 remediation 힌트가 담긴 깔끔한 error JSON을
반환한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    audit,
    schemas,
    tools_ansible,
    tools_change,
    tools_monitoring,
    tools_observability,
)

_PLUGIN_DIR = Path(__file__).resolve().parent

# (name, toolset, schema, handler, check_fn)
_TOOLS: list[tuple[str, str, dict, Any, Any]] = [
    (
        "ops_get_service_health",
        "ops-read",
        schemas.OPS_GET_SERVICE_HEALTH,
        tools_observability.get_service_health,
        tools_observability.check_aws_requirements,
    ),
    (
        "ops_explain_alert",
        "ops-read",
        schemas.OPS_EXPLAIN_ALERT,
        tools_observability.explain_alert,
        tools_observability.check_grafana_requirements,
    ),
    (
        "ops_query_metrics",
        "ops-read",
        schemas.OPS_QUERY_METRICS,
        tools_observability.query_metrics,
        tools_observability.check_grafana_requirements,
    ),
    (
        "ops_query_logs",
        "ops-read",
        schemas.OPS_QUERY_LOGS,
        tools_observability.query_logs,
        tools_observability.check_grafana_requirements,
    ),
    (
        "ops_aws_get_service",
        "ops-read",
        schemas.OPS_AWS_DESCRIBE_SERVICE,
        tools_observability.aws_describe_service,
        tools_observability.check_aws_requirements,
    ),
    (
        "ops_aws_get_alb_target_health",
        "ops-read",
        schemas.OPS_AWS_GET_ALB_TARGET_HEALTH,
        tools_observability.aws_get_alb_target_health,
        tools_observability.check_aws_requirements,
    ),
    (
        "ops_aws_get_cost_summary",
        "ops-read",
        schemas.OPS_AWS_GET_COST_SUMMARY,
        tools_observability.aws_get_cost_summary,
        tools_observability.check_aws_requirements,
    ),
    (
        "ops_aws_find_unused_candidates",
        "ops-read",
        schemas.OPS_AWS_FIND_UNUSED_CANDIDATES,
        tools_observability.aws_find_unused_candidates,
        tools_observability.check_aws_requirements,
    ),
    (
        "ops_github_get_pr_status",
        "ops-read",
        schemas.OPS_CHECK_PR_STATUS,
        tools_observability.check_pr_status,
        tools_observability.check_github_requirements,
    ),
    (
        "ops_github_get_workflow_run",
        "ops-read",
        schemas.OPS_CHECK_WORKFLOW_RUN,
        tools_observability.check_workflow_run,
        tools_observability.check_github_requirements,
    ),
    (
        "ops_pagerduty_list_incidents",
        "ops-read",
        schemas.OPS_PAGERDUTY_LIST_INCIDENTS,
        tools_observability.pagerduty_list_incidents,
        tools_observability.check_pagerduty_requirements,
    ),
    (
        "ops_pagerduty_get_oncall",
        "ops-read",
        schemas.OPS_PAGERDUTY_GET_ONCALL,
        tools_observability.pagerduty_get_oncall,
        tools_observability.check_pagerduty_requirements,
    ),
    (
        "ops_cloudflare_list_dns_records",
        "ops-read",
        schemas.OPS_CLOUDFLARE_LIST_DNS_RECORDS,
        tools_observability.cloudflare_list_dns_records,
        tools_observability.check_cloudflare_requirements,
    ),
    (
        "ops_cloudflare_list_waf_rules",
        "ops-read",
        schemas.OPS_CLOUDFLARE_LIST_WAF_RULES,
        tools_observability.cloudflare_list_waf_rules,
        tools_observability.check_cloudflare_requirements,
    ),
    (
        "ops_cloudflare_get_analytics",
        "ops-read",
        schemas.OPS_CLOUDFLARE_GET_ANALYTICS,
        tools_observability.cloudflare_get_analytics,
        tools_observability.check_cloudflare_requirements,
    ),
    # write 경로 = 봇 신원 tfvars PR 하나. auto vs human은 이 도구가 아니라 repo의
    # CODEOWNERS가 정한다(무소유 surface=자동 머지, 소유 surface=code-owner 리뷰).
    # check_change_requirements로 GitHub App 자격증명 유무에 따라 게이트한다.
    (
        "ops_github_open_tfvars_pr",
        "ops-github-write",
        schemas.OPS_OPEN_TFVARS_PR,
        tools_change.open_tfvars_pr,
        tools_change.check_change_requirements,
    ),
    # dev 코드 PR 경로(2026-07-20 개방): 읽기(현재 내용 확인)는 read 토올셋,
    # 쓰기는 tfvars PR과 같은 github-write 토올셋. dev 브랜치의 modules/·
    # 2-1-dev/·ansible/ 한정 — 경계는 tools_change의 경로 allowlist +
    # dev CODEOWNERS(소유 해제) + CI guard(plan·비용 백스톱·ansible syntax).
    (
        "ops_github_read_file",
        "ops-read",
        schemas.OPS_READ_FILE,
        tools_change.read_repo_file,
        tools_change.check_change_requirements,
    ),
    (
        "ops_github_open_code_pr",
        "ops-github-write",
        schemas.OPS_OPEN_CODE_PR,
        tools_change.open_code_pr,
        tools_change.check_change_requirements,
    ),
    # 두 번째 write 경로 = bounded ansible 실행(PR 없이 즉시). tfvars가
    # apply를 사람 리뷰 후 CI로 미루는 것과 달리 라이브 호스트를 바로 바꾸므로 카탈로그
    # 봉쇄 + --limit fleet 스코프 + dry_run으로 경계를 잡는다. 모니터링 알람 밴드의
    # 런타임 상태 조치(재시작·정리·재기동) 전용. 실행은 IaC 리포의 ansible-ops workflow를
    # workflow_dispatch로 트리거해 VPC 안 self-hosted 러너에서 돈다 — change 경로와 같은
    # GitHub App 자격증명 + repo가 있어야 노출된다(check_ansible_requirements).
    (
        "ops_run_ansible_playbook",
        "ops-ansible-write",
        schemas.OPS_RUN_ANSIBLE_PLAYBOOK,
        tools_ansible.run_ansible_playbook,
        tools_ansible.check_ansible_requirements,
    ),
    # 세 번째 write 경로 = 알람 통지 상태의 bounded 조작(tools_monitoring 참조).
    # Grafana silence(만료 필수·exact-match), PD incident lifecycle(ack/snooze/
    # resolve), PD 온콜 페이지(Events v2 trigger·dedup_key 필수 — 알람이 Slack
    # 단일 통지이므로 런북 §4 에스컬레이션의 유일한 실페이지 경로).
    (
        "ops_grafana_silence",
        "ops-monitoring-write",
        schemas.OPS_GRAFANA_SILENCE,
        tools_monitoring.grafana_silence,
        tools_monitoring.check_grafana_silence_requirements,
    ),
    (
        "ops_pagerduty_manage_incident",
        "ops-monitoring-write",
        schemas.OPS_PAGERDUTY_MANAGE_INCIDENT,
        tools_monitoring.pagerduty_manage_incident,
        tools_monitoring.check_pagerduty_write_requirements,
    ),
    (
        "ops_pagerduty_page_oncall",
        "ops-monitoring-write",
        schemas.OPS_PAGERDUTY_PAGE_ONCALL,
        tools_monitoring.pagerduty_page_oncall,
        tools_monitoring.check_pagerduty_page_requirements,
    ),
]

# 유일한 훅: 모든 도구 호출에 대한 append-only JSONL 감사. pre_tool_call block
# 훅은 없다 — write 도구의 유일한 출구가 PR이고, 차단은 CI(guard·ruleset·
# CODEOWNERS)가 담당하므로 플러그인 레벨 차단이 불필요하다.
_HOOKS = [
    ("post_tool_call", audit.record_tool_call),
]

_SKILLS = [
    (
        "ops-operating",
        "skills/ops-operating/SKILL.md",
        "Read-only lab lookup runbook: health, alerts, metrics/logs, cost, PR status",
    ),
    (
        "ops-change",
        "skills/ops-change/SKILL.md",
        "Scale-up vs scale-out decision runbook + bounded tfvars PR remediation (CODEOWNERS-gated)",
    ),
    (
        "ops-incident-response",
        "skills/ops-incident-response/SKILL.md",
        "Monitoring-alarm-driven autonomous remediation runbook (bands 2-21~29): alarm in Slack -> diagnose -> tfvars PR or ansible -> resolve",
    ),
    (
        "ops-incident-rca",
        "skills/ops-incident-rca/SKILL.md",
        "Explain-only alarm RCA runbook: diagnose cause with read tools, report evidence + recommendation, no mutations",
    ),
]


def register(ctx) -> None:
    # 도구 — 실패는 게이트웨이 시작 시 스택트레이스로 드러나게 그대로 raise한다
    for name, toolset, schema, handler, check_fn in _TOOLS:
        kwargs: dict[str, Any] = {
            "name": name,
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
        }
        if check_fn is not None:
            kwargs["check_fn"] = check_fn
        ctx.register_tool(**kwargs)

    # 라이프사이클 훅
    for hook_name, cb in _HOOKS:
        ctx.register_hook(hook_name, cb)

    # 네임스페이스가 지정된 runbook skill (read-only, 자동 목록에 안 뜸)
    for skill_name, rel, desc in _SKILLS:
        ctx.register_skill(skill_name, _PLUGIN_DIR / rel, description=desc)
