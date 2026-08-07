"""ansible toolset 테스트 — 순수 handler 계약만 검증, credential도 네트워크도 없다.

ansible 조치는 이제 로컬 SSH가 아니라 IaC 리포의 ansible-ops workflow를
workflow_dispatch로 트리거한다. 게이트(check_ansible_requirements)와 네트워크
클라이언트(http_request / github_app._headers)를 monkeypatch해서 실제 호출이 일어나지
않게 한 뒤, dispatch가 올바른 URL/inputs로 불리고 새 run URL을 돌려주는지 단위 테스트한다.
"""

import json

from ops_plugin import tools_ansible as A


def _d(s):
    return json.loads(s)


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _install_fake(monkeypatch, dispatch_status=204, new_run=True):
    """게이트를 열고 네트워크를 fake로 바꾼다. 호출 기록 리스트를 돌려준다."""
    calls = []

    def fake_http(method, url, headers, body=None, params=None, timeout=30.0):
        calls.append({"method": method, "url": url, "body": body, "params": params})
        if method == "POST" and "/dispatches" in url:
            return _Resp(dispatch_status, None, "")
        if method == "GET" and "/runs" in url:
            posted = any(c["method"] == "POST" for c in calls)
            runs = [{"id": 100, "html_url": "https://github.com/o/r/actions/runs/100"}]
            if posted and new_run:
                runs = [
                    {
                        "id": 101,
                        "html_url": "https://github.com/o/r/actions/runs/101",
                        "head_sha": "deadbeef101",
                    }
                ] + runs
            return _Resp(200, {"workflow_runs": runs}, "")
        return _Resp(200, {}, "")

    monkeypatch.setattr(A, "check_ansible_requirements", lambda: True)
    monkeypatch.setattr(A, "_repo", lambda: "o/r")
    monkeypatch.setattr(A, "http_request", fake_http)
    monkeypatch.setattr(A.github_app, "_headers", lambda: {"Authorization": "Bearer x"})
    monkeypatch.setattr(A.time, "sleep", lambda *a, **k: None)
    return calls


def _dispatch_call(calls):
    return next(c for c in calls if c["method"] == "POST" and "/dispatches" in c["url"])


def test_json_string_and_success_field():
    # 게이트 닫힘(기본 hermetic env) — 예외 없이 문자열 result.
    out = A.run_ansible_playbook({"playbook": "rolling-restart", "environment": "dev"})
    d = _d(out)
    assert isinstance(out, str) and "success" in d


def test_missing_creds_is_clean_error(monkeypatch):
    monkeypatch.setattr(A, "check_ansible_requirements", lambda: False)
    d = _d(
        A.run_ansible_playbook({"playbook": "rolling-restart", "environment": "dev"})
    )
    assert d["success"] is False and "remediation" in d


def test_unknown_playbook(monkeypatch):
    _install_fake(monkeypatch)
    d = _d(A.run_ansible_playbook({"playbook": "troublemaker", "environment": "dev"}))
    assert d["success"] is False and "unknown playbook" in d["error"]


def test_bad_environment(monkeypatch):
    _install_fake(monkeypatch)
    d = _d(A.run_ansible_playbook({"playbook": "disk-grow", "environment": "staging"}))
    assert d["success"] is False and "environment must be one of" in d["error"]


def test_dispatches_workflow_and_returns_run_url(monkeypatch):
    calls = _install_fake(monkeypatch)
    d = _d(
        A.run_ansible_playbook({"playbook": "rolling-restart", "environment": "prod"})
    )
    assert d["success"] is True
    assert d["run_url"].endswith("/runs/101")
    assert d["run_id"] == 101
    # head_sha + ref let the agent match the run to the merge commit (P4 stale-run guard)
    assert d["head_sha"] == "deadbeef101"
    assert d["ref"] == "main"
    # dispatch가 올바른 workflow 파일 + inputs로 불렸는지
    call = _dispatch_call(calls)
    assert call["url"].endswith("/actions/workflows/ansible-ops.yml/dispatches")
    assert call["body"]["ref"] == "main"
    assert call["body"]["inputs"]["playbook"] == "rolling-restart"
    assert call["body"]["inputs"]["environment"] == "prod"
    assert call["body"]["inputs"]["dry_run"] == "false"


