"""Handler 계약 테스트. 모델도, 실제 credential도, boto3/httpx도 필요 없다.

모든 handler에 대해 다음을 검증한다: JSON 문자열(절대 dict가 아님)을 반환하고,
{"success": bool} envelope를 사용하며, 누락/잘못된 파라미터에는 예외를 던지지 않고
에러로 처리하고, credential이 없을 때는 remediation을 담은 깔끔한 에러로 degrade한다.
"""

import json
from unittest import mock

import ops_plugin.tools_observability as to

ALL_HANDLERS_AND_EMPTY_ARGS = [
    to.get_service_health,
    to.explain_alert,
    to.query_metrics,
    to.query_logs,
    to.aws_describe_service,
    to.aws_get_alb_target_health,
    to.check_pr_status,
    to.check_workflow_run,
]


def _parse(s):
    assert isinstance(s, str), f"handler must return a JSON string, got {type(s)}"
    return json.loads(s)


# --- envelope 계약 ---------------------------------------------------------------
def test_every_handler_with_required_params_missing_errors_not_raises():
    for handler in ALL_HANDLERS_AND_EMPTY_ARGS:
        out = _parse(handler({}))
        assert out["success"] is False, handler.__name__
        assert "error" in out, handler.__name__


def test_handlers_accept_extra_kwargs():
    # hook/handler 호출 규약은 프레임워크 kwargs를 넘긴다; 여기서 터지면 안 된다
    out = _parse(to.get_service_health({}, session_id="s1", channel="#lab"))
    assert out["success"] is False


# --- credential degradation (conftest가 모든 credential을 제거) --------------------------------
def test_aws_tools_fail_cleanly_without_read_role():
    out = _parse(to.get_service_health({}))
    assert out["success"] is False
    assert "OPS_AWS_READ_ROLE" in out.get("remediation", "")

    out = _parse(to.aws_find_unused_candidates({}))
    assert out["success"] is False

    out = _parse(to.aws_get_cost_summary({}))
    assert out["success"] is False


def test_grafana_tools_fail_cleanly_without_grafana():
    out = _parse(
        to.query_metrics(
            {"query_name": "cpu_utilization", "labels": {"service": "demo-app"}}
        )
    )
    assert out["success"] is False
    assert "GRAFANA" in out.get("remediation", "")

    out = _parse(to.query_logs({"service": "nginx"}))
    assert out["success"] is False

    out = _parse(to.explain_alert({"alert_id": "lab-cpu-high"}))
    assert out["success"] is False


def test_github_tools_fail_cleanly_without_token():
    out = _parse(
        to.check_pr_status({"pr_url": "https://github.com/acme/ops-agent-iac/pull/7"})
    )
    assert out["success"] is False
    assert "GITHUB_TOKEN" in out.get("remediation", "")


# --- URL 파싱 ----------------------------------------------------------------------
def test_check_pr_status_rejects_non_pr_url():
    out = _parse(
        to.check_pr_status({"pr_url": "https://github.com/acme/repo/issues/3"})
    )
    assert out["success"] is False
    assert "not a PR url" in out["error"]


def test_check_workflow_run_rejects_non_run_url():
    out = _parse(to.check_workflow_run({"run_url": "https://example.com/whatever"}))
    assert out["success"] is False
    assert "not a workflow-run url" in out["error"]


# --- mock 클라이언트 정상 경로 ----------------------------------------------------------
def test_get_service_health_summarizes_mocked_aws():
    with (
        mock.patch.object(to.aws, "check_aws", return_value=True),
        mock.patch.object(
            to.aws,
            "describe_instances_by_lab",
            return_value=[{"id": "i-1", "state": "running"}],
        ),
        mock.patch.object(
            to.aws, "resolve_target_group_arns_by_lab", return_value=["arn:tg/1"]
        ),
        mock.patch.object(
            to.aws,
            "alb_target_health",
            return_value=[{"target": "i-1", "state": "healthy"}],
        ),
    ):
        out = _parse(to.get_service_health({}))
    assert out["success"] is True
    assert out["health"]["instances_running"] == 1
    assert out["health"]["alb_targets_healthy"] == "1/1"
    assert "app" in out["evidence"][0]["lookup"]


