"""read toolset 핸들러 — 전부 read-only.

계약(원본 ops 플러그인에서 이식):
  · 모든 핸들러는 (args: dict, **kwargs)를 받아 JSON 문자열을 반환한다 —
    _compat의 ok()/fail() 봉투 — 그리고 절대 raise하지 않는다;
  · 누락/잘못된 파라미터 -> 예외가 아니라 fail(...);
  · 자격증명이 필요한 핸들러는 클라이언트의 check_*()로 한 번 더 확인하고, 자격증명이
    없으면 remediation 힌트가 담긴 깔끔한 error JSON을 반환한다(보통은
    check_fn visibility gate가 먼저 모델로부터 그 도구를 숨긴다);
  · 모든 result는 EVIDENCE(쿼리, window, dashboard URL, 출처)를 담는다.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from . import settings
from ._compat import fail, ok
from .clients import aws, cloudflare, github, grafana, pagerduty


# check_fn probe (도구 노출 게이트) ------------------------------------------------
def check_grafana_requirements() -> bool:
    return grafana.check_grafana()


def check_aws_requirements() -> bool:
    return aws.check_aws()


def check_github_requirements() -> bool:
    return github.check_github()


def check_pagerduty_requirements() -> bool:
    return pagerduty.check_pagerduty()


def check_cloudflare_requirements() -> bool:
    return cloudflare.check_cloudflare()


# 헬퍼 ---------------------------------------------------------------------------
def _parse_pr_url(url: str) -> tuple[str, int]:
    p = urlparse(url)
    m = re.match(r"^/([^/]+/[^/]+)/pull/(\d+)", p.path)
    if not m:
        raise ValueError(f"not a PR url: {url}")
    return m.group(1), int(m.group(2))


def _parse_run_url(url: str) -> tuple[str, int]:
    p = urlparse(url)
    m = re.match(r"^/([^/]+/[^/]+)/actions/runs/(\d+)", p.path)
    if not m:
        raise ValueError(f"not a workflow-run url: {url}")
    return m.group(1), int(m.group(2))


_AWS_REMEDIATION = (
    "Install boto3 and set OPS_AWS_READ_ROLE to the full ARN of the "
    "<project>-hermes-readonly role created by 2-0-setup "
    "(default project name: ops-agent-iac)."
)
_GRAFANA_REMEDIATION = (
    "Set OPS_GRAFANA_URL to the monitoring server (http://<ip>:3000) and "
    "OPS_GRAFANA_TOKEN to a read-only service-account token."
)
# 설정은 됐는데 도달이 안 될 때. Hermes·monitoring은 같은 VPC의 공유 SG를 달고 있어
# Grafana 조회는 monitoring private IP:3000을 shared SG self-rule로 pull한다. 도달 실패면
# OPS_GRAFANA_URL이 공인 IP를 가리키거나(private여야 함), foundation이 미apply라 shared
# SG self 규칙이 없거나, 스택 컨테이너가 안 떠 있는 경우다.
_GRAFANA_UNREACHABLE_REMEDIATION = (
    "Grafana is configured but unreachable (connect/timeout). OPS_GRAFANA_URL should be "
    "the monitoring PRIVATE IP (http://<private-ip>:3000, from `terraform output ops_grafana_url`) "
    "— Hermes reaches it over the shared SG inside the VPC, no public IP or manual SG rule needed. "
    "Verify with `curl -sS <OPS_GRAFANA_URL>/api/health`."
)


def _looks_unreachable(err: Exception) -> bool:
    """네트워크 도달 실패(연결 거부/타임아웃)인지 대략 판별 — 설정 오류와 구분해
    SG 개방 remediation을 붙이기 위함."""
    s = str(err).lower()
    return any(
        k in s for k in ("timed out", "timeout", "connect", "refused", "unreachable")
    )


_PAGERDUTY_REMEDIATION = (
    "Set OPS_PAGERDUTY_TOKEN to a read-only PagerDuty REST API token in ~/.hermes/.env."
)
_CLOUDFLARE_REMEDIATION = (
    "Set OPS_CLOUDFLARE_READ_TOKEN (read-only zone token: Zone.DNS Read + Zone.WAF Read "
    "+ Zone Analytics Read for ops_cloudflare_get_analytics) "
    "and OPS_CLOUDFLARE_ZONE_ID (the 2-06 lab zone id)."
)


# 핸들러 --------------------------------------------------------------------------
def get_service_health(args: dict, **kwargs: Any) -> str:
    """한 서비스의 헬스 요약: ALB target health + 인스턴스."""
    try:
        prefix = args.get("prefix") or settings.project_prefix()
        if not aws.check_aws():
            return fail("AWS read client not available", remediation=_AWS_REMEDIATION)

        instances = aws.describe_instances_by_lab(prefix)
        targets: list[dict] = []
        for tg_arn in aws.resolve_target_group_arns_by_lab(prefix):
            targets.append(
                {"target_group_arn": tg_arn, "targets": aws.alb_target_health(tg_arn)}
            )

        healthy_targets = sum(
            1 for tg in targets for t in tg["targets"] if t.get("state") == "healthy"
        )
        total_targets = sum(len(tg["targets"]) for tg in targets)
        summary = {
            "prefix": prefix,
            "instances_running": sum(
                1 for i in instances if i.get("state") == "running"
            ),
            "instances_total": len(instances),
            "alb_targets_healthy": f"{healthy_targets}/{total_targets}"
            if targets
            else "no ALB in this lab",
        }
        return ok(
            health=summary,
            instances=instances,
            alb=targets,
            evidence=[
                {
                    "source": "aws (assumed read-only role)",
                    "lookup": f"tag:Role=app + tag:Name={prefix}-*",
                    "apis": [
                        "DescribeInstances",
                        "DescribeAutoScalingGroups",
                        "DescribeTargetHealth",
                    ],
                }
            ],
        )
    except Exception as e:
        return fail(f"get_service_health failed: {e}")


def explain_alert(args: dict, **kwargs: Any) -> str:
    """Grafana alert 룰을 설명한다: 상태 + firing 인스턴스 + 근거 named 쿼리.

    요약만 한다 — 절대 조치하지 않는다. 여기 인용된 구체적인 series/라인은
    ops_query_metrics / ops_query_logs로 가져온다.
    """
    try:
        alert_id = args.get("alert_id")
        if not alert_id:
            return fail("alert_id is required")
        window = args.get("time_window", "1h")
        if not grafana.check_grafana():
            return fail("grafana not configured", remediation=_GRAFANA_REMEDIATION)
        rules = grafana.list_alert_rules()
        needle = str(alert_id).lower()
        matched = [
            r
            for r in rules
            if needle in str(r.get("name", "")).lower()
            or needle == str(r.get("uid", "")).lower()
            or needle == str(r.get("labels", {}).get("rule_uid", "")).lower()
            or needle == str(r.get("labels", {}).get("__alert_rule_uid__", "")).lower()
        ]
        if not matched:
            return fail(
                f"no alert rule matching {alert_id!r}",
                remediation="Known rules: "
                + ", ".join(sorted(str(r.get("name")) for r in rules)),
            )
        return ok(
            alert_id=alert_id,
            rules=matched,
            explanation=(
                "Rule state and firing instances above; the lab alert rules are "
                "backed by the same named queries this plugin exposes "
                "(cpu_utilization / memory_used_pct / disk_usage_pct / app_5xx)."
            ),
            evidence=[
                {
                    "source": "grafana unified alerting (rules API)",
                    "time_window": window,
                }
            ],
            note="Use ops_query_metrics/ops_query_logs to pull the concrete series/lines cited here.",
        )
    except Exception as e:
        return fail(f"explain_alert failed: {e}")


def query_metrics(args: dict, **kwargs: Any) -> str:
    try:
        qname = args.get("query_name")
        labels = args.get("labels") or {}
        if not qname:
            return fail("query_name is required (a key from the metrics catalog)")
        if not grafana.check_grafana():
            return fail("grafana not configured", remediation=_GRAFANA_REMEDIATION)
        rng = min(int(args.get("range_hours", 1)), 24)
        res = grafana.query_metrics(qname, labels, rng)
        return ok(**res)
    except Exception as e:
        rem = _GRAFANA_UNREACHABLE_REMEDIATION if _looks_unreachable(e) else None
        return fail(f"query_metrics failed: {e}", remediation=rem)


def query_logs(args: dict, **kwargs: Any) -> str:
    try:
        if not grafana.check_grafana():
            return fail("grafana not configured", remediation=_GRAFANA_REMEDIATION)
        rng = min(int(args.get("range_hours", 1)), 24)
        pattern = args.get("pattern")
        qname = "service_pattern" if pattern else "app_5xx"
        # 앱 로그는 job="app" 고정 스트림이다(promtail). 기본 5xx 쿼리는 라벨이
        # 없고, pattern 검색만 리터럴 pattern을 치환한다.
        labels = {"pattern": pattern} if pattern else {}
        res = grafana.query_logs(qname, labels, rng)
        return ok(**res)
    except Exception as e:
        rem = _GRAFANA_UNREACHABLE_REMEDIATION if _looks_unreachable(e) else None
        return fail(f"query_logs failed: {e}", remediation=rem)


def aws_describe_service(args: dict, **kwargs: Any) -> str:
    try:
        prefix = args.get("prefix") or settings.project_prefix()
        if not aws.check_aws():
            return fail("AWS read client not available", remediation=_AWS_REMEDIATION)
        instances = aws.describe_instances_by_lab(prefix)
        bastion_instances = aws.describe_bastions_by_lab(prefix)
        volumes = aws.describe_volumes_by_lab(prefix)
        target_groups = aws.resolve_target_group_arns_by_lab(prefix)
        load_balancers = aws.describe_load_balancers_by_lab(prefix)
        security_groups = aws.describe_security_groups_by_lab(prefix)
        rds_instances = aws.describe_db_instances_by_lab(prefix)
        return ok(
            prefix=prefix,
            instances=instances,
            bastion_instances=bastion_instances,
            volumes=volumes,
            target_groups=target_groups,
            load_balancers=load_balancers,
            security_groups=security_groups,
            rds_instances=rds_instances,
            evidence=[
                {
                    "source": "aws",
                    "lookup": f"tag:Role=app + tag:Name={prefix}-* (bastion: tag:Role=bastion, "
                    f"volumes: {prefix}-*-data-*, "
                    f"ALBs: name {prefix}-*, SGs: group-name {prefix}-*, "
                    f"RDS: DBInstanceIdentifier {prefix}-*)",
                }
            ],
            note="load_balancers[].dns_name is the CNAME content when wiring a domain "
            "to an ALB via the <env>-dns tfvars surface. security_groups[].ingress lists "
            "live CIDR rules — after an ec2_ssh_allowlist/db-access change is merged and "
            "applied, confirm the requested CIDR:port appears here before reporting it open "
            "(EC2 running alone is not proof the rule landed). rds_instances lists the live "
            "RDS databases (id/status/endpoint/port/publicly_accessible) — an empty list "
            "means no DB exists for this env (destroyed or not yet provisioned); endpoint is "
            "the address app/bastion connect to. bastion_instances[].public_ip is the SSH "
            "tunnel entrypoint for RDS access — use it to complete ssh/psql guidance with "
            "the real IP instead of a placeholder.",
        )
    except Exception as e:
        return fail(f"aws_describe_service failed: {e}")


def aws_get_alb_target_health(args: dict, **kwargs: Any) -> str:
    try:
        prefix = args.get("prefix") or settings.project_prefix()
        if not aws.check_aws():
            return fail("AWS read client not available", remediation=_AWS_REMEDIATION)
        tg_arns = aws.resolve_target_group_arns_by_lab(prefix)
        if not tg_arns:
            return fail(
                f"no target group found for prefix '{prefix}'",
                remediation="Only labs with an ALB (04/06/10) have target groups; "
                "confirm the lab is applied.",
            )
        groups = [
            {"target_group_arn": arn, "targets": aws.alb_target_health(arn)}
            for arn in tg_arns
        ]
        return ok(prefix=prefix, target_groups=groups)
    except Exception as e:
        return fail(f"aws_get_alb_target_health failed: {e}")


def aws_get_cost_summary(args: dict, **kwargs: Any) -> str:
    try:
        if not aws.check_aws():
            return fail("AWS read client not available", remediation=_AWS_REMEDIATION)
        period_days = min(int(args.get("period_days", 7)), 90)
        if period_days < 1:
            period_days = 1
        group_by = str(args.get("group_by", "SERVICE"))
        if group_by not in ("SERVICE", "USAGE_TYPE"):
            return fail("group_by must be SERVICE or USAGE_TYPE")
        summary = aws.cost_summary(period_days, group_by)
        return ok(
            cost=summary,
            note="Whole student account (labs are too small for per-tag cost "
            "allocation to be activated by default).",
        )
    except Exception as e:
        return fail(f"aws_get_cost_summary failed: {e}")


def aws_find_unused_candidates(args: dict, **kwargs: Any) -> str:
    try:
        if not aws.check_aws():
            return fail("AWS read client not available", remediation=_AWS_REMEDIATION)
        candidates = aws.find_unused_candidates()
        return ok(
            candidates=candidates,
            note="REPORT ONLY. idle != unused (the monitoring seed includes a "
            "keep-tagged volume to teach exactly this false positive). A human "
            "confirms, then cleanup goes through a PR — this tool never deletes.",
        )
    except Exception as e:
        return fail(f"aws_find_unused_candidates failed: {e}")


def check_pr_status(args: dict, **kwargs: Any) -> str:
    try:
        url = args.get("pr_url")
        if not url:
            return fail("pr_url is required")
        repo, number = _parse_pr_url(url)
        if not github.check_github():
            return fail(
                "GitHub not configured",
                remediation="Set GITHUB_TOKEN (read-only fine-grained PAT on the lab repo).",
            )
        pr = github.get_pr(repo, number)
        # apply run은 PR 응답에 없으므로 merge_commit_sha로 Actions run을 역조회한다.
        # 두 경로 모두 이 PR의 apply다: dev 봇 auto-merge는 push 이벤트가 억제돼
        # workflow_dispatch로 돌고, 사람/admin 머지(prod 포함)는 push 이벤트로 돈다.
        # (2026-07-16 e2e C1-I4: dispatch만 허용해 prod apply를 "미발견" 처리했었다.
        # merge SHA는 이 PR의 머지 커밋에만 존재하므로 push run을 포함해도 오인 없음.)
        # Actions API의 일시 오류는 이미 얻은 PR 상태를 버리지 않고 별도 필드로 보존한다.
        apply_runs: list[dict[str, Any]] = []
        apply_runs_lookup_error: str | None = None
        merge_sha = pr.get("merge_commit_sha") if pr.get("merged") else None
        if merge_sha:
            try:
                for run in github.get_workflow_runs_for_head(repo, merge_sha):
                    if run.get("name") == "tf-apply" and run.get("event") in (
                        "workflow_dispatch",
                        "push",
                    ):
                        apply_runs.append(
                            {
                                "run_url": run.get("html_url"),
                                "status": run.get("status"),
                                "conclusion": run.get("conclusion"),
                                "event": run.get("event"),
                                "created_at": run.get("created_at"),
                            }
                        )
            except Exception as e:
                apply_runs_lookup_error = str(e)
        # guard/plan 은 PR head 에서 도는 tf-plan 워크플로의 job 이다. 에이전트가
        # "guard(plan) 결과 직접 확인 불가"라던 공백을 메운다(2026-07-16 e2e C1-I4).
        # check-runs API 대신 Actions job API 로 읽는다 — 전자는 App 설치 토큰에 별도
        # Checks:read 권한이 필요해 403 이 나지만(2026-07-17 라이브 검증), 후자는 apply run
        # 조회와 같은 Actions:read 로 되기 때문이다. PR 이벤트로 돈 run 의 job 만 모은다
        # (apply run 은 머지 커밋 기준의 push/dispatch 라 head SHA 와 겹치지 않는다).
        # Actions API 오류는 이미 얻은 PR 상태를 버리지 않고 별도 필드로 보존한다.
        checks: list[dict[str, Any]] = []
        checks_lookup_error: str | None = None
        head_sha = (pr.get("head") or {}).get("sha")
        if head_sha:
            try:
                for run in github.get_workflow_runs_for_head(repo, head_sha):
                    if run.get("event") != "pull_request":
                        continue
                    for job in github.get_workflow_jobs(repo, run.get("id")):
                        checks.append(
                            {
                                "name": job.get("name"),
                                "status": job.get("status"),
                                "conclusion": job.get("conclusion"),
                            }
                        )
            except Exception as e:
                checks_lookup_error = str(e)
        # owned surface에서 OPEN인데 review_requests가 비어 있으면 code owner가
        # 리뷰어로 지정되지 못한 것이다(CODEOWNERS owner가 write 권한이 없어 무효,
        # 또는 base의 CODEOWNERS 수정이 PR 생성 이후라 미반영). "승인 대기"와
        # 구분해 보고할 수 있도록 PR 응답에 이미 있는 값을 그대로 노출한다.
        review_requests = [
            u.get("login")
            for u in (pr.get("requested_reviewers") or [])
            if u.get("login")
        ] + [t.get("slug") for t in (pr.get("requested_teams") or []) if t.get("slug")]
        # 단, requested_reviewers는 리뷰가 제출되면 그 사람이 빠진다 — 빈 목록만으로는
        # "미지정"과 "이미 승인·머지 대기"를 구분할 수 없어 제출된 리뷰도 함께 노출한다.
        # 조회 오류는 다른 lookup과 같은 패턴으로 PR 상태를 버리지 않고 별도 필드에 보존.
        reviews: list[dict[str, Any]] = []
        reviews_lookup_error: str | None = None
        if pr.get("state") == "open":
            try:
                reviews = [
                    {
                        "user": (rv.get("user") or {}).get("login"),
                        "state": rv.get("state"),
                    }
                    for rv in github.get_pr_reviews(repo, number)
                ]
            except Exception as e:
                reviews_lookup_error = str(e)
        return ok(
            pr_url=url,
            state=pr.get("state"),
            merged=pr.get("merged"),
            mergeable=pr.get("mergeable"),
            title=pr.get("title"),
            review_requests=review_requests,
            reviews=reviews,
            reviews_lookup_error=reviews_lookup_error,
            checks=checks,
            checks_lookup_error=checks_lookup_error,
            apply_runs=apply_runs,
            apply_runs_lookup_error=apply_runs_lookup_error,
        )
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail(f"check_pr_status failed: {e}")


def pagerduty_list_incidents(args: dict, **kwargs: Any) -> str:
    """PagerDuty incident 목록(read-only). ack/resolve는 하지 않는다."""
    try:
        if not pagerduty.check_pagerduty():
            return fail("PagerDuty not configured", remediation=_PAGERDUTY_REMEDIATION)
        status = str(args.get("status", "all"))
        if status == "all":
            statuses = list(pagerduty.VALID_STATUSES)
        elif status in pagerduty.VALID_STATUSES:
            statuses = [status]
        else:
            return fail(
                f"invalid status {status!r}; allowed: all, "
                + ", ".join(pagerduty.VALID_STATUSES)
            )
        since_hours = min(int(args.get("since_hours", 24)), 720)
        if since_hours < 1:
            since_hours = 1
        limit = min(int(args.get("limit", 25)), 100)
        incidents = pagerduty.list_incidents(statuses, since_hours, limit)
        return ok(
            incidents=incidents,
            count=len(incidents),
            evidence=[
                {
                    "source": "pagerduty REST /incidents (read-only token)",
                    "window_hours": since_hours,
                    "statuses": statuses,
                }
            ],
            note="READ ONLY. Ack/resolve happen in PagerDuty itself; infra "
            "remediation goes through the IaC repo PR path.",
        )
    except Exception as e:
        return fail(f"pagerduty_list_incidents failed: {e}")


def pagerduty_get_oncall(args: dict, **kwargs: Any) -> str:
    """현재 온콜(에스컬레이션 정책/레벨/사람) 조회 — read-only."""
    try:
        if not pagerduty.check_pagerduty():
            return fail("PagerDuty not configured", remediation=_PAGERDUTY_REMEDIATION)
        oncalls = pagerduty.get_oncalls()
        return ok(
            oncalls=oncalls,
            count=len(oncalls),
            evidence=[{"source": "pagerduty REST /oncalls (read-only token)"}],
        )
    except Exception as e:
        return fail(f"pagerduty_get_oncall failed: {e}")


def cloudflare_list_dns_records(args: dict, **kwargs: Any) -> str:
    """2-06 lab zone의 DNS 레코드 조회 — read-only. 변경은 tfvars PR로."""
    try:
        if not cloudflare.check_cloudflare():
            return fail(
                "Cloudflare not configured", remediation=_CLOUDFLARE_REMEDIATION
            )
        record_type = args.get("type")
        if record_type and record_type not in ("A", "AAAA", "CNAME", "TXT", "MX"):
            return fail("type must be one of A, AAAA, CNAME, TXT, MX")
        records = cloudflare.list_dns_records(
            name_contains=args.get("name_contains"), record_type=record_type
        )
        try:
            zone_name = cloudflare.zone_name()
        except Exception:
            zone_name = None
        return ok(
            records=records,
            count=len(records),
            zone_id=settings.cloudflare_zone_id(),
            zone_name=zone_name,
            evidence=[{"source": "cloudflare v4 /dns_records (read-only zone token)"}],
            note="READ ONLY. To change a record, open a tfvars PR via "
            "ops_github_open_tfvars_pr (surface dev-dns). DNS record `name` MUST be "
            "under `zone_name` above (`<label>.<zone_name>`); a requested FQDN in any "
            "other zone (placeholder like example.com) is silently suffixed with "
            "zone_name — substitute the host label into zone_name instead.",
        )
    except Exception as e:
        return fail(f"cloudflare_list_dns_records failed: {e}")


def cloudflare_list_waf_rules(args: dict, **kwargs: Any) -> str:
    """2-06 lab zone의 WAF 커스텀 룰 조회 — read-only. 변경은 tfvars PR로."""
    try:
        if not cloudflare.check_cloudflare():
            return fail(
                "Cloudflare not configured", remediation=_CLOUDFLARE_REMEDIATION
            )
        rulesets = cloudflare.list_waf_custom_rules()
        return ok(
            rulesets=rulesets,
            zone_id=settings.cloudflare_zone_id(),
            evidence=[
                {
                    "source": "cloudflare v4 /rulesets "
                    "(phase http_request_firewall_custom, read-only zone token)"
                }
            ],
            note="READ ONLY. To change WAF rules, open a tfvars PR via "
            "ops_github_open_tfvars_pr (surface waf).",
        )
    except Exception as e:
        return fail(f"cloudflare_list_waf_rules failed: {e}")


def cloudflare_get_analytics(args: dict, **kwargs: Any) -> str:
    """2-06 lab zone의 엣지 HTTP 상태코드 분포 + 5xx 비율 — read-only.

    'Cloudflare 5xx 비율이 임계 넘었나?'에 답한다(cron 정기 점검·온디맨드 모두). 5xx
    급증은 origin 장애 신호라 app 로그(ops_query_logs)·ALB target health와 함께 본다."""
    try:
        if not cloudflare.check_cloudflare():
            return fail(
                "Cloudflare not configured", remediation=_CLOUDFLARE_REMEDIATION
            )
        period_hours = min(int(args.get("period_hours", 24)), 168)
        if period_hours < 1:
            period_hours = 1
        summary = cloudflare.http_status_summary(period_hours)
        return ok(
            analytics=summary,
            zone_id=settings.cloudflare_zone_id(),
            evidence=[
                {
                    "source": "cloudflare v4 /graphql httpRequests1hGroups "
                    "(read-only zone token, Zone Analytics:Read)"
                }
            ],
            note="READ ONLY. Edge response status over the trailing window; a 5xx "
            "spike signals origin failures — correlate with app logs (ops_query_logs) "
            "and ALB target health (ops_aws_get_alb_target_health).",
        )
    except Exception as e:
        return fail(f"cloudflare_get_analytics failed: {e}")


def check_workflow_run(args: dict, **kwargs: Any) -> str:
    try:
        url = args.get("run_url")
        if not url:
            return fail("run_url is required")
        repo, run_id = _parse_run_url(url)
        if not github.check_github():
            return fail(
                "GitHub not configured",
                remediation="Set GITHUB_TOKEN (read-only fine-grained PAT on the lab repo).",
            )
        run = github.get_workflow_run(repo, run_id)
        return ok(
            run_url=url,
            status=run.get("status"),
            conclusion=run.get("conclusion"),
            name=run.get("name"),
            head_sha=run.get("head_sha"),
            head_branch=run.get("head_branch"),
            note="head_sha is the commit this run executed against. For a multi-step "
            "action (config PR merged, then an ansible run), only treat the run as "
            "authoritative when head_sha matches the merge commit — an earlier run started "
            "before the merge landed reflects stale config/inventory.",
        )
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail(f"check_workflow_run failed: {e}")
