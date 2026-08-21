"""클라이언트 경계 + named-query 카탈로그 테스트. 실제 credential도, boto3/httpx도 없다."""

from datetime import date
from unittest import mock

import pytest

import ops_plugin.clients.aws as aws
import ops_plugin.clients.github as github
import ops_plugin.clients.grafana as grafana
import ops_plugin.settings as settings


# --- AWS 읽기 경계 -----------------------------------------------------------------
def test_check_aws_false_without_read_role_env():
    # conftest는 OPS_AWS_READ_ROLE을 제거한다: boto3가 설치되어 있어도 게이트는
    # 닫힌 채로 유지되어야 한다 — 플러그인은 로컬 raw credential로는 절대 AWS를 호출하지 않는다.
    assert settings.aws_read_role_arn() is None
    assert aws.check_aws() is False


def test_client_refuses_missing_role_arn_before_importing_boto3():
    # read 롤 ARN 없이는 boto3 import 전에 깔끔한 설정 오류가 난다
    with pytest.raises(RuntimeError, match="OPS_AWS_READ_ROLE"):
        aws._client("ec2")


def test_describe_instances_scopes_on_name_tag():
    fake_page = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-1",
                        "InstanceType": "t3.micro",
                        "State": {"Name": "running"},
                        "Placement": {"AvailabilityZone": "apne2-az1"},
                        "PublicIpAddress": "198.51.100.10",
                        "PublicDnsName": "ec2-198-51-100-10.example.compute.amazonaws.com",
                        "Tags": [
                            {"Key": "Name", "Value": "ops-agent-iac-dev-app-0"},
                            {"Key": "AppVersion", "Value": "v6"},
                        ],
                    }
                ]
            }
        ]
    }
    fake_ec2 = mock.Mock()
    fake_ec2.get_paginator.return_value.paginate.return_value = [fake_page]
    with mock.patch.object(aws, "_client", return_value=fake_ec2):
        out = aws.describe_instances_by_lab("ops-agent-iac")
    assert out[0]["id"] == "i-1"
    assert out[0]["public_ip"] == "198.51.100.10"
    assert out[0]["public_dns"] == "ec2-198-51-100-10.example.compute.amazonaws.com"
    # Deployed release must be observable from live AWS metadata. Without this
    # field the status runbook substitutes launch time for the requested version.
    assert out[0]["app_version"] == "v6"
    kwargs = fake_ec2.get_paginator.return_value.paginate.call_args.kwargs
    # Fleet selection anchors on tag:Role=app — the selector iac documents and
    # ansible/Prometheus already use — so an iac rename of <name>-app-<n> cannot
    # silently empty the fleet (the original "*-app" glob bug). Name only scopes
    # the project.
    assert kwargs["Filters"] == [
        {"Name": "tag:Role", "Values": ["app"]},
        {"Name": "tag:Name", "Values": ["ops-agent-iac-*"]},
    ]


def test_cost_summary_includes_calendar_month_run_rate():
    fake_ce = mock.Mock()
    fake_ce.get_cost_and_usage.side_effect = [
        {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                            "Metrics": {"AmortizedCost": {"Amount": "3.00"}},
                        }
                    ]
                }
            ]
        },
        {"ResultsByTime": [{"Total": {"AmortizedCost": {"Amount": "4.00"}}}]},
    ]

    with (
        mock.patch.object(aws, "_client", return_value=fake_ce),
        mock.patch.object(aws, "_today", return_value=date(2026, 8, 20)),
    ):
        out = aws.cost_summary(30)

    assert out["current_month_estimate"] == {
        "month": "2026-08",
        "actual_through": "2026-08-19",
        "month_to_date": 4.0,
        "projected_total": 6.53,
        "basis": "19 completed days run-rate",
        "days_elapsed": 19,
        "days_in_month": 31,
    }
    trailing_call, month_call = fake_ce.get_cost_and_usage.call_args_list
    assert trailing_call.kwargs["TimePeriod"] == {
        "Start": "2026-07-21",
        "End": "2026-08-20",
    }
    assert month_call.kwargs["TimePeriod"] == {
        "Start": "2026-08-01",
        "End": "2026-08-20",
    }
    assert "GroupBy" not in month_call.kwargs


