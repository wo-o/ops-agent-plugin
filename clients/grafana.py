"""랩 모니터링 스택을 위한 Grafana / Prometheus / Loki read 클라이언트.

모델은 raw PromQL/LogQL 을 절대 작성하지 않는다 — data/named-queries.yaml 에서 named query 를
고르면 핸들러가 검증된 라벨 값만 치환한다. 원래 ops 플러그인과 달리 여기에는 silence/write
엔드포인트가 없다: 이 클라이언트는 철저히 read 전용이다 (쿼리 + alert-rule 상태).

엔드포인트 (전부 read-only, 모니터링 Grafana 대상):
  · /api/datasources/proxy/uid/<uid>/api/v1/query           Prometheus instant query
  · /api/datasources/proxy/uid/<uid>/loki/api/v1/query_range Loki range query
  · /api/prometheus/grafana/api/v1/rules                    unified-alerting rule state

HTTP는 표준 라이브러리(urllib) 헬퍼만 쓰므로 외부 HTTP 패키지 없이 로드·테스트된다.
"""

from __future__ import annotations

import string
from typing import Any

from .. import settings
from . import env, http_request


def check_grafana() -> bool:
    """Visibility 게이트: OPS_GRAFANA_URL + OPS_GRAFANA_TOKEN 설정됨 (HTTP 는 stdlib)."""
    return bool(settings.grafana_url() and env("OPS_GRAFANA_TOKEN"))


class GrafanaError(Exception):
    pass


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {env('OPS_GRAFANA_TOKEN')}",
        "Accept": "application/json",
    }


# Datasource UID 는 모니터링 스택 docker-compose 프로비저닝 기본값으로 고정된다
# ("prometheus"/"loki"); 기본이 아닌 스택은 env 로 덮어쓸 수 있다.
def _prom_uid() -> str:
    return env("OPS_GRAFANA_PROM_UID") or "prometheus"


def _loki_uid() -> str:
    return env("OPS_GRAFANA_LOKI_UID") or "loki"


def _substitute(template: str, labels: dict[str, str], allowed: list[str]) -> str:
    """허용된 라벨 이름만 사용해 ${name} 플레이스홀더를 채운다. 값은 PromQL/LogQL 리터럴이며,
    쿼리 인젝션을 막으려고 따옴표/중괄호/개행을 거부한다."""
    safe: dict[str, str] = {}
    for k in allowed:
        v = str(labels.get(k, ""))
        if any(c in v for c in ('"', "'", "{", "}", "\n", "\\")):
            raise GrafanaError(f"invalid characters in label {k!r}")
        safe[k] = v
    return string.Template(template).safe_substitute(safe)


def _named(catalog_key: str, query_name: str) -> dict[str, Any]:
    cat = settings.named_queries().get(catalog_key, {})
    if query_name not in cat:
        raise GrafanaError(
            f"unknown {catalog_key} query {query_name!r}; allowed: {sorted(cat)}"
        )
    return cat[query_name]


def _limits() -> dict[str, Any]:
    return settings.named_queries().get("limits", {})


def query_metrics(
    query_name: str, labels: dict[str, str], range_hours: int = 1
) -> dict[str, Any]:
    q = _named("metrics", query_name)
    lim = _limits()
    range_hours = min(range_hours, int(lim.get("max_range_hours", 24)))
    promql = _substitute(q["promql"], labels, q.get("labels", []))

    base = settings.grafana_url()
    r = http_request(
        "GET",
        f"{base}/api/datasources/proxy/uid/{_prom_uid()}/api/v1/query",
        headers=_headers(),
        params={"query": promql},
        timeout=float(lim.get("query_timeout_seconds", 20)),
    )
    if r.status_code >= 400:
        raise GrafanaError(f"metrics query failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    return {
        "query_name": query_name,
        "promql": promql,
        # 표시용 링크는 public base — private base(base)는 브라우저에서 안 열린다.
        "dashboard": (settings.grafana_public_url() or "") + q.get("dashboard", ""),
        "result": data.get("data", {}).get("result", [])[
            : int(lim.get("max_series", 100))
        ],
    }


def _log_window_ns(range_hours: int) -> tuple[int, int]:
    """직전 range_hours 창에 대한 (start, end) 를 epoch 나노초로 반환한다. start/end 를 생략하면
    Loki 의 query_range 는 기본이 지난 1시간이라, range_hours 가 조용히 무시되어 버린다."""
    import time

    end = int(time.time() * 1e9)
    start = end - int(range_hours * 3600 * 1e9)
    return start, end


def query_logs(
    query_name: str, labels: dict[str, str], range_hours: int = 1
) -> dict[str, Any]:
    q = _named("logs", query_name)
    lim = _limits()
    range_hours = min(range_hours, int(lim.get("max_range_hours", 24)))
    logql = _substitute(q["logql"], labels, q.get("labels", []))

    base = settings.grafana_url()
    start_ns, end_ns = _log_window_ns(range_hours)
    r = http_request(
        "GET",
        f"{base}/api/datasources/proxy/uid/{_loki_uid()}/loki/api/v1/query_range",
        headers=_headers(),
        params={
            "query": logql,
            "limit": int(lim.get("max_log_lines", 200)),
            "start": str(start_ns),
            "end": str(end_ns),
            "direction": "backward",
        },
        timeout=float(lim.get("query_timeout_seconds", 20)),
    )
    if r.status_code >= 400:
        raise GrafanaError(f"logs query failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    return {
        "query_name": query_name,
        "logql": logql,
        "result": data.get("data", {}).get("result", []),
    }