def test_aws_describe_service_includes_alb_dns_name():
    with (
        mock.patch.object(to.aws, "check_aws", return_value=True),
        mock.patch.object(to.aws, "describe_instances_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_bastions_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_volumes_by_lab", return_value=[]),
        mock.patch.object(
            to.aws, "resolve_target_group_arns_by_lab", return_value=["arn:tg/1"]
        ),
        mock.patch.object(
            to.aws,
            "describe_load_balancers_by_lab",
            return_value=[
                {
                    "name": "ops-agent-iac-dev-alb",
                    "dns_name": "ops-agent-iac-dev-alb-1.ap-northeast-2.elb.amazonaws.com",
                    "arn": "arn:lb/1",
                    "state": "active",
                    "scheme": "internet-facing",
                }
            ],
        ),
        mock.patch.object(to.aws, "describe_security_groups_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_db_instances_by_lab", return_value=[]),
    ):
        out = _parse(to.aws_describe_service({}))
    assert out["success"] is True
    assert out["load_balancers"][0]["dns_name"].endswith("elb.amazonaws.com")
    # the dns_name is the CNAME content for the <env>-dns surface — keep the hint
    assert "dns" in out["note"].lower()


def test_aws_describe_service_exposes_sg_ingress_for_self_verify():
    # After an ec2_ssh_allowlist change, the agent self-verifies the CIDR:22 rule
    # landed via security_groups[].ingress — not just "EC2 running".
    with (
        mock.patch.object(to.aws, "check_aws", return_value=True),
        mock.patch.object(to.aws, "describe_instances_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_bastions_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_volumes_by_lab", return_value=[]),
        mock.patch.object(to.aws, "resolve_target_group_arns_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_load_balancers_by_lab", return_value=[]),
        mock.patch.object(
            to.aws,
            "describe_security_groups_by_lab",
            return_value=[
                {
                    "id": "sg-01",
                    "name": "ops-agent-iac-dev-bastion",
                    "ingress": [
                        {
                            "protocol": "tcp",
                            "from_port": 22,
                            "to_port": 22,
                            "cidr": "203.0.113.10/32",
                        }
                    ],
                }
            ],
        ),
        mock.patch.object(to.aws, "describe_db_instances_by_lab", return_value=[]),
    ):
        out = _parse(to.aws_describe_service({}))
    assert out["success"] is True
    rule = out["security_groups"][0]["ingress"][0]
    assert rule["from_port"] == 22 and rule["cidr"] == "203.0.113.10/32"


def test_aws_describe_service_exposes_rds_instances():
    # C4-I1: ops_aws_get_service must surface RDS so "does the DB endpoint exist?"
    # is answerable via a live read instead of an honest "can't confirm".
    with (
        mock.patch.object(to.aws, "check_aws", return_value=True),
        mock.patch.object(to.aws, "describe_instances_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_bastions_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_volumes_by_lab", return_value=[]),
        mock.patch.object(to.aws, "resolve_target_group_arns_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_load_balancers_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_security_groups_by_lab", return_value=[]),
        mock.patch.object(
            to.aws,
            "describe_db_instances_by_lab",
            return_value=[
                {
                    "id": "ops-agent-iac-prod",
                    "status": "available",
                    "engine": "postgres",
                    "instance_class": "db.t3.micro",
                    "endpoint": "ops-agent-iac-prod.abc.ap-northeast-2.rds.amazonaws.com",
                    "port": 5432,
                    "multi_az": False,
                    "publicly_accessible": False,
                }
            ],
        ),
    ):
        out = _parse(to.aws_describe_service({}))
    assert out["success"] is True
    assert out["rds_instances"][0]["endpoint"].endswith("rds.amazonaws.com")
    assert out["rds_instances"][0]["status"] == "available"
    assert "rds" in out["note"].lower()


def test_aws_describe_service_exposes_bastion_public_ip():
    # RDS 접근 안내의 SSH 터널 명령은 bastion public IP 실값으로 완성돼야 한다 —
    # read 응답에 없으면 에이전트가 <bastion_public_ip> placeholder를 남긴다.
    with (
        mock.patch.object(to.aws, "check_aws", return_value=True),
        mock.patch.object(to.aws, "describe_instances_by_lab", return_value=[]),
        mock.patch.object(
            to.aws,
            "describe_bastions_by_lab",
            return_value=[
                {
                    "id": "i-0b",
                    "name": "ops-agent-iac-dev-bastion",
                    "type": "t3.micro",
                    "state": "running",
                    "public_ip": "1.2.3.4",
                }
            ],
        ),
        mock.patch.object(to.aws, "describe_volumes_by_lab", return_value=[]),
        mock.patch.object(to.aws, "resolve_target_group_arns_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_load_balancers_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_security_groups_by_lab", return_value=[]),
        mock.patch.object(to.aws, "describe_db_instances_by_lab", return_value=[]),
    ):
        out = _parse(to.aws_describe_service({}))
    assert out["success"] is True
    assert out["bastion_instances"][0]["public_ip"] == "1.2.3.4"
    assert "bastion" in out["evidence"][0]["lookup"]
    assert "bastion_instances" in out["note"]