def test_dry_run_maps_to_string_true(monkeypatch):
    calls = _install_fake(monkeypatch)
    d = _d(
        A.run_ansible_playbook(
            {"playbook": "disk-grow", "environment": "dev", "dry_run": True}
        )
    )
    assert d["success"] is True and d["dry_run"] is True
    assert _dispatch_call(calls)["body"]["inputs"]["dry_run"] == "true"


def test_dispatch_http_error_is_clean_fail(monkeypatch):
    _install_fake(monkeypatch, dispatch_status=422)
    d = _d(A.run_ansible_playbook({"playbook": "disk-grow", "environment": "dev"}))
    assert d["success"] is False and "workflow_dispatch failed" in d["error"]


def test_dispatch_404_reports_missing_workflow(monkeypatch):
    _install_fake(monkeypatch, dispatch_status=404)
    d = _d(A.run_ansible_playbook({"playbook": "disk-grow", "environment": "dev"}))
    assert d["success"] is False and "not found" in d["error"]


def test_run_not_yet_visible_returns_actions_url(monkeypatch):
    _install_fake(monkeypatch, new_run=False)
    d = _d(A.run_ansible_playbook({"playbook": "security-patch", "environment": "dev"}))
    assert d["success"] is True
    assert d["run_url"] is None
    assert d["actions_url"].endswith("/actions/workflows/ansible-ops.yml")


def test_param_not_in_catalog_is_rejected(monkeypatch):
    _install_fake(monkeypatch)
    d = _d(
        A.run_ansible_playbook(
            {
                "playbook": "rolling-restart",
                "environment": "dev",
                "params": {"reboot": "true"},
            }
        )
    )
    assert d["success"] is False and "not allowed" in d["error"]


def test_monitoring_agents_dispatches(monkeypatch):
    # 2026-07-19 slack-rca: runbook STEP 0(no-data)이 지시하는 monitoring-agents 배선.
    calls = _install_fake(monkeypatch)
    d = _d(
        A.run_ansible_playbook({"playbook": "monitoring-agents", "environment": "prod"})
    )
    assert d["success"] is True
    body = _dispatch_call(calls)["body"]
    assert body["inputs"]["playbook"] == "monitoring-agents"
    assert body["inputs"]["environment"] == "prod"


def test_rds_temp_user_dispatches_with_bounded_params(monkeypatch):
    # 2026-07-16 e2e C1-I6: 시나리오 3 prod가 요구하는 rds-temp-user 카탈로그 배선.
    calls = _install_fake(monkeypatch)
    d = _d(
        A.run_ansible_playbook(
            {
                "playbook": "rds-temp-user",
                "environment": "prod",
                "params": {
                    "temp_user": "jin_readonly",
                    "valid_until": "2026-07-16T15:00:00Z",
                    "grant_mode": "readonly",
                    "state": "present",
                },
            }
        )
    )
    assert d["success"] is True
    body = next(c["body"] for c in calls if c["method"] == "POST")
    assert body["inputs"]["temp_user"] == "jin_readonly"
    assert body["inputs"]["grant_mode"] == "readonly"
    # 2026-07-20 slack-rca: 비밀번호는 핸들러가 생성해 dispatch input + 응답으로
    # 전달한다(러너 생성+마스킹은 요청자 전달 경로가 없는 dead end였다).
    # 영숫자만 — SQL/셸 보간과 workflow의 공백 구분 -e 문자열에 안전해야 한다.
    import re

    assert re.fullmatch(r"[A-Za-z0-9]{24}", body["inputs"]["temp_password"])
    assert d["temp_password"] == body["inputs"]["temp_password"]


def test_rds_temp_user_absent_has_no_password(monkeypatch):
    # state=absent(DROP)는 비밀번호가 필요 없다 — 생성도 반환도 하지 않는다.
    calls = _install_fake(monkeypatch)
    d = _d(
        A.run_ansible_playbook(
            {
                "playbook": "rds-temp-user",
                "environment": "prod",
                "params": {
                    "temp_user": "jin_readonly",
                    "valid_until": "2026-07-16T15:00:00Z",
                    "state": "absent",
                },
            }
        )
    )
    assert d["success"] is True
    body = next(c["body"] for c in calls if c["method"] == "POST")
    assert "temp_password" not in body["inputs"]
    assert "temp_password" not in d