def test_describe_volumes_scopes_on_data_volume_name_tag():
    fake_ec2 = mock.Mock()
    fake_ec2.describe_volumes.return_value = {
        "Volumes": [
            {
                "VolumeId": "vol-1",
                "Size": 10,
                "VolumeType": "gp3",
                "State": "in-use",
                "Attachments": [{"InstanceId": "i-1"}],
                "Tags": [{"Key": "Name", "Value": "ops-agent-iac-dev-data-0"}],
            }
        ]
    }
    with mock.patch.object(aws, "_client", return_value=fake_ec2):
        out = aws.describe_volumes_by_lab("ops-agent-iac")
    assert out[0]["id"] == "vol-1"
    # the Name tag is surfaced so mixed dev/prod results are self-describing
    assert out[0]["name"] == "ops-agent-iac-dev-data-0"
    # data volumes are named <name>-data-<n>, not <name>-*-app. The glob keeps the
    # segment before `data` optional (`-*data-*`) so an env-scoped prefix
    # (ops-agent-iac-prod) matches ops-agent-iac-prod-data-0 with an empty `*`.
    assert fake_ec2.describe_volumes.call_args.kwargs["Filters"] == [
        {"Name": "tag:Name", "Values": ["ops-agent-iac-*data-*"]}
    ]


def test_find_unused_candidates_reconfirms_iam_roles_via_get_role():
    # list_roles 는 RoleLastUsed 를 설계상 채우지 않는다(항상 빈 값). 그래서 예전
    # 구현은 프리픽스에 걸리는 실사용 롤(OIDC 배포 롤 등)까지 전부 미사용 후보로
    # 올렸다. 후보는 롤마다 get_role 로 재확인되어야 하고, 콘솔의 "Last activity"에
    # 대응하는 LastUsedDate 가 있으면 후보에서 빠져야 한다.
    from datetime import datetime

    fake_ec2 = mock.Mock()
    fake_ec2.describe_volumes.return_value = {"Volumes": []}
    fake_ec2.describe_addresses.return_value = {"Addresses": []}
    fake_ec2.get_paginator.return_value.paginate.return_value = [
        {"NetworkInterfaces": []}
    ]
    fake_ec2.describe_security_groups.return_value = {"SecurityGroups": []}
    fake_ec2.describe_snapshots.return_value = {"Snapshots": []}

    fake_iam = mock.Mock()
    # list_roles 는 RoleLastUsed 없이 이름만 준다 (실제 API 동작).
    fake_iam.get_paginator.return_value.paginate.return_value = [
        {
            "Roles": [
                {"RoleName": "ops-agent-iac-github-oidc"},
                {"RoleName": "ops-agent-iac-stale"},
                {"RoleName": "unrelated-role"},  # 프리픽스 밖 → 조회조차 안 함
            ]
        }
    ]

    def _fake_get_role(RoleName):
        used = {
            # 실사용 롤: get_role 은 콘솔과 같은 LastUsedDate 를 준다 → 후보 제외.
            "ops-agent-iac-github-oidc": {
                "RoleName": RoleName,
                "CreateDate": datetime(2026, 1, 1),
                "RoleLastUsed": {"LastUsedDate": datetime(2026, 8, 20)},
            },
            # 진짜 미사용: get_role 로도 LastUsedDate 없음 → 후보 유지.
            "ops-agent-iac-stale": {
                "RoleName": RoleName,
                "CreateDate": datetime(2026, 1, 1),
                "RoleLastUsed": {},
            },
        }
        return {"Role": used[RoleName]}

    fake_iam.get_role.side_effect = _fake_get_role

    def _fake_client(service, region=None):
        return fake_iam if service == "iam" else fake_ec2

    with mock.patch.object(aws, "_client", side_effect=_fake_client):
        out = aws.find_unused_candidates()

    names = [r["name"] for r in out["unused_iam_roles"]]
    assert names == ["ops-agent-iac-stale"]
    # 프리픽스 밖 롤은 get_role 조차 호출하지 않는다.
    called = {c.kwargs["RoleName"] for c in fake_iam.get_role.call_args_list}
    assert called == {"ops-agent-iac-github-oidc", "ops-agent-iac-stale"}