def test_cost_summary_validates_group_by():
    with mock.patch.object(to.aws, "check_aws", return_value=True):
        out = _parse(to.aws_get_cost_summary({"group_by": "LINKED_ACCOUNT"}))
    assert out["success"] is False
    assert "group_by" in out["error"]


def test_find_unused_candidates_is_report_only():
    with (
        mock.patch.object(to.aws, "check_aws", return_value=True),
        mock.patch.object(
            to.aws,
            "find_unused_candidates",
            return_value={"unattached_ebs": [], "unassociated_eip": []},
        ),
    ):
        out = _parse(to.aws_find_unused_candidates({}))
    assert out["success"] is True
    assert "REPORT ONLY" in out["note"]


def test_query_logs_defaults_to_app_5xx_named_query():
    seen = {}

    def _fake_query_logs(qname, labels, rng):
        seen["qname"], seen["labels"], seen["rng"] = qname, labels, rng
        return {"query_name": qname, "logql": "x", "result": []}

    with (
        mock.patch.object(to.grafana, "check_grafana", return_value=True),
        mock.patch.object(to.grafana, "query_logs", side_effect=_fake_query_logs),
    ):
        # 앱 로그는 job="app" 고정 스트림 — 기본 5xx 쿼리는 라벨이 없다.
        out = _parse(to.query_logs({}))
        assert out["success"] is True
        assert seen["qname"] == "app_5xx"
        assert seen["labels"] == {}

        out = _parse(to.query_logs({"pattern": "timeout"}))
        assert out["success"] is True
        assert seen["qname"] == "service_pattern"
        assert seen["labels"]["pattern"] == "timeout"


def test_query_metrics_caps_range_hours():
    seen = {}

    def _fake_query_metrics(qname, labels, rng):
        seen["rng"] = rng
        return {"query_name": qname, "promql": "x", "dashboard": "", "result": []}

    with (
        mock.patch.object(to.grafana, "check_grafana", return_value=True),
        mock.patch.object(to.grafana, "query_metrics", side_effect=_fake_query_metrics),
    ):
        out = _parse(
            to.query_metrics(
                {
                    "query_name": "cpu_utilization",
                    "labels": {"service": "demo-app"},
                    "range_hours": 999,
                }
            )
        )
    assert out["success"] is True
    assert seen["rng"] == 24


def test_explain_alert_matches_rule_by_name():
    rules = [
        {
            "name": "lab-cpu-high",
            "state": "firing",
            "labels": {},
            "annotations": {},
            "alerts": [],
        },
        {
            "name": "lab-disk-high",
            "state": "inactive",
            "labels": {},
            "annotations": {},
            "alerts": [],
        },
    ]
    with (
        mock.patch.object(to.grafana, "check_grafana", return_value=True),
        mock.patch.object(to.grafana, "list_alert_rules", return_value=rules),
    ):
        out = _parse(to.explain_alert({"alert_id": "cpu"}))
        assert out["success"] is True
        assert out["rules"][0]["name"] == "lab-cpu-high"

        out = _parse(to.explain_alert({"alert_id": "does-not-exist"}))
        assert out["success"] is False
        assert "lab-cpu-high" in out.get("remediation", "")


def test_explain_alert_matches_rule_by_uid():
    # 알람 webhook payload는 룰을 UID로 지칭한다 — list_alert_rules 요약에 uid가
    # 빠지면 매 알람마다 이 매칭이 실패한다 (F1 회귀 방지).
    rules = [
        {
            "name": "[monitoring] instance scrape down (up==0)",
            "uid": "bfs9g5iio20owb",
            "state": "firing",
            "labels": {},
            "annotations": {},
            "alerts": [],
        },
    ]
    with (
        mock.patch.object(to.grafana, "check_grafana", return_value=True),
        mock.patch.object(to.grafana, "list_alert_rules", return_value=rules),
    ):
        out = _parse(to.explain_alert({"alert_id": "bfs9g5iio20owb"}))
        assert out["success"] is True
        assert out["rules"][0]["uid"] == "bfs9g5iio20owb"


