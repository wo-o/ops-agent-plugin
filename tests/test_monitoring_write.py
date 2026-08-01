"""ops-monitoring-write 계약 테스트 — Grafana silence + PagerDuty incident lifecycle.

다른 write 경로와 같은 계약: JSON 문자열 + {"success": bool} envelope, credential 없으면
remediation 에러, 잘못된 파라미터는 raise가 아니라 fail. HTTP는 http_request를 mock한다.
"""

import json
from unittest import mock

import ops_plugin.clients.grafana as gf
import ops_plugin.clients.pagerduty as pd
import ops_plugin.tools_monitoring as tm


def _parse(s):
    assert isinstance(s, str), f"handler must return a JSON string, got {type(s)}"
    return json.loads(s)


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


# --- credential degradation (conftest가 모든 credential을 제거) ----------------------
def test_tools_fail_cleanly_without_credentials():
    out = _parse(tm.grafana_silence({"action": "list"}))
    assert out["success"] is False
    assert "OPS_GRAFANA_TOKEN" in out.get("remediation", "")

    out = _parse(
        tm.pagerduty_manage_incident({"incident_id": "PABC1", "action": "acknowledge"})
    )
    assert out["success"] is False
    assert "OPS_PAGERDUTY_FROM_EMAIL" in out.get("remediation", "")

    out = _parse(tm.pagerduty_page_oncall({"summary": "s", "dedup_key": "d"}))
    assert out["success"] is False
    assert "OPS_PAGERDUTY_ROUTING_KEY" in out.get("remediation", "")

    assert tm.check_grafana_silence_requirements() is False
    assert tm.check_pagerduty_write_requirements() is False
    assert tm.check_pagerduty_page_requirements() is False


# --- Grafana silence ---------------------------------------------------------------
def _with_grafana(fn):
    return mock.patch.object(gf, "check_grafana", return_value=True)(fn)


def test_silence_create_requires_alertname_and_comment():
    with mock.patch.object(gf, "check_grafana", return_value=True):
        out = _parse(tm.grafana_silence({"action": "create"}))
        assert out["success"] is False and "alertname" in out["error"]

        out = _parse(
            tm.grafana_silence(
                {"action": "create", "alertname": "cpu high", "comment": " "}
            )
        )
        assert out["success"] is False and "comment" in out["error"]


def test_silence_create_bounds_duration_and_matcher_chars():
    with mock.patch.object(gf, "check_grafana", return_value=True):
        out = _parse(
            tm.grafana_silence(
                {
                    "action": "create",
                    "alertname": "x",
                    "comment": "c",
                    "duration_minutes": 100000,
                }
            )
        )
        assert out["success"] is False and "duration_minutes" in out["error"]

        out = _parse(
            tm.grafana_silence(
                {"action": "create", "alertname": 'a"b{', "comment": "c"}
            )
        )
        assert out["success"] is False and "invalid matcher" in out["error"]


def test_silence_create_posts_and_returns_id():
    with (
        mock.patch.object(gf, "check_grafana", return_value=True),
        mock.patch.object(gf.settings, "grafana_url", return_value="http://g"),
        mock.patch.object(
            gf, "http_request", return_value=_Resp(201, {"silenceID": "sid-1"})
        ) as m,
    ):
        out = _parse(
            tm.grafana_silence(
                {
                    "action": "create",
                    "alertname": "[monitoring] cpu high",
                    "instance": "10.0.0.1:9100",
                    "duration_minutes": 30,
                    "comment": "rolling restart underway",
                }
            )
        )
    assert out["success"] is True
    assert out["silence_id"] == "sid-1"
    assert out["matchers"]["alertname"] == "[monitoring] cpu high"
    method, url = m.call_args[0][:2]
    assert method == "POST" and url.endswith(
        "/api/alertmanager/grafana/api/v2/silences"
    )
    body = m.call_args[1]["body"]
    assert all(m_["isRegex"] is False for m_ in body["matchers"])
    assert body["endsAt"] > body["startsAt"]


def test_silence_expire_and_list():
    with (
        mock.patch.object(gf, "check_grafana", return_value=True),
        mock.patch.object(gf.settings, "grafana_url", return_value="http://g"),
    ):
        with mock.patch.object(gf, "http_request", return_value=_Resp(200, {})) as m:
            out = _parse(
                tm.grafana_silence({"action": "expire", "silence_id": "sid-9"})
            )
            assert out["success"] is True and out["expired"] is True
            assert m.call_args[0][0] == "DELETE"
            assert m.call_args[0][1].endswith("/silence/sid-9")

        listed = [
            {
                "id": "a",
                "status": {"state": "active"},
                "matchers": [{"name": "alertname", "value": "x"}],
                "endsAt": "e",
                "comment": "c",
                "createdBy": "ops-agent",
            },
            {"id": "b", "status": {"state": "expired"}, "matchers": []},
        ]
        with mock.patch.object(gf, "http_request", return_value=_Resp(200, listed)):
            out = _parse(tm.grafana_silence({"action": "list"}))
            assert out["success"] is True and out["count"] == 1
            assert out["silences"][0]["silence_id"] == "a"

        out = _parse(tm.grafana_silence({"action": "expire"}))
        assert out["success"] is False and "silence_id" in out["error"]

        out = _parse(tm.grafana_silence({"action": "nuke"}))
        assert out["success"] is False and "invalid action" in out["error"]