def test_describe_db_instances_includes_env_scoped_prefix():
    # RDS ids are <project>-<env> with no further suffix (ops-agent-iac-dev /
    # ops-agent-iac-prod), unlike EC2/ALB/SG which carry a <...>-app-0/-* suffix.
    # When the agent scopes to one env by passing prefix=<project>-<env>, the id
    # equals the prefix exactly — a bare startswith(prefix + "-") would drop it and
    # the tool would falsely report "no DB for this env" while still listing that
    # env's EC2. Exercise the real filter (mock only the boto3 paginator, not the
    # function) so this asymmetry can't regress. C4-I1's test mocked the whole
    # function's return_value and so never ran the filter — that gap is why the bug
    # shipped.
    fake_page = {
        "DBInstances": [
            {
                "DBInstanceIdentifier": "ops-agent-iac-dev",
                "DBInstanceStatus": "available",
                "Engine": "postgres",
                "DBInstanceClass": "db.t3.micro",
                "Endpoint": {"Address": "dev.rds.example", "Port": 5432},
                "MultiAZ": False,
                "PubliclyAccessible": False,
            },
            {
                "DBInstanceIdentifier": "ops-agent-iac-prod",
                "DBInstanceStatus": "available",
                "Engine": "postgres",
                "DBInstanceClass": "db.t3.micro",
                "Endpoint": {"Address": "prod.rds.example", "Port": 5432},
                "MultiAZ": False,
                "PubliclyAccessible": False,
            },
        ]
    }
    fake_rds = mock.Mock()
    fake_rds.get_paginator.return_value.paginate.return_value = [fake_page]
    with mock.patch.object(aws, "_client", return_value=fake_rds):
        # env-scoped prefix equals the id — must still be found
        prod = aws.describe_db_instances_by_lab("ops-agent-iac-prod")
        assert [d["id"] for d in prod] == ["ops-agent-iac-prod"]
        dev = aws.describe_db_instances_by_lab("ops-agent-iac-dev")
        assert [d["id"] for d in dev] == ["ops-agent-iac-dev"]
        # project-only prefix spans both envs (unchanged behaviour)
        both = aws.describe_db_instances_by_lab("ops-agent-iac")
        assert {d["id"] for d in both} == {"ops-agent-iac-dev", "ops-agent-iac-prod"}


def test_filters_match_iac_tagging_contract():
    # Contract with ops-agent-iac modules/service/main.tf: app instances carry
    # Role=app (the selector ansible/Prometheus also use); data volumes have no
    # Role tag and are named <name>-data-<n>. fnmatch mirrors EC2 filter glob
    # semantics (* = zero or more chars) — the original "*-app" Name glob failed
    # exactly this check and silently returned an empty fleet.
    from fnmatch import fnmatch

    app_filters = aws._app_filter("ops-agent-iac")
    assert {"Name": "tag:Role", "Values": ["app"]} in app_filters
    scope_glob = next(f["Values"][0] for f in app_filters if f["Name"] == "tag:Name")
    for name in ("ops-agent-iac-dev-app-0", "ops-agent-iac-prod-app-1"):
        assert fnmatch(name, scope_glob), f"{scope_glob} must match {name}"

    data_glob = aws._data_volume_filter("ops-agent-iac")[0]["Values"][0]
    assert fnmatch("ops-agent-iac-dev-data-1", data_glob)
    assert not fnmatch("ops-agent-iac-dev-app-0", data_glob)
    assert not fnmatch("ops-agent-iac-seed-vol-keep", data_glob)

    # C8-I1 regression: the agent scopes ops_aws_get_service to one env by passing
    # prefix=<project>-<env> (e.g. ops-agent-iac-prod). The volume Name is
    # ops-agent-iac-prod-data-0 with NO segment between prefix and `data`, so the
    # old `<prefix>-*-data-*` glob forced a nonexistent segment and returned 0
    # volumes → the agent falsely reported "no /data disk / N/A". `-*data-*` matches
    # with an empty `*`. Exercise both env prefixes.
    for env_prefix in ("ops-agent-iac-dev", "ops-agent-iac-prod"):
        env_glob = aws._data_volume_filter(env_prefix)[0]["Values"][0]
        assert fnmatch(f"{env_prefix}-data-0", env_glob), (
            f"{env_glob} must match {env_prefix}-data-0"
        )
        assert not fnmatch(f"{env_prefix}-app-0", env_glob)