def test_list_alert_rules_summary_includes_uid():
    payload = {
        "data": {
            "groups": [
                {
                    "rules": [
                        {
                            "name": "r1",
                            "uid": "u-1",
                            "state": "inactive",
                            "labels": {},
                            "annotations": {},
                            "alerts": [],
                        }
                    ]
                }
            ]
        }
    }

    class _R:
        status_code = 200
        text = ""

        def json(self):
            return payload

    with (
        mock.patch.object(to.grafana.settings, "grafana_url", return_value="http://g"),
        mock.patch.object(to.grafana, "http_request", return_value=_R()),
    ):
        rules = to.grafana.list_alert_rules()
    assert rules[0]["uid"] == "u-1"


def test_check_pr_status_reads_mocked_pr():
    with (
        mock.patch.object(to.github, "check_github", return_value=True),
        mock.patch.object(
            to.github,
            "get_pr",
            return_value={
                "state": "open",
                "merged": False,
                "mergeable": True,
                "title": "t",
                "head": {"sha": "head123"},
                "requested_reviewers": [{"login": "owner1"}],
                "requested_teams": [{"slug": "infra-team"}],
            },
        ),
        # 리뷰 제출 시 requested_reviewers에서 빠지므로 제출된 리뷰도 함께 노출한다.
        mock.patch.object(
            to.github,
            "get_pr_reviews",
            return_value=[{"user": {"login": "owner0"}, "state": "APPROVED"}],
        ),
        # guard/plan 은 tf-plan(pull_request) run 의 job 으로 조회한다. push 이벤트
        # run(apply)은 head SHA 에 걸려도 제외돼야 한다.
        mock.patch.object(
            to.github,
            "get_workflow_runs_for_head",
            return_value=[
                {"name": "tf-plan", "event": "pull_request", "id": 111},
                {"name": "tf-apply", "event": "push", "id": 999},
            ],
        ),
        mock.patch.object(
            to.github,
            "get_workflow_jobs",
            return_value=[
                {"name": "guard", "status": "completed", "conclusion": "success"},
                {
                    "name": "plan (2-1-dev)",
                    "status": "completed",
                    "conclusion": "success",
                },
            ],
        ),
    ):
        out = _parse(
            to.check_pr_status(
                {"pr_url": "https://github.com/acme/ops-agent-iac/pull/12"}
            )
        )
    assert out["success"] is True
    assert out["state"] == "open"
    assert out["merged"] is False
    # code owner가 리뷰어로 실제 지정됐는지 에이전트가 판별할 수 있어야 한다
    # (둘 다 빈 목록 = CODEOWNERS 무효/미반영 — "승인 대기"로 보고하면 안 되는 상태).
    assert out["review_requests"] == ["owner1", "infra-team"]
    assert out["reviews"] == [{"user": "owner0", "state": "APPROVED"}]
    assert out["reviews_lookup_error"] is None
    # guard/plan 이 에이전트에 노출되고, push run 은 제외된다 (C1-I4).
    assert out["checks"] == [
        {"name": "guard", "status": "completed", "conclusion": "success"},
        {"name": "plan (2-1-dev)", "status": "completed", "conclusion": "success"},
    ]
    assert out["checks_lookup_error"] is None
    assert out["apply_runs"] == []
    assert out["apply_runs_lookup_error"] is None


def test_check_pr_status_preserves_pr_when_check_runs_lookup_fails():
    with (
        mock.patch.object(to.github, "check_github", return_value=True),
        mock.patch.object(
            to.github,
            "get_pr",
            return_value={
                "state": "open",
                "merged": False,
                "mergeable": True,
                "title": "t",
                "head": {"sha": "head123"},
            },
        ),
        mock.patch.object(to.github, "get_pr_reviews", return_value=[]),
        mock.patch.object(
            to.github,
            "get_workflow_runs_for_head",
            side_effect=RuntimeError("jobs API 503"),
        ),
    ):
        out = _parse(
            to.check_pr_status(
                {"pr_url": "https://github.com/acme/ops-agent-iac/pull/12"}
            )
        )
    assert out["success"] is True
    assert out["state"] == "open"
    assert out["checks"] == []
    assert "jobs API 503" in out["checks_lookup_error"]


def test_check_pr_status_preserves_pr_when_reviews_lookup_fails():
    # reviews API 일시 오류로 "리뷰어 미지정" 오판정을 내리면 안 된다 —
    # 이미 얻은 PR 상태는 보존하고 오류는 별도 필드로 노출한다.
    with (
        mock.patch.object(to.github, "check_github", return_value=True),
        mock.patch.object(
            to.github,
            "get_pr",
            return_value={
                "state": "open",
                "merged": False,
                "mergeable": True,
                "title": "t",
            },
        ),
        mock.patch.object(
            to.github,
            "get_pr_reviews",
            side_effect=RuntimeError("reviews API 503"),
        ),
    ):
        out = _parse(
            to.check_pr_status(
                {"pr_url": "https://github.com/acme/ops-agent-iac/pull/12"}
            )
        )
    assert out["success"] is True
    assert out["state"] == "open"
    assert out["review_requests"] == []
    assert out["reviews"] == []
    assert "reviews API 503" in out["reviews_lookup_error"]