# --- PagerDuty incident lifecycle --------------------------------------------------
def test_pd_manage_validates_id_and_action():
    with mock.patch.object(pd, "check_pagerduty_write", return_value=True):
        out = _parse(
            tm.pagerduty_manage_incident({"incident_id": "../etc", "action": "resolve"})
        )
        assert out["success"] is False and "invalid incident id" in out["error"]

        out = _parse(
            tm.pagerduty_manage_incident({"incident_id": "PABC1", "action": "explode"})
        )
        assert out["success"] is False and "invalid action" in out["error"]

        out = _parse(
            tm.pagerduty_manage_incident(
                {"incident_id": "PABC1", "action": "snooze", "snooze_minutes": 999999}
            )
        )
        assert out["success"] is False and "snooze_minutes" in out["error"]


def test_pd_ack_and_snooze_and_resolve_requests():
    inc = {
        "incident": {
            "id": "PABC1",
            "incident_number": 7,
            "status": "acknowledged",
            "title": "t",
            "html_url": "u",
        }
    }
    with (
        mock.patch.object(pd, "check_pagerduty_write", return_value=True),
        mock.patch.object(
            pd,
            "env",
            side_effect=lambda k: {
                "OPS_PAGERDUTY_TOKEN": "tk",
                "OPS_PAGERDUTY_FROM_EMAIL": "a@b.c",
            }.get(k),
        ),
        mock.patch.object(pd, "http_request", return_value=_Resp(200, inc)) as m,
    ):
        out = _parse(
            tm.pagerduty_manage_incident(
                {"incident_id": "PABC1", "action": "acknowledge"}
            )
        )
        assert out["success"] is True and out["status"] == "acknowledged"
        method, url = m.call_args[0][:2]
        assert method == "PUT" and url.endswith("/incidents/PABC1")
        assert m.call_args[1]["headers"]["From"] == "a@b.c"
        assert m.call_args[1]["body"]["incident"]["status"] == "acknowledged"

        _parse(
            tm.pagerduty_manage_incident(
                {"incident_id": "PABC1", "action": "snooze", "snooze_minutes": 30}
            )
        )
        method, url = m.call_args[0][:2]
        assert method == "POST" and url.endswith("/incidents/PABC1/snooze")
        assert m.call_args[1]["body"]["duration"] == 1800

        _parse(
            tm.pagerduty_manage_incident({"incident_id": "PABC1", "action": "resolve"})
        )
        assert m.call_args[1]["body"]["incident"]["status"] == "resolved"


# --- PagerDuty page (Events API v2) -------------------------------------------------
def test_pd_page_requires_summary_and_dedup_key():
    with mock.patch.object(pd, "check_pagerduty_page", return_value=True):
        out = _parse(tm.pagerduty_page_oncall({"dedup_key": "d"}))
        assert out["success"] is False and "summary" in out["error"]

        out = _parse(tm.pagerduty_page_oncall({"summary": "s"}))
        assert out["success"] is False and "dedup_key" in out["error"]

        out = _parse(tm.pagerduty_page_oncall({"summary": "s", "dedup_key": "d" * 300}))
        assert out["success"] is False and "dedup_key too long" in out["error"]

        out = _parse(
            tm.pagerduty_page_oncall(
                {"summary": "s", "dedup_key": "d", "details": "not-an-object"}
            )
        )
        assert out["success"] is False and "details" in out["error"]


def test_pd_page_triggers_events_api_with_dedup():
    with (
        mock.patch.object(pd, "check_pagerduty_page", return_value=True),
        mock.patch.object(
            pd, "env", side_effect=lambda k: {"OPS_PAGERDUTY_ROUTING_KEY": "rk"}.get(k)
        ),
        mock.patch.object(
            pd,
            "http_request",
            return_value=_Resp(202, {"status": "success", "dedup_key": "uid:t0"}),
        ) as m,
    ):
        out = _parse(
            tm.pagerduty_page_oncall(
                {
                    "summary": "prod InstanceDown — 자동 대응 중단(3회 재발화)",
                    "dedup_key": "uid:t0",
                    "details": {
                        "attempted": ["rolling-restart run#12"],
                        "state": "still firing",
                    },
                    "link": "https://slack.example/thread",
                }
            )
        )
    assert out["success"] is True and out["dedup_key"] == "uid:t0"
    assert "Do NOT" in out["note"]
    method, url = m.call_args[0][:2]
    assert method == "POST" and url == "https://events.pagerduty.com/v2/enqueue"
    body = m.call_args[1]["body"]
    assert body["routing_key"] == "rk"
    assert body["event_action"] == "trigger"
    assert body["dedup_key"] == "uid:t0"
    assert body["payload"]["severity"] == "critical"
    assert body["payload"]["custom_details"]["state"] == "still firing"
    assert body["links"][0]["href"] == "https://slack.example/thread"


def test_pd_page_surfaces_api_errors():
    with (
        mock.patch.object(pd, "check_pagerduty_page", return_value=True),
        mock.patch.object(
            pd, "env", side_effect=lambda k: {"OPS_PAGERDUTY_ROUTING_KEY": "rk"}.get(k)
        ),
        mock.patch.object(
            pd, "http_request", return_value=_Resp(400, {"status": "invalid event"})
        ),
    ):
        out = _parse(tm.pagerduty_page_oncall({"summary": "s", "dedup_key": "d"}))
        assert out["success"] is False and "400" in out["error"]
