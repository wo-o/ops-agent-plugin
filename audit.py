"""감사 추적(audit trail, post_tool_call 훅).

MCP 서버는 표준 감사 로그를 내보내지 않으므로, 모든 도구 호출을 post_tool_call에서
직접 기록한다: 도구명, args, 소요시간, status, correlation ID — append-only
JSONL 한 줄로. args는 원문 기록한다(시크릿은 설계상 args로 전달되지 않음 —
settings.py 참조); 긴 문자열 값만 500자에서 자른다.

이 훅은 "agent-layer audit" 주제를 위한 Day 2 guardrail 성격의 산출물이다:
학생들은 도구를 돌리면서 `tail -f state/audit.jsonl`로 관찰할 수 있다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import settings

_MAX_ARG_CHARS = 500


def _audit_path():
    return settings.STATE_DIR() / "audit.jsonl"


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_ARG_CHARS:
        return value[:_MAX_ARG_CHARS] + f"...(truncated at {_MAX_ARG_CHARS} chars)"
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v) for v in value]
    return value


def _args_record(args: Any) -> Any:
    """args 원문을 JSON 직렬화 가능한 형태로(default=str), 긴 문자열 값만 잘라서."""
    try:
        safe = json.loads(json.dumps(args, ensure_ascii=False, default=str))
    except Exception:
        safe = str(args)
    return _truncate(safe)


def _correlation_id(kwargs: dict[str, Any]) -> str:
    args = kwargs.get("args") or kwargs.get("arguments") or {}
    if isinstance(args, dict):
        for key in ("correlation_id", "request_id", "incident_id"):
            if args.get(key):
                return str(args[key])
    return kwargs.get("session_id") or kwargs.get("task_id") or "-"


def record_tool_call(**kwargs: Any) -> None:
    """post_tool_call 훅. 관찰 전용: 절대 raise하지 않고, 반환값은 무시된다.

    정확한 훅 payload 키는 프레임워크 버전에 따라 달라지므로 **kwargs를 받는다;
    흔한 키들을 탐색하고 없으면 "-"로 대체한다.
    """
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": kwargs.get("tool_name") or kwargs.get("name") or "-",
            "args": _args_record(kwargs.get("args") or kwargs.get("arguments") or {}),
            "duration_ms": kwargs.get("duration_ms") or kwargs.get("duration"),
            "status": kwargs.get("status")
            or ("error" if kwargs.get("error") else "ok"),
            "correlation_id": _correlation_id(kwargs),
        }
        with _audit_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        # 감사는 절대 에이전트를 망가뜨려선 안 된다
        return
