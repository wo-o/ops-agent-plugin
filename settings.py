"""플러그인 설정 + 데이터 파일 로딩.

시크릿이 아닌 설정의 출처:
  1. env 변수(OPS_*, OPS_GRAFANA_URL) — 우선순위 최상, 설치 시점에 설정
  2. data/에 함께 배포되는 YAML 데이터 파일 — 템플릿 기본값

시크릿(토큰)은 여기서 절대 읽지 않는다; 클라이언트가 직접 env에서 읽는다
(도구 args에서는 절대 아님). 자격증명이 필요한 각 도구의 check_fn이 그 도구가
모델에 노출될지 여부까지 게이트한다.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - yaml은 프레임워크 의존성이라 런타임에 존재한다
    yaml = None


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v else default


PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"


def _state_dir() -> Path:
    """감사 로그가 있는 위치. 재정의 가능; 기본값은 플러그인 디렉토리 아래라
    (gitignore된) 호스트 체크아웃과 함께 따라다닌다."""
    override = _env("OPS_STATE_DIR") or None
    base = Path(override) if override else (PLUGIN_DIR / "state")
    base.mkdir(parents=True, exist_ok=True)
    return base


STATE_DIR = _state_dir


@lru_cache(maxsize=None)
def _load_yaml(name: str) -> dict[str, Any]:
    path = DATA_DIR / name
    if yaml is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def named_queries() -> dict[str, Any]:
    return _load_yaml("named-queries.yaml")


# --- AWS (읽기 전용) ---------------------------------------------------------------
def aws_read_role_arn() -> str | None:
    """assume할 read-only 롤의 전체 ARN(2-0-setup의 <project>-hermes-readonly,
    기본 프로젝트 이름이면 ops-agent-iac-hermes-readonly).
    None(미설정) => AWS 도구는 노출되지 않는다. 플러그인은 raw 로컬 자격증명으로
    AWS를 절대 호출하지 않는다 — 세션은 오직 이 롤을 assume해서만 생성된다."""
    arn = _env("OPS_AWS_READ_ROLE").strip()
    return arn or None


def aws_region(default: str = "ap-northeast-2") -> str:
    return _env("OPS_AWS_REGION", default)


def project_prefix(default: str = "ops-agent-iac") -> str:
    """IaC 리포의 프로젝트 이름 — 모든 리소스 이름의 프리픽스(<project>-...).
    IaC 리포 bootstrap.sh가 정하는 값(기본: 리포 디렉토리 이름)과 같아야 한다.
    unused-candidates 스캔의 SG/IAM 이름 필터가 이 값을 쓴다."""
    return _env("OPS_PROJECT_PREFIX", default)


# --- 엔드포인트 접근자 (non-secret) -----------------------------------------------
def grafana_url() -> str | None:
    """모니터링 서버 Grafana의 base URL(예: http://<ip>:3000)."""
    return os.getenv("OPS_GRAFANA_URL")


def grafana_public_url() -> str | None:
    """사용자에게 인용하는 dashboard URL의 base(예: http://<public-ip>:3000).
    쿼리는 OPS_GRAFANA_URL(같은 VPC private IP)로 나가지만, 그 주소는 수강생
    브라우저에서 열리지 않는다 — 표시용 base만 이 값(1-foundation의 grafana_url
    output)으로 분리한다. 미설정이면 OPS_GRAFANA_URL로 폴백."""
    return os.getenv("OPS_GRAFANA_PUBLIC_URL") or grafana_url()


def github_repo() -> str | None:
    """PR / 런 URL 확인용 선택적 owner/name 기본값(참고용)."""
    return _env("OPS_GITHUB_REPO") or None


def cloudflare_zone_id() -> str | None:
    """Cloudflare zone의 ID(non-secret). 미설정 => Cloudflare read 도구는
    노출되지 않는다. read 토큰(OPS_CLOUDFLARE_READ_TOKEN)은 클라이언트가 env에서 읽는다."""
    return _env("OPS_CLOUDFLARE_ZONE_ID") or None


# --- ansible (bounded 실행 경로, 2-3 incident-response) ---------------------------
# ansible 조치는 이 호스트에서 직접 SSH로 돌리지 않고 IaC 리포의 ansible-ops workflow를
# workflow_dispatch로 트리거한다(실제 실행은 VPC 안 self-hosted 러너). 그래서 여기엔 로컬
# 리포 경로/SSH 키 설정이 없다 — dispatch는 change 경로와 같은 GitHub App 자격증명
# (clients.github_app) + github_repo()를 쓴다(tools_ansible.check_ansible_requirements 참고).
