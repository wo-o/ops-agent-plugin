"""PagerDuty / Cloudflare read 도구 계약 테스트. 실제 credential도 httpx도 필요 없다.

test_handlers.py와 같은 계약을 검증한다: JSON 문자열 반환, {"success": bool} envelope,
credential 없으면 remediation을 담은 깔끔한 에러, 잘못된 파라미터는 raise가 아니라 fail.
클라이언트 매핑은 _get을 mock해서 API 페이로드 -> 요약 스키마 변환만 검증한다.
"""

import json
from unittest import mock

import ops_plugin.clients.cloudflare as cf
import ops_plugin.clients.pagerduty as pd
import ops_plugin.tools_observability as to


def _parse(s):
    assert isinstance(s, str), f"handler must return a JSON string, got {type(s)}"
    return json.loads(s)


# --- credential degradation (conftest가 모든 credential을 제거) ----------------------
def test_pagerduty_tools_fail_cleanly_without_token():
    for handler in (to.pagerduty_list_incidents, to.pagerduty_get_oncall):
        out = _parse(handler({}))
        assert out["success"] is False
        assert "OPS_PAGERDUTY_TOKEN" in out.get("remediation", "")


def test_cloudflare_tools_fail_cleanly_without_token_and_zone():
    for handler in (
        to.cloudflare_list_dns_records,
        to.cloudflare_list_waf_rules,
        to.cloudflare_get_analytics,
    ):
        out = _parse(handler({}))
        assert out["success"] is False
        assert "OPS_CLOUDFLARE_READ_TOKEN" in out.get("remediation", "")


def test_check_fns_are_false_without_credentials():
    assert to.check_pagerduty_requirements() is False
    assert to.check_cloudflare_requirements() is False


# --- 파라미터 검증 (credential이 있어도 잘못된 입력은 fail) --------------------------
def test_list_incidents_rejects_bad_status():
    with mock.patch.object(pd, "check_pagerduty", return_value=True):
        out = _parse(to.pagerduty_list_incidents({"status": "exploded"}))
    assert out["success"] is False
    assert "invalid status" in out["error"]


def test_list_dns_records_rejects_bad_type():
    with mock.patch.object(cf, "check_cloudflare", return_value=True):
        out = _parse(to.cloudflare_list_dns_records({"type": "SRV"}))
    assert out["success"] is False
    assert "type must be" in out["error"]


# --- 성공 경로 (클라이언트를 mock) ---------------------------------------------------
def test_list_incidents_happy_path_maps_and_caps():
    sample = [
        {
            "id": "P123",
            "number": 7,
            "title": "instance CPU high",
            "status": "triggered",
            "urgency": "high",
            "service": "lab-fleet",
            "created_at": "2026-07-09T00:00:00Z",
            "assignees": ["oncall-user"],
            "url": "https://acme.pagerduty.com/incidents/P123",
        }
    ]
    with (
        mock.patch.object(pd, "check_pagerduty", return_value=True),
        mock.patch.object(pd, "list_incidents", return_value=sample) as m,
    ):
        out = _parse(
            to.pagerduty_list_incidents(
                {"status": "triggered", "since_hours": 9999, "limit": 9999}
            )
        )
    assert out["success"] is True
    assert out["count"] == 1
    assert out["incidents"][0]["id"] == "P123"
    # 상한: since_hours <= 720, limit <= 100
    m.assert_called_once_with(["triggered"], 720, 100)
    assert out["evidence"][0]["statuses"] == ["triggered"]


def test_get_oncall_happy_path():
    sample = [
        {
            "escalation_policy": "lab-ep",
            "level": 1,
            "user": "oncall-user",
            "schedule": "primary",
            "start": None,
            "end": None,
        }
    ]
    with (
        mock.patch.object(pd, "check_pagerduty", return_value=True),
        mock.patch.object(pd, "get_oncalls", return_value=sample),
    ):
        out = _parse(to.pagerduty_get_oncall({}))
    assert out["success"] is True
    assert out["oncalls"][0]["user"] == "oncall-user"


def test_list_dns_records_happy_path_passes_filters():
    sample = [{"name": "app.example.com", "type": "A", "content": "203.0.113.7"}]
    with (
        mock.patch.object(cf, "check_cloudflare", return_value=True),
        mock.patch.object(cf, "list_dns_records", return_value=sample) as m,
    ):
        out = _parse(
            to.cloudflare_list_dns_records({"name_contains": "app", "type": "A"})
        )
    assert out["success"] is True
    assert out["count"] == 1
    m.assert_called_once_with(name_contains="app", record_type="A")


