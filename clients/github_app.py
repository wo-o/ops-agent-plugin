"""GitHub App WRITE 클라이언트 — 앱의 BOT 정체성으로 PR 을 연다.

상태 조회용 read-only PAT 클라이언트인 clients/github.py 와 달리, 이 클라이언트는 GitHub App
자격증명으로 수명이 짧은 INSTALLATION 토큰을 발급받아 브랜치를 만들고 파일을 커밋하고
PR 을 연다. 토큰이 앱 소유이기 때문에 PR 작성자는 사람이 아니라 `<app-slug>[bot]` 이 된다.

자격증명 (도구 인자가 아니라 env 로만; 절대 도구 인자에서 읽지 않는다):
  OPS_GITHUB_APP_ID              숫자 형식의 app id
  OPS_GITHUB_PRIVATE_KEY_PATH    앱 .pem 파일 경로  (권장)
    또는 OPS_GITHUB_PRIVATE_KEY   PEM 내용을 인라인으로
  OPS_GITHUB_INSTALLATION_ID     해당 repo 의 installation id
  OPS_GITHUB_REPO                 owner/name

HTTP 는 표준 라이브러리(urllib)만 쓴다. App JWT 는 GitHub 공식 방식대로 PyJWT 로
RS256 서명한다(호스트에 이미 설치됨). installation 토큰은 만료 약 1분 전까지 캐시된다.
"""

from __future__ import annotations

import calendar
import os
import time
from typing import Any, Optional

from . import env, http_request

_API = "https://api.github.com"
_token_cache: dict[str, Any] = {"token": None, "exp": 0}


def _private_key() -> Optional[str]:
    path = env("OPS_GITHUB_PRIVATE_KEY_PATH")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return env("OPS_GITHUB_PRIVATE_KEY")


def check_change() -> bool:
    """Visibility 게이트: 앱 자격증명이 모두 있고 PyJWT(RS256 서명) 가능할 것."""
    try:
        import importlib.util

        if importlib.util.find_spec("jwt") is None:
            return False
    except Exception:
        return False
    return bool(
        env("OPS_GITHUB_APP_ID")
        and env("OPS_GITHUB_INSTALLATION_ID")
        and env("OPS_GITHUB_REPO")
        and _private_key()
    )


class GitHubAppError(Exception):
    pass


def _app_jwt() -> str:
    # GitHub 공식 방식: PyJWT 로 RS256 서명(내부적으로 cryptography 사용).
    # clock skew 대비로 iat 를 60초 과거로 둔다(공식 문서 권장).
    import jwt  # PyJWT

    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": str(env("OPS_GITHUB_APP_ID"))},
        _private_key(),
        algorithm="RS256",
    )


def _installation_token() -> str:
    now = int(time.time())
    if _token_cache["token"] and now < _token_cache["exp"] - 60:
        return _token_cache["token"]
    inst = env("OPS_GITHUB_INSTALLATION_ID")
    r = http_request(
        "POST",
        f"{_API}/app/installations/{inst}/access_tokens",
        headers={
            "Authorization": f"Bearer {_app_jwt()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "ops-plugin",
        },
    )
    if r.status_code >= 400:
        raise GitHubAppError(f"installation token -> {r.status_code} {r.text[:200]}")
    data = r.json()
    _token_cache["token"] = data["token"]
    # expires_at은 ISO8601 UTC("...Z") — timegm은 UTC로 유지; mktime은 로컬
    # 시간으로 오해해 만료 시각이 호스트 UTC offset만큼 밀린다
    try:
        exp = calendar.timegm(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        exp = now + 3600
    _token_cache["exp"] = exp
    return data["token"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_installation_token()}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ops-plugin",
    }