def list_alert_rules() -> list[dict[str, Any]]:
    """unified-alerting 룰 상태를 읽는다 (Prometheus 호환 rules API).

    {name, state, labels, annotations, alerts} 의 플랫 리스트를 반환한다 — 네 개의 랩 alert
    룰(CPU / memory / disk / nginx 5xx) 과 랩 Grafana 에 정의된 그 외 룰까지. Read-only.
    """
    base = settings.grafana_url()
    lim = _limits()
    r = http_request(
        "GET",
        f"{base}/api/prometheus/grafana/api/v1/rules",
        headers=_headers(),
        timeout=float(lim.get("query_timeout_seconds", 20)),
    )
    if r.status_code >= 400:
        raise GrafanaError(f"rules read failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    out: list[dict[str, Any]] = []
    for group in data.get("data", {}).get("groups", []):
        for rule in group.get("rules", []):
            out.append(
                {
                    "name": rule.get("name"),
                    # 알람 webhook payload는 룰을 UID로 지칭한다 — explain_alert의
                    # UID 매칭이 이 필드에 의존한다(누락 시 매 알람 툴콜 1회 낭비).
                    "uid": rule.get("uid"),
                    "state": rule.get("state"),
                    "labels": rule.get("labels") or {},
                    "annotations": rule.get("annotations") or {},
                    "alerts": [
                        {
                            "state": a.get("state"),
                            "labels": a.get("labels") or {},
                            "active_at": a.get("activeAt"),
                            "value": a.get("value"),
                        }
                        for a in (rule.get("alerts") or [])
                    ][:20],
                }
            )
    return out


# ---------------------------------------------------------------- silence (쓰기)
# Grafana 내장 Alertmanager 의 silence API. Viewer 로는 403 — Editor 롤 SA 토큰이
# 필요하다(2-0-setup/4-grafana 가 발급). silence 는 만료가 있는 런타임 상태라
# tfvars PR 경로가 아니라 이 bounded write 경로로 다룬다(ansible dispatch 와 동급).
_SILENCE_MAX_MINUTES = 24 * 60  # 상한 24h — 만료 없는 무기한 mute 를 봉쇄한다
_SILENCE_MATCHER_NAMES = ("alertname", "instance")  # 허용 matcher 라벨 2개뿐


def _am(path: str) -> str:
    return f"{settings.grafana_url()}/api/alertmanager/grafana/api/v2{path}"


def create_silence(
    alertname: str,
    duration_minutes: int,
    comment: str,
    instance: str = "",
) -> dict[str, Any]:
    """alertname(+선택 instance) exact-match silence 를 만든다. 정규식 matcher 는
    지원하지 않는다(과대 mute 방지). 반환: {silence_id, ends_at, matchers}."""
    from datetime import datetime, timedelta, timezone

    if not (1 <= duration_minutes <= _SILENCE_MAX_MINUTES):
        raise GrafanaError(
            f"duration_minutes must be 1..{_SILENCE_MAX_MINUTES} (got {duration_minutes})"
        )
    if not comment.strip():
        raise GrafanaError("comment is required (why + expected recovery)")
    matchers = [
        {"name": "alertname", "value": alertname, "isRegex": False, "isEqual": True}
    ]
    if instance:
        matchers.append(
            {"name": "instance", "value": instance, "isRegex": False, "isEqual": True}
        )
    for m in matchers:
        if (
            any(c in m["value"] for c in ('"', "'", "{", "}", "\n", "\\"))
            or not m["value"].strip()
        ):
            raise GrafanaError(f"invalid matcher value for {m['name']!r}")

    now = datetime.now(timezone.utc)
    ends = now + timedelta(minutes=duration_minutes)
    r = http_request(
        "POST",
        _am("/silences"),
        headers={**_headers(), "Content-Type": "application/json"},
        body={
            "matchers": matchers,
            "startsAt": now.isoformat(),
            "endsAt": ends.isoformat(),
            "comment": comment,
            "createdBy": "ops-agent",
        },
    )
    if r.status_code >= 400:
        raise GrafanaError(f"silence create failed: {r.status_code} {r.text[:200]}")
    return {
        "silence_id": r.json().get("silenceID"),
        "ends_at": ends.isoformat(),
        "matchers": {m["name"]: m["value"] for m in matchers},
    }


def expire_silence(silence_id: str) -> dict[str, Any]:
    """silence 를 즉시 만료(unsilence)한다."""
    if not silence_id.strip() or "/" in silence_id:
        raise GrafanaError(f"invalid silence_id {silence_id!r}")
    r = http_request("DELETE", _am(f"/silence/{silence_id}"), headers=_headers())
    if r.status_code >= 400:
        raise GrafanaError(f"silence expire failed: {r.status_code} {r.text[:200]}")
    return {"silence_id": silence_id, "expired": True}


def list_silences() -> list[dict[str, Any]]:
    """active/pending silence 목록(만료된 것 제외)."""
    r = http_request("GET", _am("/silences"), headers=_headers())
    if r.status_code >= 400:
        raise GrafanaError(f"silence list failed: {r.status_code} {r.text[:200]}")
    out: list[dict[str, Any]] = []
    for s in r.json() or []:
        if (s.get("status") or {}).get("state") == "expired":
            continue
        out.append(
            {
                "silence_id": s.get("id"),
                "state": (s.get("status") or {}).get("state"),
                "matchers": {
                    m.get("name"): m.get("value") for m in (s.get("matchers") or [])
                },
                "ends_at": s.get("endsAt"),
                "comment": s.get("comment"),
                "created_by": s.get("createdBy"),
            }
        )
    return out