def test_list_waf_rules_happy_path():
    sample = [
        {
            "ruleset": "custom",
            "phase": "http_request_firewall_custom",
            "rules": [{"description": "block scanner", "action": "block"}],
        }
    ]
    with (
        mock.patch.object(cf, "check_cloudflare", return_value=True),
        mock.patch.object(cf, "list_waf_custom_rules", return_value=sample),
    ):
        out = _parse(to.cloudflare_list_waf_rules({}))
    assert out["success"] is True
    assert out["rulesets"][0]["rules"][0]["action"] == "block"


def test_get_analytics_happy_path_and_clamps_period():
    sample = {
        "period_hours": 168,
        "total_requests": 1100,
        "requests_5xx": 200,
        "rate_5xx_pct": 18.182,
        "by_class": {"5xx": 200},
    }
    with (
        mock.patch.object(cf, "check_cloudflare", return_value=True),
        mock.patch.object(cf, "http_status_summary", return_value=sample) as m,
    ):
        # 999h 요청은 168로 clamp 돼 클라이언트에 전달된다
        out = _parse(to.cloudflare_get_analytics({"period_hours": 999}))
    assert out["success"] is True
    assert out["analytics"]["rate_5xx_pct"] == 18.182
    m.assert_called_once_with(168)


# --- 클라이언트 매핑 (_get만 mock, 클라이언트 코드는 실제로 실행) ---------------------
def test_pagerduty_client_maps_api_payload():
    payload = {
        "incidents": [
            {
                "id": "P1",
                "incident_number": 3,
                "title": "t",
                "status": "resolved",
                "urgency": "low",
                "service": {"summary": "svc"},
                "created_at": "2026-07-09T00:00:00Z",
                "assignments": [{"assignee": {"summary": "oncall-user"}}],
                "html_url": "https://x/incidents/P1",
            }
        ]
    }
    with mock.patch.object(pd, "_get", return_value=payload) as m:
        out = pd.list_incidents(["resolved"], since_hours=1, limit=5)
    assert out == [
        {
            "id": "P1",
            "number": 3,
            "title": "t",
            "status": "resolved",
            "urgency": "low",
            "service": "svc",
            "created_at": "2026-07-09T00:00:00Z",
            "assignees": ["oncall-user"],
            "url": "https://x/incidents/P1",
        }
    ]
    params = m.call_args.args[1]
    assert params["statuses[]"] == ["resolved"]
    assert params["limit"] == 5


def test_pagerduty_client_rejects_invalid_status_before_any_call():
    with mock.patch.object(pd, "_get") as m:
        try:
            pd.list_incidents(["exploded"])
            raise AssertionError("expected PagerDutyError")
        except pd.PagerDutyError:
            pass
    m.assert_not_called()


def test_cloudflare_client_expands_only_custom_firewall_phase(monkeypatch):
    monkeypatch.setenv("OPS_CLOUDFLARE_ZONE_ID", "zone123")

    def fake_get(path, params=None):
        if path.endswith("/rulesets"):
            return [
                {
                    "id": "rs1",
                    "name": "custom",
                    "phase": "http_request_firewall_custom",
                },
                {
                    "id": "rs2",
                    "name": "managed",
                    "phase": "http_request_firewall_managed",
                },
            ]
        assert path.endswith("/rulesets/rs1"), f"unexpected detail fetch: {path}"
        return {
            "rules": [
                {
                    "description": "block scanner",
                    "expression": 'http.user_agent contains "sqlmap"',
                    "action": "block",
                    "enabled": True,
                }
            ]
        }

    with mock.patch.object(cf, "_get", side_effect=fake_get):
        out = cf.list_waf_custom_rules()
    assert len(out) == 1
    assert out[0]["ruleset"] == "custom"
    assert out[0]["rules"][0]["action"] == "block"


def test_cloudflare_http_status_summary_aggregates_5xx_rate(monkeypatch):
    monkeypatch.setenv("OPS_CLOUDFLARE_ZONE_ID", "zone123")
    payload = {
        "viewer": {
            "zones": [
                {
                    "httpRequests1hGroups": [
                        {
                            "sum": {
                                "responseStatusMap": [
                                    {"edgeResponseStatus": 200, "requests": 800},
                                    {"edgeResponseStatus": 502, "requests": 150},
                                ]
                            }
                        },
                        {
                            "sum": {
                                "responseStatusMap": [
                                    {"edgeResponseStatus": 200, "requests": 100},
                                    {"edgeResponseStatus": 503, "requests": 50},
                                ]
                            }
                        },
                    ]
                }
            ]
        }
    }
    with mock.patch.object(cf, "_graphql", return_value=payload) as m:
        out = cf.http_status_summary(24)
    # 두 시간 버킷의 responseStatusMap 을 상태코드별로 합산한다
    assert out["total_requests"] == 1100
    assert out["by_class"]["2xx"] == 900
    assert out["requests_5xx"] == 200
    assert out["rate_5xx_pct"] == round(100.0 * 200 / 1100, 3)
    # 5xx 세부는 상태코드 내림차순
    assert out["status_5xx_breakdown"] == {"503": 50, "502": 150}
    # zoneTag 는 query 문자열에 인라인된다
    assert "zone123" in m.call_args.args[0]