def open_multi_file_pr(
    repo: str,
    files: dict[str, str],
    branch: str,
    title: str,
    body: str,
    base: str = "main",
) -> dict[str, Any]:
    """`base`에서 `branch`를 만들고, `files`(path -> content)를 각각 커밋한 뒤
    PR을 연다. Contents API 패턴을 파일 수만큼 반복한다(파일당 커밋 하나 —
    랩/데모에 충분). {number, html_url, author} 반환.

    호출자(write 도구)가 모든 path를 고정 surface 스펙 또는 검증된 feature
    슬러그로 직접 구성하므로 이 함수는 path를 신뢰한다 — 경로 확정은 상위에서 한다."""
    import base64

    if not files:
        raise GitHubAppError("open_multi_file_pr: no files given")

    h = _headers()
    r = http_request("GET", f"{_API}/repos/{repo}/git/ref/heads/{base}", headers=h)
    if r.status_code >= 400:
        raise GitHubAppError(f"get base ref -> {r.status_code} {r.text[:200]}")
    base_sha = r.json()["object"]["sha"]

    r = http_request(
        "POST",
        f"{_API}/repos/{repo}/git/refs",
        headers=h,
        body={"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    if r.status_code >= 400 and "Reference already exists" not in r.text:
        raise GitHubAppError(f"create branch -> {r.status_code} {r.text[:200]}")

    for path, content in files.items():
        existing_sha = None
        r = http_request(
            "GET", f"{_API}/repos/{repo}/contents/{path}?ref={branch}", headers=h
        )
        if r.status_code == 200:
            existing_sha = r.json().get("sha")
        # 파일이 여럿일 때만 커밋 메시지에 path를 붙여 구분한다
        message = f"{title} ({path})" if len(files) > 1 else title
        put = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if existing_sha:
            put["sha"] = existing_sha
        r = http_request(
            "PUT", f"{_API}/repos/{repo}/contents/{path}", headers=h, body=put
        )
        if r.status_code >= 400:
            raise GitHubAppError(f"put {path} -> {r.status_code} {r.text[:200]}")

    r = http_request(
        "POST",
        f"{_API}/repos/{repo}/pulls",
        headers=h,
        body={"title": title, "head": branch, "base": base, "body": body},
    )
    if r.status_code >= 400:
        raise GitHubAppError(f"open pr -> {r.status_code} {r.text[:200]}")
    pr = r.json()
    return {
        "number": pr["number"],
        "html_url": pr["html_url"],
        "author": (pr.get("user") or {}).get("login"),
    }


def create_snapshot_branch(
    repo: str,
    source_branch: str,
    base_branch: str,
    paths: list[str] | tuple[str, ...],
    branch: str,
    message: str,
) -> dict[str, Any]:
    """`base_branch` HEAD에서 새 브랜치를 만들고, `source_branch`의 `paths`
    (리포 루트 디렉터리 단위) 최종 상태만 담은 커밋 하나를 얹는다 — 승격 PR용.

    브랜치 전체를 head로 쓰는 PR과 달리 CODEOWNERS·워크플로 등 다른 파일이
    diff에 실리지 않고, base HEAD에서 방금 만든 브랜치라 머지 충돌이 구조적으로
    불가능하다(충돌 PR은 GitHub가 test merge commit을 못 만들어 tf-plan/guard가
    아예 안 돈다 — 2026-08-01 승격 실패의 원인).

    paths의 diff가 없으면 브랜치를 만들지 않고 {"changed": False}를 반환한다.
    반환: {"changed": True, "branch": branch, "sha": <commit sha>}"""
    h = _headers()

    def _head_and_tree(ref: str) -> tuple[str, str]:
        r = http_request("GET", f"{_API}/repos/{repo}/git/ref/heads/{ref}", headers=h)
        if r.status_code >= 400:
            raise GitHubAppError(f"get ref {ref} -> {r.status_code} {r.text[:200]}")
        head_sha = r.json()["object"]["sha"]
        r = http_request(
            "GET", f"{_API}/repos/{repo}/git/commits/{head_sha}", headers=h
        )
        if r.status_code >= 400:
            raise GitHubAppError(f"get commit {ref} -> {r.status_code} {r.text[:200]}")
        return head_sha, r.json()["tree"]["sha"]

    def _root_dir_shas(tree_sha: str) -> dict[str, str]:
        r = http_request("GET", f"{_API}/repos/{repo}/git/trees/{tree_sha}", headers=h)
        if r.status_code >= 400:
            raise GitHubAppError(f"get tree -> {r.status_code} {r.text[:200]}")
        return {
            e["path"]: e["sha"] for e in r.json().get("tree", []) if e["type"] == "tree"
        }

    base_head, base_tree = _head_and_tree(base_branch)
    _, src_tree = _head_and_tree(source_branch)
    base_dirs = _root_dir_shas(base_tree)
    src_dirs = _root_dir_shas(src_tree)

    # 디렉터리 단위 tree SHA 교체 — source의 서브트리를 통째로 가리키므로
    # 파일 추가·수정·삭제가 모두 반영된다. source에 없는 디렉터리는 승격 대상이
    # 아니다(디렉터리 자체의 삭제 승격은 다루지 않는다 — 랩 범위 밖).
    entries = [
        {"path": p, "mode": "040000", "type": "tree", "sha": src_dirs[p]}
        for p in paths
        if p in src_dirs and base_dirs.get(p) != src_dirs[p]
    ]
    if not entries:
        return {"changed": False}

    r = http_request(
        "POST",
        f"{_API}/repos/{repo}/git/trees",
        headers=h,
        body={"base_tree": base_tree, "tree": entries},
    )
    if r.status_code >= 400:
        raise GitHubAppError(f"create tree -> {r.status_code} {r.text[:200]}")
    new_tree = r.json()["sha"]

    r = http_request(
        "POST",
        f"{_API}/repos/{repo}/git/commits",
        headers=h,
        body={"message": message, "tree": new_tree, "parents": [base_head]},
    )
    if r.status_code >= 400:
        raise GitHubAppError(f"create commit -> {r.status_code} {r.text[:200]}")
    commit_sha = r.json()["sha"]

    r = http_request(
        "POST",
        f"{_API}/repos/{repo}/git/refs",
        headers=h,
        body={"ref": f"refs/heads/{branch}", "sha": commit_sha},
    )
    if r.status_code >= 400:
        raise GitHubAppError(
            f"create branch {branch} -> {r.status_code} {r.text[:300]}"
        )
    return {"changed": True, "branch": branch, "sha": commit_sha}


def open_branch_pr(
    repo: str, head: str, base: str, title: str, body: str
) -> dict[str, Any]:
    """기존 브랜치 `head`를 `base`로 올리는 PR 하나를 연다 — 커밋 저작 없음.
    {number, html_url, author} 반환.
    diff 없음/이미 열림 등 422는 그대로 GitHubAppError로 올려 호출자가 분기한다."""
    r = http_request(
        "POST",
        f"{_API}/repos/{repo}/pulls",
        headers=_headers(),
        body={"title": title, "head": head, "base": base, "body": body},
    )
    if r.status_code >= 400:
        raise GitHubAppError(
            f"open pr {head}->{base} -> {r.status_code} {r.text[:300]}"
        )
    pr = r.json()
    return {
        "number": pr["number"],
        "html_url": pr["html_url"],
        "author": (pr.get("user") or {}).get("login"),
    }


def find_open_pr(repo: str, head: str, base: str) -> Optional[dict[str, Any]]:
    """head→base로 이미 열린 PR을 찾는다(없으면 None) — 승격 PR 중복 방지용."""
    owner = repo.split("/")[0]
    r = http_request(
        "GET",
        f"{_API}/repos/{repo}/pulls?state=open&head={owner}:{head}&base={base}",
        headers=_headers(),
    )
    if r.status_code >= 400:
        raise GitHubAppError(
            f"list prs {head}->{base} -> {r.status_code} {r.text[:200]}"
        )
    prs = r.json()
    if not prs:
        return None
    pr = prs[0]
    return {
        "number": pr["number"],
        "html_url": pr["html_url"],
        "author": (pr.get("user") or {}).get("login"),
    }


def find_open_pr_by_head_prefix(
    repo: str, head_prefix: str, base: str
) -> Optional[dict[str, Any]]:
    """head 브랜치명이 `head_prefix`로 시작하는 열린 PR을 찾는다(없으면 None) —
    스냅샷 브랜치명이 매번 달라지는 승격 PR의 중복 방지용."""
    r = http_request(
        "GET",
        f"{_API}/repos/{repo}/pulls?state=open&base={base}&per_page=100",
        headers=_headers(),
    )
    if r.status_code >= 400:
        raise GitHubAppError(f"list prs base={base} -> {r.status_code} {r.text[:200]}")
    for pr in r.json():
        if ((pr.get("head") or {}).get("ref") or "").startswith(head_prefix):
            return {
                "number": pr["number"],
                "html_url": pr["html_url"],
                "author": (pr.get("user") or {}).get("login"),
                "head": pr["head"]["ref"],
            }
    return None