def test_rds_temp_user_rejects_sql_unsafe_username(monkeypatch):
    # temp_user는 psql SQL로 보간된다 — pattern 봉쇄가 인젝션 방지선.
    _install_fake(monkeypatch)
    d = _d(
        A.run_ansible_playbook(
            {
                "playbook": "rds-temp-user",
                "environment": "prod",
                "params": {
                    "temp_user": "x; DROP ROLE dbadmin",
                    "valid_until": "2026-07-16T15:00:00Z",
                },
            }
        )
    )
    assert d["success"] is False and "must match" in d["error"]


def test_rds_readonly_user_dispatches_on_dev(monkeypatch):
    # 2026-08-01 사용자 결정: dev 상시 readonly 계정은 bastion user_data가 아니라
    # dev 세팅 apply 후 이 playbook으로 만든다(멱등). 비밀번호는 강의 자료 데모
    # 고정값(러너 쪽 기본값) — dispatch input에도 응답에도 자격증명이 없다.
    calls = _install_fake(monkeypatch)
    d = _d(
        A.run_ansible_playbook({"playbook": "rds-readonly-user", "environment": "dev"})
    )
    assert d["success"] is True
    body = _dispatch_call(calls)["body"]
    assert body["ref"] == "dev"
    assert body["inputs"]["playbook"] == "rds-readonly-user"
    assert body["inputs"]["environment"] == "dev"
    assert "temp_password" not in body["inputs"]
    assert "temp_password" not in d


def test_rds_readonly_user_rejects_prod(monkeypatch):
    # prod 상시 계정 금지 계약 — 카탈로그 envs 한정이 dispatch 전에 막는다.
    calls = _install_fake(monkeypatch)
    d = _d(
        A.run_ansible_playbook({"playbook": "rds-readonly-user", "environment": "prod"})
    )
    assert d["success"] is False and "restricted to" in d["error"]
    assert not any(c["method"] == "POST" for c in calls)


def test_dev_dispatch_uses_dev_ref(monkeypatch):
    # 브랜치=환경(2026-07-20 dev IaC 개방): dev 실행은 dev 브랜치를 체크아웃해야
    # 에이전트가 dev에서 수정한 플레이북·패키지 목록이 dispatch에 반영된다.
    calls = _install_fake(monkeypatch)
    d = _d(A.run_ansible_playbook({"playbook": "security-patch", "environment": "dev"}))
    assert d["success"] is True
    call = _dispatch_call(calls)
    assert call["body"]["ref"] == "dev"
    # run 목록 조회도 같은 ref(branch)로 필터해야 전후 비교가 맞는다
    get = next(c for c in calls if c["method"] == "GET" and "/runs" in c["url"])
    assert get["params"]["branch"] == "dev"


def test_inflight_duplicate_run_is_reused(monkeypatch):
    # 알람 반복 발화 중 같은 playbook@env가 queued/in_progress면 재dispatch하지 않고
    # 그 run을 돌려준다 (2026-07-20 GH 장애 중 11건 적체의 근본 수정).
    calls = _install_fake(monkeypatch)
    monkeypatch.setattr(
        A,
        "_list_dispatch_runs",
        lambda repo, ref: [
            {
                "id": 1,
                "display_title": "ansible-ops: monitoring-agents @ dev",
                "status": "queued",
                "html_url": "https://github.com/o/r/actions/runs/1",
                "head_sha": "abc",
            }
        ],
    )
    d = _d(
        A.run_ansible_playbook({"playbook": "monitoring-agents", "environment": "dev"})
    )
    assert d["success"] is True and d["reused_run"] is True
    assert not any(c["method"] == "POST" for c in calls)  # dispatch 안 함


