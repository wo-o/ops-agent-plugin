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