def test_keep_tag_is_case_insensitive():
    assert aws._has_keep_tag([{"Key": "Keep", "Value": "TRUE"}]) is True
    assert aws._has_keep_tag([{"Key": "keep", "Value": "no"}]) is False
    assert aws._has_keep_tag(None) is False


# --- service 허용 목록 -------------------------------------------------------------------


# --- Grafana 쿼리 인젝션 방어 --------------------------------------------------------
def test_substitute_fills_only_allowed_labels():
    q = grafana._substitute(
        'up{service="${service}", other="${other}"}',
        {"service": "demo-app", "other": "x"},
        ["service"],
    )
    assert 'service="demo-app"' in q
    assert "${other}" in q  # 허용되지 않은 label은 절대 치환되지 않는다


def test_substitute_rejects_injection_characters():
    for bad in ('a"b', "a{b", "a}b", "a\nb", "a\\b", "a'b"):
        with pytest.raises(grafana.GrafanaError):
            grafana._substitute(
                'x{service="${service}"}', {"service": bad}, ["service"]
            )


def test_check_grafana_false_without_env():
    assert grafana.check_grafana() is False


def test_grafana_public_url_falls_back_to_private(monkeypatch):
    monkeypatch.setenv("OPS_GRAFANA_URL", "http://10.0.0.1:3000")
    assert settings.grafana_public_url() == "http://10.0.0.1:3000"
    monkeypatch.setenv("OPS_GRAFANA_PUBLIC_URL", "http://3.3.3.3:3000")
    assert settings.grafana_public_url() == "http://3.3.3.3:3000"


# --- GitHub 게이트 ---------------------------------------------------------------------------
def test_check_github_false_without_token():
    assert github.check_github() is False


# --- named-query 카탈로그 (런타임과 마찬가지로 pyyaml 필요) -----------------------------------
def test_catalog_has_the_designed_queries():
    pytest.importorskip("yaml")
    cat = settings.named_queries()
    assert set(cat["metrics"]) == {
        "cpu_utilization",
        "memory_used_pct",
        "disk_usage_pct",
    }
    assert set(cat["logs"]) == {"app_5xx", "service_pattern"}
    # 선언된 모든 placeholder는 허용된 label로 뒷받침된다
    import re

    for section in ("metrics", "logs"):
        for name, q in cat[section].items():
            template = q.get("promql") or q.get("logql")
            placeholders = set(re.findall(r"\$\{(\w+)\}", template))
            assert placeholders == set(q.get("labels", [])), name
    lim = cat["limits"]
    assert lim["max_range_hours"] == 24
    assert lim["max_log_lines"] <= 200


def test_unknown_named_query_raises_with_allowed_list():
    pytest.importorskip("yaml")
    with pytest.raises(grafana.GrafanaError) as ei:
        grafana._named("metrics", "free_form_promql")
    assert "cpu_utilization" in str(ei.value)