def test_disk_grow_reuses_push_auto_disk_grow(monkeypatch):
    # prod admin-merge는 진짜 push라 "auto-disk-grow @ push" run(event=push)이 뜬다.
    # 이건 workflow_dispatch 조회에 안 잡혀, 에이전트의 disk-grow @ prod dispatch가
    # 중복으로 나가 growpart no-op(changed=0)을 돌렸다(2026-07-20 C2-I2). disk-grow는
    # push 트리거 run도 같은 작업이므로 재사용한다.
    calls = _install_fake(monkeypatch)

    def fake_list_runs(repo, ref, event):
        if event == "push":
            return [
                {
                    "id": 900,
                    "display_title": "ansible-ops: auto-disk-grow @ push",
                    "status": "in_progress",
                    "html_url": "https://github.com/o/r/actions/runs/900",
                    "head_sha": "pushsha",
                }
            ]
        return []  # workflow_dispatch: 에이전트 disk-grow @ prod는 아직 없음

    monkeypatch.setattr(A, "_list_runs", fake_list_runs)
    d = _d(A.run_ansible_playbook({"playbook": "disk-grow", "environment": "prod"}))
    assert d["success"] is True and d["reused_run"] is True
    assert d["run_url"].endswith("/runs/900")
    assert not any(c["method"] == "POST" for c in calls)  # dispatch 안 함


def test_disk_grow_reuses_completed_auto_run_same_head(monkeypatch):
    # F3 (2026-08-03 C2): dev auto-merge가 dispatch한 "disk-grow @ dev" 자동 run이
    # 에이전트의 dispatch 시도(~2분 뒤)보다 먼저 success로 끝나면 queued/in_progress
    # 창을 벗어나, 에이전트가 이미 커진 볼륨에 growpart no-op을 중복 dispatch했다.
    # 현재 머지 head에 대해 이미 success인 자동 run은 head_sha로 상관해 재사용한다.
    calls = _install_fake(monkeypatch)
    monkeypatch.setattr(A, "_branch_head_sha", lambda repo, ref: "mergehead")

    def fake_list_dispatch(repo, ref):
        return [
            {
                "id": 700,
                "display_title": "ansible-ops: disk-grow @ dev",
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.com/o/r/actions/runs/700",
                "head_sha": "mergehead",
            }
        ]

    monkeypatch.setattr(A, "_list_dispatch_runs", fake_list_dispatch)
    monkeypatch.setattr(A, "_list_runs", lambda repo, ref, event: [])
    d = _d(A.run_ansible_playbook({"playbook": "disk-grow", "environment": "dev"}))
    assert d["success"] is True and d["reused_run"] is True
    assert d["run_url"].endswith("/runs/700")
    assert not any(c["method"] == "POST" for c in calls)  # 중복 dispatch 안 함


def test_disk_grow_dispatches_when_completed_run_is_stale_head(monkeypatch):
    # 다른 머지(head_sha 불일치)의 옛 성공 run은 재사용하지 않는다 —
    # 이번 요청은 실제로 dispatch돼야 한다(오래된 run 재사용으로 조용히 no-op 방지).
    calls = _install_fake(monkeypatch)
    monkeypatch.setattr(A, "_branch_head_sha", lambda repo, ref: "newhead")

    def fake_list_dispatch(repo, ref):
        posted = any(c["method"] == "POST" for c in calls)
        runs = [
            {
                "id": 600,
                "display_title": "ansible-ops: disk-grow @ dev",
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.com/o/r/actions/runs/600",
                "head_sha": "oldhead",
            }
        ]
        if posted:
            runs = [
                {
                    "id": 601,
                    "display_title": "ansible-ops: disk-grow @ dev",
                    "status": "queued",
                    "html_url": "https://github.com/o/r/actions/runs/601",
                    "head_sha": "newhead",
                }
            ] + runs
        return runs

    monkeypatch.setattr(A, "_list_dispatch_runs", fake_list_dispatch)
    monkeypatch.setattr(A, "_list_runs", lambda repo, ref, event: [])
    d = _d(A.run_ansible_playbook({"playbook": "disk-grow", "environment": "dev"}))
    assert d["success"] is True and d.get("reused_run") is not True
    assert any(c["method"] == "POST" for c in calls)  # 정상 dispatch


def test_non_disk_grow_ignores_push_runs(monkeypatch):
    # push 트리거 재사용은 disk-grow 전용이다 — 다른 playbook은 push run을 보지 않는다.
    calls = _install_fake(monkeypatch)

    def fake_list_runs(repo, ref, event):
        if event == "push":
            return [
                {
                    "id": 900,
                    "display_title": "ansible-ops: auto-disk-grow @ push",
                    "status": "in_progress",
                    "html_url": "u",
                    "head_sha": "s",
                }
            ]
        return []

    monkeypatch.setattr(A, "_list_runs", fake_list_runs)
    d = _d(
        A.run_ansible_playbook({"playbook": "security-patch", "environment": "prod"})
    )
    assert d["success"] is True and d.get("reused_run") is not True
    assert any(c["method"] == "POST" for c in calls)  # 정상 dispatch


