"""dev 코드 PR 도구 테스트 (2026-07-20 개방) — 순수 handler 계약만 검증.

경계 검증이 핵심이다: 코드 PR은 dev 브랜치의 2-1-dev/·modules/·ansible/에만 열리고,
.github/·scripts/·2-0-setup/·2-2-prod/·CODEOWNERS·packages surface 파일은 경로
단계에서 거부된다. 네트워크는 전부 monkeypatch — 실제 GitHub 호출 없음.
"""

import base64
import json

from ops_plugin import tools_change as C


def _d(s):
    return json.loads(s)


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _gate_open(monkeypatch):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(C, "_repo", lambda: "wo-o/ops-agent-iac")


def _capture_open_pr(monkeypatch):
    captured = {}

    def fake_open(repo, files, branch, title, body, base="main"):
        captured.update(
            repo=repo, files=files, branch=branch, title=title, body=body, base=base
        )
        return {"number": 77, "html_url": "https://x/pull/77", "author": "b[bot]"}

    monkeypatch.setattr(C.github_app, "open_multi_file_pr", fake_open)
    return captured


# ---------------------------------------------------------------- open_code_pr


def test_gate_closed_is_clean_error():
    d = _d(C.open_code_pr({"files": {"modules/service/x.tf": "a"}, "title": "t"}))
    assert d["success"] is False and "remediation" in d


def test_rejects_paths_outside_dev_writable_set(monkeypatch):
    _gate_open(monkeypatch)
    for bad in (
        "2-2-prod/main.tf",
        ".github/workflows/tf-plan.yml",
        ".github/CODEOWNERS",
        "scripts/expire_access.py",
        "2-0-setup/1-foundation/main.tf",
    ):
        d = _d(C.open_code_pr({"files": {bad: "content"}, "title": "t", "reason": "r"}))
        assert d["success"] is False, bad
        assert "outside the dev-writable set" in d["error"], bad


def test_rejects_path_traversal(monkeypatch):
    _gate_open(monkeypatch)
    d = _d(
        C.open_code_pr(
            {
                "files": {"modules/../.github/CODEOWNERS": "x"},
                "title": "t",
                "reason": "r",
            }
        )
    )
    assert d["success"] is False and "traversal" in d["error"]


def test_rejects_packages_surface_file(monkeypatch):
    # 전용 surface가 있는 파일은 코드 PR로 못 건드린다(경로 이원화 방지).
    _gate_open(monkeypatch)
    d = _d(
        C.open_code_pr(
            {
                "files": {"ansible/patch-extra-packages.yml": "extra_packages: []\n"},
                "title": "t",
                "reason": "r",
            }
        )
    )
    assert d["success"] is False and "dedicated surface" in d["error"]


def test_opens_pr_on_dev_with_feat_title(monkeypatch):
    _gate_open(monkeypatch)
    captured = _capture_open_pr(monkeypatch)
    d = _d(
        C.open_code_pr(
            {
                "files": {
                    "modules/service/cache.tf": 'resource "aws_elasticache_x" "c" {}\n',
                    "2-1-dev/cache.auto.tfvars": "cache_enabled = true\n",
                },
                "title": "dev 캐시 추가",
                "reason": "dev에 캐시 붙여줘",
            }
        )
    )
    assert d["success"] is True
    assert captured["base"] == "dev"
    assert captured["branch"].startswith("ops/agent-code-")
    assert captured["title"].startswith("feat(dev): ")
    assert set(captured["files"]) == {
        "modules/service/cache.tf",
        "2-1-dev/cache.auto.tfvars",
    }
    # terraform 경로 포함 → followup이 apply run 확인을 지시한다
    assert "tf-apply" in d["followup"]
    # prod 코드 PR 금지가 followup에 명시된다
    assert "promotion" in d["followup"]


def test_ansible_only_pr_has_no_apply_followup(monkeypatch):
    _gate_open(monkeypatch)
    _capture_open_pr(monkeypatch)
    d = _d(
        C.open_code_pr(
            {
                "files": {"ansible/log-rotate.yml": "---\n- hosts: role_app\n"},
                "title": "로그 로테이션 플레이북",
                "reason": "로그 정리 자동화",
            }
        )
    )
    assert d["success"] is True
    assert "NO tf-apply" in d["followup"]


def test_requires_title_and_nonempty_files(monkeypatch):
    _gate_open(monkeypatch)
    d = _d(C.open_code_pr({"files": {}, "title": "t", "reason": "r"}))
    assert d["success"] is False
    d = _d(C.open_code_pr({"files": {"modules/a.tf": "x"}, "title": "", "reason": "r"}))
    assert d["success"] is False and "title" in d["error"]
    d = _d(
        C.open_code_pr({"files": {"modules/a.tf": "   "}, "title": "t", "reason": "r"})
    )
    assert d["success"] is False and "non-empty" in d["error"]


# ---------------------------------------------------------------- read_repo_file


def test_read_file_rejects_bad_ref_and_traversal(monkeypatch):
    _gate_open(monkeypatch)
    d = _d(C.read_repo_file({"path": "modules/service/main.tf", "ref": "prod"}))
    assert d["success"] is False and "ref must be" in d["error"]
    d = _d(C.read_repo_file({"path": "../etc/passwd"}))
    assert d["success"] is False and "invalid path" in d["error"]


def test_read_file_returns_content_and_missing_flag(monkeypatch):
    _gate_open(monkeypatch)
    content = 'resource "aws_instance" "app" {}\n'

    def fake_http(method, url, headers, body=None, params=None, timeout=30.0):
        if "missing.tf" in url:
            return _Resp(404)
        payload = {"content": base64.b64encode(content.encode()).decode()}
        return _Resp(200, payload)

    monkeypatch.setattr(C, "http_request", fake_http)
    monkeypatch.setattr(C.github_app, "_headers", lambda: {"Authorization": "Bearer x"})
    d = _d(C.read_repo_file({"path": "modules/service/app.tf"}))
    assert d["success"] is True and d["content"] == content and d["ref"] == "dev"
    d = _d(C.read_repo_file({"path": "modules/missing.tf"}))
    assert d["success"] is True and d["exists"] is False


# ---------------------------------------------------------------- packages env-keying


def test_dev_packages_surface_targets_dev_branch(monkeypatch):
    # 브랜치=환경: dev-packages는 dev 브랜치에 PR — security-patch dispatch(ref=dev)가
    # 같은 브랜치를 체크아웃해야 목록이 반영된다.
    _gate_open(monkeypatch)
    monkeypatch.setattr(
        C, "_fetch_current", lambda path, ref="main": {"_raw": "extra_packages: []\n"}
    )
    captured = _capture_open_pr(monkeypatch)
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-packages",
                "op": "set_value",
                "value": ["fail2ban"],
                "reason": "dev 보안 패치에 fail2ban",
            }
        )
    )
    assert d["success"] is True
    assert captured["base"] == "dev"
    assert "- fail2ban\n" in captured["files"]["ansible/patch-extra-packages.yml"]


def test_legacy_packages_alias_still_targets_main(monkeypatch):
    # 하위호환: 옛 이름 "packages"는 prod 몫(base=main)으로 동작한다.
    _gate_open(monkeypatch)
    monkeypatch.setattr(
        C, "_fetch_current", lambda path, ref="main": {"_raw": "extra_packages: []\n"}
    )
    captured = _capture_open_pr(monkeypatch)
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "packages",
                "op": "set_value",
                "value": ["auditd"],
                "reason": "prod 보안 패치에 auditd",
            }
        )
    )
    assert d["success"] is True and captured["base"] == "main"
