"""result 헬퍼 — Hermes 핸들러 계약.

Hermes 핸들러 계약은 이렇다: 성공이든 에러든 JSON 문자열을 반환하고, 절대
raise하지 않는다(모든 핸들러는 모델 없이 테스트 가능해야 한다). ok()/fail()이
result 봉투(envelope)를 씌운다:

    {"success": true,  ...}
    {"success": false, "error": "...", "remediation": "..."}
"""

from __future__ import annotations

import json
from typing import Any, Optional


def ok(**fields: Any) -> str:
    """{"success": true} 봉투를 붙인 JSON 문자열 형태의 성공 result."""
    return json.dumps({"success": True, **fields}, ensure_ascii=False)


def fail(error: Any, remediation: Optional[str] = None, **extra: Any) -> str:
    """JSON 문자열 형태의 에러 result. 절대 raise하지 않는다; 핸들러가 이를 잡아 반환한다."""
    body: dict[str, Any] = {"success": False, "error": str(error)}
    if remediation:
        body["remediation"] = remediation
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)