def test_check_pr_status_returns_apply_run_for_merged_pr():
    with (
        mock.patch.object(to.github, "check_github", return_value=True),
        mock.patch.object(
            to.github,
            "get_pr",
            return_value={
                "state": "closed",
                "merged": True,
                "merge_commit_sha": "abc123",
                "title": "t",
            },
        ),
        mock.patch.object(
            to.github,
            "get_workflow_runs_for_head",
            return_value=[
                {
                    "name": "tf-plan",
                    "html_url": "https://github.com/acme/repo/actions/runs/1",
                },
                {
                    "name": "tf-apply",
                    "html_url": "https://github.com/acme/repo/actions/runs/push",
                    "status": "completed",
                    "conclusion": "success",
                    "event": "push",
                    "created_at": "2026-07-16T00:00:00Z",
                },
                {
                    "name": "tf-apply",
                    "html_url": "https://github.com/acme/repo/actions/runs/2",
                    "status": "completed",
                    "conclusion": "success",
                    "event": "workflow_dispatch",
                    "created_at": "2026-07-16T00:00:00Z",
                },
            ],
        ),
    ):
        out = _parse(
            to.check_pr_status(
                {"pr_url": "https://github.com/acme/ops-agent-iac/pull/12"}
            )
        )
    # merge SHA에 걸린 tf-apply run은 dispatch(dev auto-merge)든 push(사람/admin
    # 머지 — prod 경로)든 모두 이 PR의 apply다 (2026-07-16 e2e C1-I4).
    assert out["apply_runs"] == [
        {
            "run_url": "https://github.com/acme/repo/actions/runs/push",
            "status": "completed",
            "conclusion": "success",
            "event": "push",
            "created_at": "2026-07-16T00:00:00Z",
        },
        {
            "run_url": "https://github.com/acme/repo/actions/runs/2",
            "status": "completed",
            "conclusion": "success",
            "event": "workflow_dispatch",
            "created_at": "2026-07-16T00:00:00Z",
        },
    ]
    assert out["apply_runs_lookup_error"] is None


def test_check_pr_status_preserves_merged_result_when_apply_lookup_fails():
    with (
        mock.patch.object(to.github, "check_github", return_value=True),
        mock.patch.object(
            to.github,
            "get_pr",
            return_value={
                "state": "closed",
                "merged": True,
                "merge_commit_sha": "abc123",
                "title": "t",
            },
        ),
        mock.patch.object(
            to.github,
            "get_workflow_runs_for_head",
            side_effect=RuntimeError("GitHub API timeout"),
        ),
    ):
        out = _parse(
            to.check_pr_status(
                {"pr_url": "https://github.com/acme/ops-agent-iac/pull/12"}
            )
        )
    assert out["success"] is True
    assert out["merged"] is True
    assert out["apply_runs"] == []
    assert "GitHub API timeout" in out["apply_runs_lookup_error"]


def test_check_workflow_run_reads_mocked_run():
    with (
        mock.patch.object(to.github, "check_github", return_value=True),
        mock.patch.object(
            to.github,
            "get_workflow_run",
            return_value={
                "status": "completed",
                "conclusion": "success",
                "name": "tf-apply",
                "head_sha": "a7f334abcdef",
                "head_branch": "dev",
            },
        ),
    ):
        out = _parse(
            to.check_workflow_run(
                {"run_url": "https://github.com/acme/ops-agent-iac/actions/runs/42"}
            )
        )
    assert out["success"] is True
    assert out["conclusion"] == "success"
    # head_sha lets the agent confirm the run ran on the merge commit (P4 stale-run guard)
    assert out["head_sha"] == "a7f334abcdef"


def test_client_exception_becomes_error_json_not_raise():
    with (
        mock.patch.object(to.aws, "check_aws", return_value=True),
        mock.patch.object(
            to.aws, "describe_instances_by_lab", side_effect=RuntimeError("boom")
        ),
        mock.patch.object(to.aws, "describe_volumes_by_lab", return_value=[]),
        mock.patch.object(to.aws, "resolve_target_group_arns_by_lab", return_value=[]),
    ):
        out = _parse(to.aws_describe_service({}))
    assert out["success"] is False
    assert "boom" in out["error"]