def test_cloudflare_http_status_summary_zero_traffic_is_zero_rate(monkeypatch):
    monkeypatch.setenv("OPS_CLOUDFLARE_ZONE_ID", "zone123")
    empty = {"viewer": {"zones": [{"httpRequests1hGroups": []}]}}
    with mock.patch.object(cf, "_graphql", return_value=empty):
        out = cf.http_status_summary(1)
    assert out["total_requests"] == 0
    assert out["rate_5xx_pct"] == 0.0


def test_cloudflare_graphql_raises_on_errors_envelope(monkeypatch):
    monkeypatch.setenv("OPS_CLOUDFLARE_READ_TOKEN", "tok")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"data": None, "errors": [{"message": "no Analytics:Read"}]}

    with mock.patch.object(cf, "http_request", return_value=FakeResp()):
        try:
            cf._graphql("{ viewer { __typename } }")
            raise AssertionError("expected CloudflareError")
        except cf.CloudflareError as e:
            assert "no Analytics:Read" in str(e)


def test_cloudflare_client_passes_server_side_name_filter(monkeypatch):
    monkeypatch.setenv("OPS_CLOUDFLARE_ZONE_ID", "zone123")
    envelope = {
        "success": True,
        "result": [{"name": "app.example.com", "type": "A", "content": "1.2.3.4"}],
        "result_info": {"page": 1, "total_pages": 1},
    }
    with mock.patch.object(cf, "_get_envelope", return_value=envelope) as m:
        out = cf.list_dns_records(name_contains="app")
    assert [r["name"] for r in out] == ["app.example.com"]
    # 필터는 클라이언트가 아니라 서버 측 파라미터로 전달된다 (모든 페이지에 적용)
    params = m.call_args.args[1]
    assert params["name.contains"] == "app"


def test_cloudflare_client_paginates_dns_records(monkeypatch):
    monkeypatch.setenv("OPS_CLOUDFLARE_ZONE_ID", "zone123")

    def fake_envelope(path, params=None):
        page = params["page"]
        return {
            "success": True,
            "result": [
                {"name": f"rec-{page}.example.com", "type": "A", "content": "1.2.3.4"}
            ],
            "result_info": {"page": page, "total_pages": 3},
        }

    with mock.patch.object(cf, "_get_envelope", side_effect=fake_envelope) as m:
        out = cf.list_dns_records()
    assert [r["name"] for r in out] == [
        "rec-1.example.com",
        "rec-2.example.com",
        "rec-3.example.com",
    ]
    assert m.call_count == 3


def test_check_cloudflare_requires_zone_id_not_just_token(monkeypatch):
    # 토큰만 있고 zone id가 없으면 게이트가 닫혀야 한다 (반대 조합도 동일)
    monkeypatch.setenv("OPS_CLOUDFLARE_READ_TOKEN", "tok")
    monkeypatch.delenv("OPS_CLOUDFLARE_ZONE_ID", raising=False)
    assert cf.check_cloudflare() is False


def test_handlers_wrap_client_errors_as_fail_json():
    with (
        mock.patch.object(cf, "check_cloudflare", return_value=True),
        mock.patch.object(
            cf, "list_dns_records", side_effect=cf.CloudflareError("boom")
        ),
    ):
        out = _parse(to.cloudflare_list_dns_records({}))
    assert out["success"] is False
    assert "boom" in out["error"]

    with (
        mock.patch.object(pd, "check_pagerduty", return_value=True),
        mock.patch.object(pd, "list_incidents", side_effect=pd.PagerDutyError("kaput")),
    ):
        out = _parse(to.pagerduty_list_incidents({}))
    assert out["success"] is False
    assert "kaput" in out["error"]


def test_cloudflare_zone_name_resolves_apex_domain(monkeypatch):
    monkeypatch.setenv("OPS_CLOUDFLARE_ZONE_ID", "zone123")

    def fake_get(path, params=None):
        assert path == "/zones/zone123", f"unexpected path: {path}"
        return {"id": "zone123", "name": "example.com"}

    with mock.patch.object(cf, "_get", side_effect=fake_get):
        assert cf.zone_name() == "example.com"


def test_cloudflare_zone_name_raises_when_missing(monkeypatch):
    monkeypatch.setenv("OPS_CLOUDFLARE_ZONE_ID", "zone123")
    with mock.patch.object(cf, "_get", return_value={"id": "zone123"}):
        try:
            cf.zone_name()
            raise AssertionError("expected CloudflareError")
        except cf.CloudflareError:
            pass
