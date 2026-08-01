"""monitoring write toolset — 세 번째 write 경로: 알람 통지 상태의 bounded 조작.

tfvars PR(인프라 값)·ansible dispatch(서버 런타임 상태)와 별개로, "알람 통지"라는
런타임 상태를 다룬다: Grafana silence(만료 필수, 최대 24h), PagerDuty incident
lifecycle(ack/snooze/resolve 3개), 그리고 온콜 실페이지(Events API v2 trigger).
silence 는 만료가 있는 임시 상태라 git/PR 모델과 맞지 않아(Terraform grafana
provider 에도 silence 리소스가 없다) 이 경로가 정본이다.

안전 경계:
  · Grafana — exact-match matcher(alertname + 선택 instance)만, 정규식 금지(과대
    mute 방지), duration 상한 24h(무기한 봉쇄), comment 필수(감사).
  · PagerDuty lifecycle — 기존 incident 의 3액션만. 스케줄/서비스/정책 변경
    없음(구성은 3-1-pagerduty Terraform).
  · PagerDuty page — 런북 §4 서킷 브레이커 전용 에스컬레이션. 알람은 Slack 단일
    통지이므로 이 도구가 사람 폰을 울리는 유일한 경로다. dedup_key 필수(에피소드당
    1 incident — 재호출해도 중복 페이지 없음).
  · 자격증명 게이트 — Grafana 는 Editor SA 토큰(기존 OPS_GRAFANA_* 재사용),
    PagerDuty lifecycle 은 full-access 토큰 + OPS_PAGERDUTY_FROM_EMAIL(REST 필수
    From 헤더), page 는 OPS_PAGERDUTY_ROUTING_KEY(3-1 output) — 없으면 해당 도구가
    노출되지 않는다.
  · 감사 — post_tool_call 훅의 append-only JSONL (다른 write 경로와 동일).
"""

from __future__ import annotations

from typing import Any

from ._compat import fail, ok
from .clients import grafana, pagerduty

_GRAFANA_REMEDIATION = (
    "Set OPS_GRAFANA_URL + OPS_GRAFANA_TOKEN in ~/.hermes/.env. Silences need an "
    "Editor-role SA token (2-0-setup/4-grafana issues it) — a Viewer token gets 403."
)
_PAGERDUTY_WRITE_REMEDIATION = (
    "Set OPS_PAGERDUTY_TOKEN (full-access REST API key — a read-only key gets 403) "
    "and OPS_PAGERDUTY_FROM_EMAIL (a valid PagerDuty user email; required From "
    "header for incident writes) in ~/.hermes/.env."
)


def check_grafana_silence_requirements() -> bool:
    return grafana.check_grafana()


def check_pagerduty_write_requirements() -> bool:
    return pagerduty.check_pagerduty_write()


def grafana_silence(args: dict, **kwargs: Any) -> str:
    """Grafana silence create/expire/list. create 는 알람 통지만 멈춘다 — 룰 평가와
    상태(firing)는 계속 돌므로, 원인 조치 없이 걸면 장애가 조용히 지속된다."""
    action = args.get("action")
    try:
        if not grafana.check_grafana():
            return fail("grafana not configured", remediation=_GRAFANA_REMEDIATION)
        if action == "create":
            alertname = (args.get("alertname") or "").strip()
            if not alertname:
                return fail("create requires alertname (see ops_explain_alert / rules)")
            res = grafana.create_silence(
                alertname=alertname,
                duration_minutes=int(args.get("duration_minutes") or 60),
                comment=(args.get("comment") or "").strip(),
                instance=(args.get("instance") or "").strip(),
            )
            return ok(
                **res,
                note=(
                    "Notifications muted until ends_at; the rule keeps evaluating and "
                    "stays firing until the cause is fixed. Report the silence_id and "
                    "ends_at in the Slack thread."
                ),
            )
        if action == "expire":
            silence_id = (args.get("silence_id") or "").strip()
            if not silence_id:
                return fail("expire requires silence_id (from create or list)")
            return ok(**grafana.expire_silence(silence_id))
        if action == "list":
            silences = grafana.list_silences()
            return ok(silences=silences, count=len(silences))
        return fail(f"invalid action {action!r}; allowed: create, expire, list")
    except grafana.GrafanaError as e:
        return fail(f"ops_grafana_silence failed: {e}")


_PAGERDUTY_PAGE_REMEDIATION = (
    "Set OPS_PAGERDUTY_ROUTING_KEY (Events API v2 integration key — "
    "`terraform -chdir=3-1-pagerduty output -raw routing_key`) in ~/.hermes/.env. "
    "Until then escalation is Slack-only: name the on-call via ops_pagerduty_get_oncall, "
    "real-mention OPS_INFRA_SLACK_MENTION (from ~/.hermes/.env) in the handoff message, "
    "and report '온콜 확인 · Slack 인계 요청' — never claim a page was sent."
)


def check_pagerduty_page_requirements() -> bool:
    return pagerduty.check_pagerduty_page()


def pagerduty_page_oncall(args: dict, **kwargs: Any) -> str:
    """온콜 실페이지 (Events API v2). 런북이 자동 대응을 포기했을 때만 —
    dedup_key(에피소드 정체성) 필수, 같은 키 재호출은 기존 incident에 병합된다."""
    try:
        if not pagerduty.check_pagerduty_page():
            return fail(
                "PagerDuty paging not configured",
                remediation=_PAGERDUTY_PAGE_REMEDIATION,
            )
        details = args.get("details")
        if details is not None and not isinstance(details, dict):
            return fail("details must be an object (attempted actions, current state)")
        res = pagerduty.page_oncall(
            summary=(args.get("summary") or "").strip(),
            dedup_key=(args.get("dedup_key") or "").strip(),
            details=details,
            link=(args.get("link") or "").strip(),
        )
        return ok(
            **res,
            note=(
                "Page sent — the escalation policy is now ringing the on-call. Do NOT "
                "acknowledge/resolve this incident; it belongs to the human. Report "
                "'페이지 완료 (dedup_key=...)' with the attempted remediations in the "
                "Slack thread."
            ),
        )
    except pagerduty.PagerDutyError as e:
        return fail(f"ops_pagerduty_page_oncall failed: {e}")


def pagerduty_manage_incident(args: dict, **kwargs: Any) -> str:
    """PagerDuty incident ack/snooze/resolve. resolve 는 지표가 정상 복귀를 확인한
    뒤에만 — 소음 억제가 목적이면 snooze 를 쓴다."""
    try:
        if not pagerduty.check_pagerduty_write():
            return fail(
                "PagerDuty write not configured",
                remediation=_PAGERDUTY_WRITE_REMEDIATION,
            )
        res = pagerduty.manage_incident(
            incident_id=(args.get("incident_id") or "").strip(),
            action=args.get("action") or "",
            snooze_minutes=int(args.get("snooze_minutes") or 60),
        )
        return ok(**res)
    except pagerduty.PagerDutyError as e:
        return fail(f"ops_pagerduty_manage_incident failed: {e}")
