"""Cloudflare READ 클라이언트 — DNS 레코드 / WAF 커스텀 룰 조회만 한다.

Cloudflare zone의 read 짝이다: 변경은 ops_github_open_tfvars_pr(surface
dev-dns / waf(존 전역))로 tfvars PR을 열어야 하고, 이 클라이언트는
"지금 실제 존재하는 것"을 읽기만 한다. CI의 apply 토큰과는 별개인 read 전용 zone
토큰(OPS_CLOUDFLARE_READ_TOKEN, Zone.DNS:Read + Zone.WAF:Read)을 쓴다.

read 엔드포인트:
  · GET  /zones/{zone}/dns_records                   (ops_cloudflare_list_dns_records)
  · GET  /zones/{zone}/rulesets + /rulesets/{id}      (ops_cloudflare_list_waf_rules —
      phase http_request_firewall_custom 룰셋만 전개한다)
  · POST /graphql (httpRequests1hGroups)             (ops_cloudflare_get_analytics —
      엣지 응답 상태코드 분포 + 5xx 비율; 토큰에 Zone Analytics:Read 필요)

HTTP는 표준 라이브러리(urllib) 헬퍼만 쓰므로 외부 HTTP 패키지 없이 로드·테스트된다.
"""

from __future__ import annotations

from typing import Any

from .. import settings
from . import env, http_request

_API = "https://api.cloudflare.com/client/v4"

_WAF_CUSTOM_PHASE = "http_request_firewall_custom"


def check_cloudflare() -> bool:
    """Visibility 게이트: OPS_CLOUDFLARE_READ_TOKEN + zone id 설정됨 (HTTP 는 stdlib)."""
    return bool(env("OPS_CLOUDFLARE_READ_TOKEN") and settings.cloudflare_zone_id())


class CloudflareError(Exception):
    pass


