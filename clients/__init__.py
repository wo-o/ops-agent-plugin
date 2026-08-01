"""ops 플러그인이 사용하는 외부 클라이언트 모음.

여기의 모든 클라이언트는:
  · 자격증명을 도구 인자가 아니라 env 에서 직접 읽는다 (도구 인자에서 읽는 일은 절대 없다);
  · boto3 같은 무거운 SDK 는 지연 import 해서, 설치돼 있지 않아도
    플러그인 import 와 유닛테스트가 동작하도록 한다;
  · 도구의 check_fn 으로 쓰이는 check_*() -> bool 가용성 프로브를 노출한다
    (안전 게이트가 아니라 노출 여부를 결정하는 visibility 게이트다);
  · read-only / 범위 제한 동작을 코드로 강제한다 (실제 경계는 서버 측에서 assume 한
    역할에 걸린 read-only IAM 정책이다).

자격증명/의존성이 없으면 => 그 도구는 애초에 모델에 노출되지 않으며(check_fn False),
그래도 호출되면 핸들러가 깔끔한 에러 JSON 을 반환한다.
"""

from __future__ import annotations

import os
from typing import Optional


def env(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v if v else None


# --- 표준 라이브러리 기반 HTTP (httpx 의존성 제거) --------------------------------
# GitHub API 호출은 몇 개뿐이라 urllib 로 충분하다. 외부 패키지 없이 동작한다.
class HttpError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"HTTP {status}: {text[:200]}")
        self.status = status
        self.text = text


class HttpResponse:
    def __init__(self, status: int, body: bytes):
        self.status_code = status
        self._body = body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", "replace")

    def json(self):
        import json

        return json.loads(self._body or b"null")


def http_request(
    method: str, url: str, headers: dict, body=None, params=None, timeout: float = 30.0
) -> "HttpResponse":
    """단일 요청. body 는 dict(→JSON) 또는 None. params 는 dict(→쿼리스트링) 또는 None.
    4xx/5xx 도 예외 없이 응답으로 반환한다(호출부가 status_code 로 분기 — httpx 시맨틱과 동일)."""
    import json
    import urllib.request
    import urllib.error
    import urllib.parse

    if params:
        # doseq=True: list 값을 repeated 파라미터로 인코딩한다
        # (예: statuses[]=triggered&statuses[]=acknowledged). 없으면 list가
        # Python repr로 인코딩돼 PagerDuty가 단일 status로 오인, 400을 반환한다.
        url = (
            url
            + ("&" if "?" in url else "?")
            + urllib.parse.urlencode(params, doseq=True)
        )
    data = None
    hdrs = dict(headers)
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return HttpResponse(r.status, r.read())
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code, e.read())
