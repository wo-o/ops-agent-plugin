"""PagerDuty 클라이언트 — read(incident 목록/온콜) + bounded incident lifecycle write.

read 엔드포인트 (REST API v2):
  · GET /incidents   (ops_pagerduty_list_incidents)
  · GET /oncalls     (ops_pagerduty_get_oncall)

write 는 incident lifecycle 3개 (ops_pagerduty_manage_incident):
  · acknowledge / resolve  — PUT /incidents/{id}
  · snooze                 — POST /incidents/{id}/snooze (상한 24h)
+ 에스컬레이션 페이지 1개 (ops_pagerduty_page_oncall):
  · trigger — Events API v2 enqueue (routing key, dedup_key 필수)

lifecycle write 에는 full-access API 토큰 + From 헤더용 사용자 이메일
(OPS_PAGERDUTY_FROM_EMAIL — PD REST 필수 헤더)이, page 에는 OPS_PAGERDUTY_ROUTING_KEY
(3-1-pagerduty output)가 있어야 하고, 없으면 해당 write 도구가 아예 노출되지 않는다.
스케줄/서비스/정책 변경 같은 구성 write 는 여기 없다 — 그건 3-1-pagerduty Terraform 이
담당한다.
HTTP는 표준 라이브러리(urllib) 헬퍼만 쓰므로 외부 HTTP 패키지 없이 로드·테스트된다.
"""

from __future__ import annotations

from typing import Any

from . import env, http_request

_API = "https://api.pagerduty.com"

VALID_STATUSES = ("triggered", "acknowledged", "resolved")


def check_pagerduty() -> bool:
    """Visibility 게이트: OPS_PAGERDUTY_TOKEN 설정됨 (HTTP 는 stdlib)."""
    return bool(env("OPS_PAGERDUTY_TOKEN"))


class PagerDutyError(Exception):
    pass


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    r = http_request(
        "GET",
        _API + path,
        headers={
            "Authorization": f"Token token={env('OPS_PAGERDUTY_TOKEN')}",
            "Accept": "application/json",
            "User-Agent": "ops-plugin",
        },
        params=params or None,
    )
    if r.status_code >= 400:
        raise PagerDutyError(f"GET {path} -> {r.status_code} {r.text[:200]}")
    return r.json()