def _get_envelope(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """전체 응답 봉투({success, result, result_info, errors})를 반환한다 —
    페이지네이션이 필요한 목록 엔드포인트용."""
    r = http_request(
        "GET",
        _API + path,
        headers={
            "Authorization": f"Bearer {env('OPS_CLOUDFLARE_READ_TOKEN')}",
            "Accept": "application/json",
            "User-Agent": "ops-plugin",
        },
        params=params or None,
    )
    if r.status_code >= 400:
        raise CloudflareError(f"GET {path} -> {r.status_code} {r.text[:200]}")
    data = r.json()
    if not data.get("success", False):
        raise CloudflareError(
            f"GET {path} -> success=false errors={data.get('errors')}"
        )
    return data


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    return _get_envelope(path, params).get("result")


def _graphql(query: str) -> dict[str, Any]:
    """GraphQL Analytics API 를 POST 한다. 봉투는 {data, errors} 이고, errors 가
    있으면(부분 성공 포함) 실패로 처리한다 — 분석 결과가 조용히 비는 것을 막는다."""
    r = http_request(
        "POST",
        _API + "/graphql",
        headers={
            "Authorization": f"Bearer {env('OPS_CLOUDFLARE_READ_TOKEN')}",
            "Accept": "application/json",
            "User-Agent": "ops-plugin",
        },
        body={"query": query},
    )
    if r.status_code >= 400:
        raise CloudflareError(f"POST /graphql -> {r.status_code} {r.text[:200]}")
    data = r.json()
    if data.get("errors"):
        raise CloudflareError(f"POST /graphql -> errors={data['errors']}")
    return data.get("data") or {}


def zone_name() -> str:
    """관리 zone ID(OPS_CLOUDFLARE_ZONE_ID)의 실제 apex 도메인(예: `example.net`)을
    조회한다. tfvars DNS `name`은 이 값 기준으로 구성해야 한다 — 요청 FQDN이 다른
    zone(placeholder `example.com` 등)이면 Cloudflare가 관리 zone을 접미해 엉뚱한
    레코드를 만든다. 그걸 막으려면 호출자가 관리 zone의 실제 이름을 알아야 한다."""
    zone = settings.cloudflare_zone_id()
    result = _get(f"/zones/{zone}") or {}
    name = result.get("name")
    if not name:
        raise CloudflareError(f"GET /zones/{zone} -> no zone name in result")
    return str(name)


# 폭주 방지 상한 — 랩 zone 기준으로 넉넉하고, 그 이상은 잘렸음을 호출자가 안다.
_MAX_DNS_RECORDS = 1000


def list_dns_records(
    name_contains: str | None = None, record_type: str | None = None
) -> list[dict[str, Any]]:
    zone = settings.cloudflare_zone_id()
    params: dict[str, Any] = {"per_page": 100, "page": 1}
    if record_type:
        params["type"] = record_type
    if name_contains:
        # 서버 측 부분 일치 필터 — 클라이언트 측 필터와 달리 모든 페이지에서 매칭된다
        params["name.contains"] = name_contains
    out: list[dict[str, Any]] = []
    while True:
        data = _get_envelope(f"/zones/{zone}/dns_records", params)
        for rec in data.get("result") or []:
            out.append(
                {
                    "name": rec.get("name"),
                    "type": rec.get("type"),
                    "content": rec.get("content"),
                    "proxied": rec.get("proxied"),
                    "ttl": rec.get("ttl"),
                    "comment": rec.get("comment"),
                }
            )
        info = data.get("result_info") or {}
        total_pages = int(info.get("total_pages") or 1)
        if params["page"] >= total_pages or len(out) >= _MAX_DNS_RECORDS:
            break
        params["page"] += 1
    return out[:_MAX_DNS_RECORDS]


# 상한 168 = 7일 × 시간당 버킷. period_hours 를 이 안으로 clamp 해서 호출한다.
_MAX_ANALYTICS_HOURS = 168


def http_status_summary(period_hours: int = 24) -> dict[str, Any]:
    """직전 period_hours 동안의 엣지 HTTP 응답 상태코드 분포와 5xx 비율.

    Cloudflare GraphQL Analytics(httpRequests1hGroups)를 조회한다. 상태코드는 edge
    기준(Cloudflare 가 클라이언트에 돌려준 코드)이라 5xx 급증은 origin 장애/타임아웃
    신호다. 토큰에 Zone Analytics:Read 가 없으면 _graphql 이 errors 로 실패한다.
    """
    from datetime import datetime, timedelta, timezone

    zone = settings.cloudflare_zone_id()
    now = datetime.now(timezone.utc)
    # since 를 정시로 내려 해당 시간 버킷을 포함시킨다(버킷 datetime 은 정시 기준).
    since = (now - timedelta(hours=period_hours)).replace(
        minute=0, second=0, microsecond=0
    )

    def _iso(dt: "datetime") -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # zoneTag·시각은 서버 통제값이지만 안전하게 json.dumps 로 quoting 해 인라인한다.
    import json

    query = (
        "{ viewer { zones(filter: {zoneTag: %s}) { "
        "httpRequests1hGroups(limit: %d, filter: {datetime_geq: %s, datetime_lt: %s}) { "
        "sum { responseStatusMap { edgeResponseStatus requests } } } } } }"
    ) % (
        json.dumps(zone),
        _MAX_ANALYTICS_HOURS,
        json.dumps(_iso(since)),
        json.dumps(_iso(now)),
    )

    data = _graphql(query)
    zones = ((data.get("viewer") or {}).get("zones")) or []
    groups = (zones[0].get("httpRequests1hGroups") if zones else []) or []

    counts: dict[int, int] = {}
    for g in groups:
        for entry in (g.get("sum") or {}).get("responseStatusMap") or []:
            status = int(entry.get("edgeResponseStatus") or 0)
            counts[status] = counts.get(status, 0) + int(entry.get("requests") or 0)

    by_class = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0}
    for status, n in counts.items():
        bucket = f"{status // 100}xx"
        by_class[bucket if bucket in by_class else "other"] += n

    total = sum(counts.values())
    server_5xx = by_class["5xx"]
    rate_5xx = round(100.0 * server_5xx / total, 3) if total else 0.0
    status_5xx = {
        str(s): n for s, n in sorted(counts.items(), reverse=True) if 500 <= s <= 599
    }
    return {
        "period_hours": period_hours,
        "since": _iso(since),
        "until": _iso(now),
        "total_requests": total,
        "by_class": by_class,
        "requests_5xx": server_5xx,
        "rate_5xx_pct": rate_5xx,
        "status_5xx_breakdown": status_5xx,
    }


def list_waf_custom_rules() -> list[dict[str, Any]]:
    """커스텀 방화벽 phase 룰셋의 룰들을 {룰셋, 룰[]} 리스트로 전개한다.

    참고: /rulesets 목록은 cursor 페이지네이션이 있으나 여기서는 첫 페이지만
    읽는다 — zone 룰셋은 phase당 1개 수준이라 랩 규모에서 잘릴 일이 없다.
    """
    zone = settings.cloudflare_zone_id()
    rulesets = _get(f"/zones/{zone}/rulesets") or []
    out: list[dict[str, Any]] = []
    for rs in rulesets:
        if rs.get("phase") != _WAF_CUSTOM_PHASE:
            continue
        detail = _get(f"/zones/{zone}/rulesets/{rs.get('id')}") or {}
        out.append(
            {
                "ruleset": rs.get("name"),
                "phase": rs.get("phase"),
                "rules": [
                    {
                        "description": r.get("description"),
                        "expression": r.get("expression"),
                        "action": r.get("action"),
                        "enabled": r.get("enabled"),
                    }
                    for r in (detail.get("rules") or [])
                ],
            }
        )
    return out
