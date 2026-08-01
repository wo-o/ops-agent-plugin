"""등록(registration) + audit-hook 계약 테스트. 프레임워크도, credential도 없다."""

import json
import os
from pathlib import Path

import ops_plugin
import ops_plugin.audit as audit

EXPECTED_TOOLS = {
    "ops_get_service_health",
    "ops_explain_alert",
    "ops_query_metrics",
    "ops_query_logs",
    "ops_aws_get_service",
    "ops_aws_get_alb_target_health",
    "ops_aws_get_cost_summary",
    "ops_aws_find_unused_candidates",
    "ops_github_get_pr_status",
    "ops_github_get_workflow_run",
    "ops_pagerduty_list_incidents",
    "ops_pagerduty_get_oncall",
    "ops_cloudflare_list_dns_records",
    "ops_cloudflare_list_waf_rules",
    "ops_cloudflare_get_analytics",
    "ops_github_open_tfvars_pr",
    "ops_github_read_file",
    "ops_github_open_code_pr",
    "ops_github_open_promotion_pr",
    "ops_run_ansible_playbook",
    "ops_grafana_silence",
    "ops_pagerduty_manage_incident",
    "ops_pagerduty_page_oncall",
}


class FakeCtx:
    def __init__(self):
        self.tools = []
        self.hooks = []
        self.skills = []

    def register_tool(self, **kw):
        self.tools.append(kw)

    def register_hook(self, name, cb):
        self.hooks.append((name, cb))

    def register_skill(self, name, path, description=None):
        self.skills.append((name, path, description))


def test_register_wires_exactly_the_manifest_tools():
    ctx = FakeCtx()
    ops_plugin.register(ctx)
    names = {t["name"] for t in ctx.tools}
    assert names == EXPECTED_TOOLS
    assert len(ctx.tools) == 23
    # toolset: read 하나 + write 셋
    assert {t["toolset"] for t in ctx.tools} == {
        "ops-read",
        "ops-github-write",
        "ops-ansible-write",
        "ops-monitoring-write",
    }
    assert {t["name"] for t in ctx.tools if t["toolset"] == "ops-github-write"} == {
        "ops_github_open_tfvars_pr",
        "ops_github_open_code_pr",
        "ops_github_open_promotion_pr",
    }
    # 모든 도구는 name이 등록된 이름과 일치하는 schema를 함께 제공한다
    for t in ctx.tools:
        assert t["schema"]["name"] == t["name"]
        assert callable(t["handler"])
        assert callable(t["check_fn"])  # 모든 도구는 credential로 게이팅된다


def test_register_wires_only_the_audit_hook_and_the_skill():
    ctx = FakeCtx()
    ops_plugin.register(ctx)
    assert [name for name, _ in ctx.hooks] == ["post_tool_call"]
    assert ctx.hooks[0][1] is audit.record_tool_call
    assert [s[0] for s in ctx.skills] == [
        "ops-operating",
        "ops-change",
        "ops-incident-response",
        "ops-incident-rca",
    ]
    for s in ctx.skills:
        assert Path(s[1]).exists()


def test_manifest_lists_the_same_tools():
    # plugin.yaml은 메타데이터 전용이지만, register()와 어긋나(drift) 있으면 안 된다
    manifest = (Path(ops_plugin.__file__).parent / "plugin.yaml").read_text(
        encoding="utf-8"
    )
    for name in EXPECTED_TOOLS:
        assert name in manifest


# --- audit hook (추가 전용 JSONL) ---------------------------------------------------
def _audit_file() -> Path:
    return Path(os.environ["OPS_STATE_DIR"]) / "audit.jsonl"


def test_record_tool_call_appends_jsonl_and_records_args():
    before = _audit_file().read_text().splitlines() if _audit_file().exists() else []
    audit.record_tool_call(
        tool_name="ops_query_metrics",
        args={
            "query_name": "cpu_utilization",
            "labels": {"service": "demo-app"},
        },
        duration_ms=12,
        status="ok",
        session_id="sess-1",
    )
    lines = _audit_file().read_text().splitlines()
    assert len(lines) == len(before) + 1
    rec = json.loads(lines[-1])
    assert rec["tool"] == "ops_query_metrics"
    assert rec["status"] == "ok"
    assert rec["correlation_id"] == "sess-1"
    # args는 원문으로 기록된다 (시크릿은 설계상 args로 전달되지 않는다)
    assert rec["args"] == {
        "query_name": "cpu_utilization",
        "labels": {"service": "demo-app"},
    }


def test_record_tool_call_truncates_long_string_values():
    audit.record_tool_call(tool_name="t", args={"blob": "x" * 600})
    rec = json.loads(_audit_file().read_text().splitlines()[-1])
    assert rec["args"]["blob"].startswith("x" * 500)
    assert "truncated" in rec["args"]["blob"]
    assert len(rec["args"]["blob"]) < 600


def test_record_tool_call_never_raises_on_weird_payloads():
    audit.record_tool_call()  # kwargs가 전혀 없음
    audit.record_tool_call(args=object(), error=RuntimeError("x"))
    rec = json.loads(_audit_file().read_text().splitlines()[-1])
    assert rec["status"] == "error"
