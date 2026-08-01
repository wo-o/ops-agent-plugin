"""GitHub READ 클라이언트 — PR 상태 + workflow-run 상태 조회만 한다.

원래 ops 플러그인의 GitHub 클라이언트보다 의도적으로 훨씬 좁다: 여기에는 브랜치/커밋/PR
열기/dispatch 코드가 전혀 없다. 랩 플러그인의 GitHub 표면은 read 엔드포인트 두 개뿐이다:

  · GET /repos/{repo}/pulls/{number}       (ops_github_get_pr_status)
  · GET /repos/{repo}/actions/runs/{id}    (ops_github_get_workflow_run)

App 이 설정돼 있으면 인증은 GitHub App installation 토큰(change 툴셋이 PR 을 열 때 쓰는 그
App)을 사용한다 — 그래서 "App only" 구성은 PAT 이 아예 필요 없다. App 이 설정돼 있지 않으면
GITHUB_TOKEN 의 read-only PAT 로 폴백한다. HTTP 는 표준 라이브러리(urllib)만 쓴다.
"""

from __future__ import annotations

from typing import Any

from . import env, http_request
from . import github_app

_API = "https://api.github.com"


def check_github() -> bool:
    """Visibility 게이트: GitHub App 설정됨 OR GITHUB_TOKEN 존재 (HTTP 는 stdlib)."""
    return bool(github_app.check_change() or env("GITHUB_TOKEN"))


def _token() -> str:
    """App installation 토큰을 우선 사용하고 (App-only 구성), 없으면 PAT 로 폴백한다."""
    if github_app.check_change():
        return github_app._installation_token()
    return env("GITHUB_TOKEN") or ""


class GitHubError(Exception):
    pass


def _get(path: str) -> Any:
    r = http_request(
        "GET",
        _API + path,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "ops-plugin",
        },
    )
    if r.status_code >= 400:
        raise GitHubError(f"GET {path} -> {r.status_code} {r.text[:200]}")
    return r.json()


def get_pr(repo: str, number: int) -> dict[str, Any]:
    return _get(f"/repos/{repo}/pulls/{number}")


def get_pr_reviews(repo: str, number: int) -> list[dict[str, Any]]:
    """제출된 리뷰 목록. requested_reviewers는 리뷰 제출 시점에 그 사람이 빠지므로,
    "리뷰어 미지정"과 "승인 완료·머지 대기"를 구분하려면 이 목록이 필요하다."""
    data = _get(f"/repos/{repo}/pulls/{number}/reviews?per_page=100")
    return data if isinstance(data, list) else []


def get_workflow_run(repo: str, run_id: int) -> dict[str, Any]:
    return _get(f"/repos/{repo}/actions/runs/{run_id}")


def get_workflow_runs_for_head(repo: str, head_sha: str) -> list[dict[str, Any]]:
    """특정 커밋에서 시작된 Actions run 목록을 반환한다.

    auto-merge가 ``workflow_dispatch``하는 ``tf-apply``는 PR URL에 run URL을 남기지 않는다.
    PR의 merge_commit_sha로 좁히면 동시 실행 중인 다른 apply를 잘못 연결하지 않고
    그 PR의 apply run URL을 에이전트에 돌려줄 수 있다.
    """
    data = _get(f"/repos/{repo}/actions/runs?head_sha={head_sha}&per_page=100")
    return list(data.get("workflow_runs", []))


def get_workflow_jobs(repo: str, run_id: int) -> list[dict[str, Any]]:
    """워크플로 run 의 job 목록을 반환한다(guard, plan 등).

    guard/plan 은 tf-plan 워크플로의 job 이다. 이걸 ``/commits/{sha}/check-runs`` 로 읽으면
    App 설치 토큰에 별도 ``Checks: read`` 권한이 필요해 "Resource not accessible by
    integration" 403 이 난다(2026-07-17 라이브 검증). Actions job API 는 apply run 조회와
    같은 ``Actions: read`` 권한이면 되므로 App-only 구성에서도 동작한다.
    """
    data = _get(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    return list(data.get("jobs", []))