def test_registered_dev_playbook_dispatches(monkeypatch):
    # 에이전트가 dev 코드 PR로 추가·등록한 조치 playbook은 빌트인이 아니어도
    # dev 매니페스트에 등록돼 있으면 dispatch된다(dev ref, params 없음).
    calls = _install_fake(monkeypatch)
    monkeypatch.setattr(
        A,
        "_fetch_registered_playbooks",
        lambda repo, ref: {"log-sweep-ssh-hardening": "로그 정리 + sshd 하드닝"},
    )
    d = _d(
        A.run_ansible_playbook(
            {"playbook": "log-sweep-ssh-hardening", "environment": "dev"}
        )
    )
    assert d["success"] is True
    body = _dispatch_call(calls)["body"]
    assert body["ref"] == "dev"
    assert body["inputs"]["playbook"] == "log-sweep-ssh-hardening"
    assert body["inputs"]["environment"] == "dev"


def test_registered_playbook_prod_requires_main_registration(monkeypatch):
    # allowlist는 환경 정본 브랜치 매니페스트 — dev에만 등록된 playbook의 prod
    # dispatch는 unknown(main 미등록)으로 거부되고 승격 PR 안내가 붙는다.
    calls = _install_fake(monkeypatch)
    monkeypatch.setattr(
        A,
        "_fetch_registered_playbooks",
        lambda repo, ref: {"log-sweep-ssh-hardening": "d"} if ref == "dev" else {},
    )
    d = _d(
        A.run_ansible_playbook(
            {"playbook": "log-sweep-ssh-hardening", "environment": "prod"}
        )
    )
    assert d["success"] is False and "unknown playbook" in d["error"]
    assert "승격 PR" in d["remediation"]
    assert not any(c["method"] == "POST" for c in calls)


def test_registered_playbook_promoted_runs_on_prod(monkeypatch):
    # dev→main 승격 PR이 머지돼 main 매니페스트에 등록되면 prod dispatch가
    # 허용된다(ref=main — 2026-08-07, 구 dev 전용 규칙 대체).
    calls = _install_fake(monkeypatch)
    monkeypatch.setattr(
        A,
        "_fetch_registered_playbooks",
        lambda repo, ref: {"log-sweep-ssh-hardening": "d"} if ref == "main" else {},
    )
    d = _d(
        A.run_ansible_playbook(
            {"playbook": "log-sweep-ssh-hardening", "environment": "prod"}
        )
    )
    assert d["success"] is True
    body = _dispatch_call(calls)["body"]
    assert body["ref"] == "main"
    assert body["inputs"]["environment"] == "prod"


def test_unregistered_non_builtin_is_unknown(monkeypatch):
    # 빌트인도 아니고 매니페스트에도 없는 이름은 여전히 unknown으로 거부된다.
    _install_fake(monkeypatch)
    monkeypatch.setattr(A, "_fetch_registered_playbooks", lambda repo, ref: {})
    d = _d(
        A.run_ansible_playbook({"playbook": "wipe-everything", "environment": "dev"})
    )
    assert d["success"] is False and "unknown playbook" in d["error"]


def test_rds_temp_user_never_dedups(monkeypatch):
    # 자격증명이 호출마다 달라 rds-temp-user는 in-flight가 있어도 새로 dispatch한다.
    calls = _install_fake(monkeypatch)
    monkeypatch.setattr(
        A,
        "_list_dispatch_runs",
        lambda repo, ref: [
            {
                "id": 1,
                "display_title": "ansible-ops: rds-temp-user @ prod",
                "status": "in_progress",
                "html_url": "u",
                "head_sha": "abc",
            }
        ],
    )
    d = _d(
        A.run_ansible_playbook(
            {
                "playbook": "rds-temp-user",
                "environment": "prod",
                "params": {
                    "temp_user": "jin_ro",
                    "valid_until": "2026-07-21T00:00:00Z",
                },
            }
        )
    )
    assert d["success"] is True and d.get("reused_run") is not True
    assert any(c["method"] == "POST" for c in calls)