def _since_iso(hours: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def list_incidents(
    statuses: list[str], since_hours: int = 24, limit: int = 25
) -> list[dict[str, Any]]:
    """직전 since_hours 창의 incident 목록. statuses는 VALID_STATUSES의 부분집합."""
    for s in statuses:
        if s not in VALID_STATUSES:
            raise PagerDutyError(
                f"invalid status {s!r}; allowed: {list(VALID_STATUSES)}"
            )
    params: dict[str, Any] = {
        "since": _since_iso(since_hours),
        "limit": limit,
        "sort_by": "created_at:desc",
        "statuses[]": statuses,
    }
    data = _get("/incidents", params)
    out: list[dict[str, Any]] = []
    for inc in data.get("incidents", []):
        out.append(
            {
                "id": inc.get("id"),
                "number": inc.get("incident_number"),
                "title": inc.get("title"),
                "status": inc.get("status"),
                "urgency": inc.get("urgency"),
                "service": (inc.get("service") or {}).get("summary"),
                "created_at": inc.get("created_at"),
                "assignees": [
                    (a.get("assignee") or {}).get("summary")
                    for a in (inc.get("assignments") or [])
                ],
                "url": inc.get("html_url"),
            }
        )
    return out


def get_oncalls() -> list[dict[str, Any]]:
    """현재 시각의 온콜 목록(에스컬레이션 정책 / 레벨 / 사람 / 스케줄 창)."""
    data = _get("/oncalls", {"limit": 100})
    out: list[dict[str, Any]] = []
    for oc in data.get("oncalls", []):
        out.append(
            {
                "escalation_policy": (oc.get("escalation_policy") or {}).get("summary"),
                "level": oc.get("escalation_level"),
                "user": (oc.get("user") or {}).get("summary"),
                "schedule": (oc.get("schedule") or {}).get("summary"),
                "start": oc.get("start"),
                "end": oc.get("end"),
            }
        )
    return out


# ---------------------------------------------------------------- 쓰기 (범위 제한)
_SNOOZE_MAX_MINUTES = 24 * 60


def check_pagerduty_write() -> bool:
    """Visibility 게이트: 토큰 + From 이메일(write 필수 헤더) 둘 다 있어야 한다."""
    return bool(env("OPS_PAGERDUTY_TOKEN") and env("OPS_PAGERDUTY_FROM_EMAIL"))


def _write(method: str, path: str, body: dict[str, Any]) -> Any:
    r = http_request(
        method,
        _API + path,
        headers={
            "Authorization": f"Token token={env('OPS_PAGERDUTY_TOKEN')}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "From": env("OPS_PAGERDUTY_FROM_EMAIL") or "",
            "User-Agent": "ops-plugin",
        },
        body=body,
    )
    if r.status_code >= 400:
        raise PagerDutyError(f"{method} {path} -> {r.status_code} {r.text[:200]}")
    return r.json()


def _validate_incident_id(incident_id: str) -> str:
    import re

    if not re.fullmatch(r"[A-Z0-9]{1,20}", incident_id or ""):
        raise PagerDutyError(f"invalid incident id {incident_id!r}")
    return incident_id


def manage_incident(
    incident_id: str, action: str, snooze_minutes: int = 60
) -> dict[str, Any]:
    """incident 하나에 대한 lifecycle 액션. action: acknowledge|snooze|resolve.

    trigger(새 incident 생성)는 여기 없다 — 에스컬레이션 페이지는 page_oncall
    (Events API v2)이 담당한다.
    """
    incident_id = _validate_incident_id(incident_id)
    if action in ("acknowledge", "resolve"):
        data = _write(
            "PUT",
            f"/incidents/{incident_id}",
            {
                "incident": {
                    "type": "incident_reference",
                    "status": "acknowledged" if action == "acknowledge" else "resolved",
                }
            },
        )
        inc = data.get("incident") or {}
    elif action == "snooze":
        if not (1 <= snooze_minutes <= _SNOOZE_MAX_MINUTES):
            raise PagerDutyError(
                f"snooze_minutes must be 1..{_SNOOZE_MAX_MINUTES} (got {snooze_minutes})"
            )
        data = _write(
            "POST",
            f"/incidents/{incident_id}/snooze",
            {"duration": snooze_minutes * 60},
        )
        inc = data.get("incident") or {}
    else:
        raise PagerDutyError(
            f"invalid action {action!r}; allowed: acknowledge, snooze, resolve"
        )
    return {
        "id": inc.get("id"),
        "number": inc.get("incident_number"),
        "status": inc.get("status"),
        "title": inc.get("title"),
        "url": inc.get("html_url"),
    }


# ---------------------------------------------------------------- 페이지 (Events API v2)
# The one incident-CREATING write. Deliberately separate from the REST lifecycle
# writes: it needs only the Events API v2 routing key (3-1-pagerduty output), and
# the escalation policy on the service does the actual paging. dedup_key is
# mandatory so repeat notifications of the same episode collapse into one page.
_EVENTS_API = "https://events.pagerduty.com/v2/enqueue"
_SUMMARY_MAX = 1024
_DEDUP_MAX = 255


def check_pagerduty_page() -> bool:
    """Visibility 게이트: OPS_PAGERDUTY_ROUTING_KEY (Events API v2) 설정됨."""
    return bool(env("OPS_PAGERDUTY_ROUTING_KEY"))


def page_oncall(
    summary: str,
    dedup_key: str,
    details: dict[str, Any] | None = None,
    link: str = "",
) -> dict[str, Any]:
    """Events API v2 trigger — escalation policy를 태워 온콜을 실제 페이지한다.

    summary/dedup_key 필수. 같은 dedup_key의 open incident에는 새 페이지가
    나가지 않고 alert만 병합된다(에피소드당 1 incident).
    """
    summary = (summary or "").strip()
    dedup_key = (dedup_key or "").strip()
    if not summary:
        raise PagerDutyError("page requires a non-empty summary")
    if not dedup_key:
        raise PagerDutyError(
            "page requires dedup_key (alert UID + active_at — one page per episode)"
        )
    if len(dedup_key) > _DEDUP_MAX:
        raise PagerDutyError(f"dedup_key too long (max {_DEDUP_MAX})")
    body: dict[str, Any] = {
        "routing_key": env("OPS_PAGERDUTY_ROUTING_KEY"),
        "event_action": "trigger",
        "dedup_key": dedup_key,
        "payload": {
            "summary": summary[:_SUMMARY_MAX],
            "source": "ops-agent",
            "severity": "critical",
            "custom_details": details or {},
        },
    }
    if link:
        body["links"] = [{"href": link, "text": "Slack thread"}]
    r = http_request(
        "POST",
        _EVENTS_API,
        headers={"Accept": "application/json", "User-Agent": "ops-plugin"},
        body=body,
    )
    if r.status_code >= 400:
        raise PagerDutyError(f"POST v2/enqueue -> {r.status_code} {r.text[:200]}")
    data = r.json() or {}
    return {
        "status": data.get("status"),
        "dedup_key": data.get("dedup_key") or dedup_key,
    }
