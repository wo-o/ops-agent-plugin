"""change toolset 테스트 — 순수 handler 계약만 검증, credential도 네트워크도 없다.

이 도구들은 GitHub App credential(check_change_requirements)로 게이팅된다. 테스트 환경에서
credential이 설정되지 않은 상태에서는, 모든 handler가 remediation을 담은 깔끔한
{"success": false} JSON을 반환해야 한다 — 절대 예외를 던지거나 네트워크를 건드리면 안 된다.
아울러 게이트와 네트워크 클라이언트를 monkeypatch해서 실제 호출이 일어나지 않게 한 뒤,
structured-content 검증(CIDR, expiry, caps)을 단위 테스트한다.
"""

import json

import pytest

from ops_plugin import tools_change as C
from ops_plugin.schemas import OPS_OPEN_TFVARS_PR


def _d(s):
    return json.loads(s)


def test_json_string_and_success_field():
    out = C.open_tfvars_pr({"surface": "dev-ec2-ssh", "op": "set_entry"})
    d = _d(out)
    assert isinstance(out, str) and "success" in d


def test_ec2_ssh_schema_explicitly_allows_single_ip_cidr():
    entry_description = OPS_OPEN_TFVARS_PR["parameters"]["properties"]["entry"][
        "description"
    ]
    assert "203.0.113.10/32" in entry_description
    assert "single-IP /32 allowed" in entry_description


def test_missing_creds_is_clean_error(monkeypatch):
    # host env와 상관없이 게이트가 닫혀 있도록 보장한다
    monkeypatch.setattr(C, "check_change_requirements", lambda: False)
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-ec2-ssh",
                "op": "set_entry",
                "entry_key": "x",
                "entry": {"cidr": "10.0.0.0/24"},
            }
        )
    )
    assert d["success"] is False and "remediation" in d


def test_unknown_surface(monkeypatch):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    d = _d(C.open_tfvars_pr({"surface": "99-nope", "op": "set_value", "value": 1}))
    assert d["success"] is False and "unknown surface" in d["error"]


@pytest.mark.parametrize("cidr", ["203.0.113.0/24", "203.0.113.10/32"])
def test_cidr_validation_accepts_ssh_prefix_boundaries(cidr):
    assert C._validate_cidr(cidr) is None


@pytest.mark.parametrize("cidr", ["203.0.112.0/23", "2001:db8::/64"])
def test_cidr_validation_rejects_prefixes_outside_ssh_range(cidr):
    assert C._validate_cidr(cidr) is not None


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "10.0.0.0/8", "::/0"])
def test_rejects_broad_cidr(monkeypatch, cidr):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C, "_fetch_current", lambda path, ref="main": {"ec2_ssh_allowlist": {}}
    )
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-ec2-ssh",
                "op": "set_entry",
                "entry_key": "bad",
                "entry": {"cidr": cidr, "ports": [22]},
            }
        )
    )
    assert d["success"] is False


def test_access_requires_expiry(monkeypatch):
    # prod db-access는 expires_at 필수(access-expiry cron 회수). dev는 상시 접근이라
    # 만료 생략 가능 — 그래서 이 검사는 prod surface로 한다.
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(C, "_fetch_current", lambda path, ref="main": {"db_grants": {}})
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "prod-db-access",
                "op": "set_entry",
                "entry_key": "g",
                "entry": {"cidr": "203.0.113.0/24", "target": "db"},
            }
        )
    )
    assert d["success"] is False and "expires_at" in d["error"]


def test_dns_requires_content(monkeypatch):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C, "_fetch_current", lambda path, ref="main": {"dns_records": {}}
    )
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-dns",
                "op": "set_entry",
                "entry_key": "r",
                "entry": {"name": "x.example.com", "type": "A", "value": "1.2.3.4"},
            }
        )
    )
    assert d["success"] is False and "content" in d["error"]


def test_dns_duplicate_name_type_rejected(monkeypatch):
    # 같은 name+type 레코드를 다른 key로 추가하면 apply에서 Cloudflare가 거부하고
    # 브랜치↔클라우드 drift가 남는다 (2026-07-16 PR #102) — 도구 레벨에서 막는다.
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C,
        "_fetch_current",
        lambda path, ref="main": {
            "dns_records": {
                "existing": {
                    "name": "fc.example.com",
                    "type": "CNAME",
                    "content": "alb.example.com",
                }
            }
        },
    )
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-dns",
                "op": "set_entry",
                "entry_key": "another-key",
                "entry": {
                    "name": "fc.example.com",
                    "type": "CNAME",
                    "content": "alb.example.com",
                },
            }
        )
    )
    assert d["success"] is False and "existing" in d["error"]


def test_db_grant_duplicate_cidr_under_different_key_rejected(monkeypatch):
    # 만료 전 기존 grant가 살아있는데 같은 CIDR을 새 key로 재요청하면 module의
    # db_grants validation이 중복 CIDR을 거부해 plan(guard)이 실패한다 (2026-07-16
    # e2e #135) — ec2-ssh/DNS와 같은 패턴으로 도구 레벨에서 먼저 막는다.
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C,
        "_fetch_current",
        lambda path, ref="main": {
            "db_grants": {
                "existing": {
                    "cidr": "203.0.113.10/32",
                    "expires_at": "2026-07-20T00:00:00Z",
                }
            }
        },
    )
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "prod-db-access",
                "op": "set_entry",
                "entry_key": "another-key",
                "entry": {
                    "cidr": "203.0.113.10/32",
                    "expires_at": "2026-07-21T00:00:00Z",
                },
            }
        )
    )
    assert d["success"] is False and "existing" in d["error"]


def test_waf_requires_ip(monkeypatch):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(C, "_fetch_current", lambda path, ref="main": {"waf_rules": {}})
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "waf",
                "op": "set_entry",
                "entry_key": "r",
                "entry": {"action": "block"},
            }
        )
    )
    assert d["success"] is False and "ip" in d["error"]


def test_volume_cap_is_1_to_100_not_0_to_2(monkeypatch):
    # disk-grow의 volume surface는 1..100 GiB로 캡한다(용량의 0..2가 아님).
    # write 데모("20GB로 키워줘")가 잘못된 0..2 캡에 막히지 않아야 한다.
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    over = _d(
        C.open_tfvars_pr({"surface": "dev-disk", "op": "set_value", "value": 200})
    )
    assert over["success"] is False and "1..100" in over["error"]
    zero = _d(C.open_tfvars_pr({"surface": "dev-disk", "op": "set_value", "value": 0}))
    assert zero["success"] is False and "1..100" in zero["error"]


def test_volume_grow_only_rejects_shrink(monkeypatch):
    # grow-only: 범위(1..100)는 통과하지만 현재 값(14)보다 작은 요청은
    # 축소=데이터 유실이라 거부한다. 범위 캡과 달리 현재 값과 비교가 필요하다.
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C,
        "_fetch_current",
        lambda path, ref="main": {"_raw": "data_volume_size_gb = 14\n"},
    )
    d = _d(C.open_tfvars_pr({"surface": "dev-disk", "op": "set_value", "value": 10}))
    assert d["success"] is False and "shrink" in d["error"]


def test_happy_path_opens_pr(monkeypatch):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C, "_fetch_current", lambda path, ref="main": {"ec2_ssh_allowlist": {}}
    )
    monkeypatch.setattr(C, "_repo", lambda: "wo-o/ops-agent-iac")
    captured = {}

    def fake_open(repo, files, branch, title, body, base="main"):
        captured.update(repo=repo, files=files, branch=branch, base=base)
        return {
            "number": 42,
            "html_url": "https://github.com/wo-o/ops-agent-iac/pull/42",
            "author": "ops-agent-iac-wo-o[bot]",
        }

    monkeypatch.setattr(C.github_app, "open_multi_file_pr", fake_open)
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-ec2-ssh",
                "op": "set_entry",
                "entry_key": "office-ssh",
                "entry": {"cidr": "203.0.113.0/24", "ports": [22], "description": "ci"},
                "reason": "슬랙: office IP ssh 열어줘",
            }
        )
    )
    assert d["success"] is True
    assert d["author"].endswith("[bot]")
    # 정확히 하나의 surface 파일, entry가 병합된 유효한 JSON
    assert list(captured["files"]) == ["2-1-dev/ec2-ssh.auto.tfvars.json"]
    body = json.loads(captured["files"]["2-1-dev/ec2-ssh.auto.tfvars.json"])
    assert body["ec2_ssh_allowlist"]["office-ssh"]["cidr"] == "203.0.113.0/24"
    # 브랜치=환경 매핑: dev surface PR의 base는 dev 브랜치다.
    assert captured["base"] == "dev"


def test_prod_surface_pr_base_is_main(monkeypatch):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C, "_fetch_current", lambda path, ref="main": {"ec2_ssh_allowlist": {}}
    )
    monkeypatch.setattr(C, "_repo", lambda: "wo-o/ops-agent-iac")
    captured = {}

    def fake_open(repo, files, branch, title, body, base="main"):
        captured.update(base=base)
        return {"number": 43, "html_url": "https://x/pull/43", "author": "b[bot]"}

    monkeypatch.setattr(C.github_app, "open_multi_file_pr", fake_open)
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "prod-ec2-ssh",
                "op": "set_entry",
                "entry_key": "office-ssh",
                "entry": {"cidr": "203.0.113.0/24", "ports": [22], "description": "ci"},
                "reason": "prod ssh",
            }
        )
    )
    assert d["success"] is True
    # 브랜치=환경 매핑: prod surface PR의 base는 main이다.
    assert captured["base"] == "main"


# --- 브랜치 prefix 계약 -----------------------------------------------------
# 브랜치는 항상 ops/agent-* (유니크 접미사)여야 하고, args로 위조할 수 없어야
# 한다. 아래 두 테스트가 그 계약을 회귀로부터 고정한다.


def _fake_pr_capture(monkeypatch):
    captured = {}

    def fake_open(repo, files, branch, title, body, base="main"):
        captured.update(branch=branch, body=body)
        return {"number": 1, "html_url": "https://x/pull/1", "author": "b[bot]"}

    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C, "_fetch_current", lambda path, ref="main": {"ec2_ssh_allowlist": {}}
    )
    monkeypatch.setattr(C.github_app, "open_multi_file_pr", fake_open)
    return captured


_ENTRY_ARGS = {
    "surface": "dev-ec2-ssh",
    "op": "set_entry",
    "entry_key": "k",
    "entry": {"cidr": "203.0.113.0/24", "ports": [22], "description": "d"},
    "reason": "test",
}


def test_branch_prefix_is_agent(monkeypatch):
    captured = _fake_pr_capture(monkeypatch)
    d = _d(C.open_tfvars_pr(dict(_ENTRY_ARGS)))
    assert d["success"] is True
    assert captured["branch"].startswith("ops/agent-")
    assert not captured["branch"].startswith("ops/auto/")


def test_auto_prefix_not_forgeable_via_args(monkeypatch):
    # args에 무엇을 넣어도 브랜치는 ops/agent-*여야 한다(prefix 위조 불가)
    captured = _fake_pr_capture(monkeypatch)
    args = dict(_ENTRY_ARGS)
    args["correlation_id"] = "auto/whatever"  # 슬러그 정제 후에도 prefix 불변
    args["branch"] = "ops/auto/injected"  # 존재하지 않는 파라미터 — 무시돼야 함
    d = _d(C.open_tfvars_pr(args))
    assert d["success"] is True
    assert captured["branch"].startswith("ops/agent-")


# --- PR 본문 변경 상세: 생성/변경/삭제를 사람이 읽을 수 있게 남긴다 --------


def test_pr_body_detail_create(monkeypatch):
    # 없던 key를 넣으면 body에 "생성" + entry 내용(JSON)이 실린다
    captured = _fake_pr_capture(monkeypatch)
    d = _d(C.open_tfvars_pr(dict(_ENTRY_ARGS)))
    assert d["success"] is True
    assert "### 변경 상세" in captured["body"]
    assert "**생성**" in captured["body"]
    assert "203.0.113.0/24" in captured["body"]
    # usage(머지 후 접근/확인법)도 리뷰어용으로 body에 실린다
    assert "### 머지·apply 후" in captured["body"]


def test_pr_body_detail_update_shows_before_after(monkeypatch):
    # 이미 있는 key를 덮어쓰면 "변경" + 변경 전/후 값이 둘 다 실린다
    captured = _fake_pr_capture(monkeypatch)
    monkeypatch.setattr(
        C,
        "_fetch_current",
        lambda path, ref="main": {
            "ec2_ssh_allowlist": {"k": {"cidr": "198.51.100.0/24", "ports": [22]}}
        },
    )
    d = _d(C.open_tfvars_pr(dict(_ENTRY_ARGS)))
    assert d["success"] is True
    assert "**변경**" in captured["body"]
    assert "198.51.100.0/24" in captured["body"]  # before
    assert "203.0.113.0/24" in captured["body"]  # after


def test_pr_body_detail_remove_shows_removed_entry(monkeypatch):
    captured = _fake_pr_capture(monkeypatch)
    monkeypatch.setattr(
        C,
        "_fetch_current",
        lambda path, ref="main": {
            "ec2_ssh_allowlist": {"k": {"cidr": "198.51.100.0/24", "ports": [22]}}
        },
    )
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-ec2-ssh",
                "op": "remove_entry",
                "entry_key": "k",
                "reason": "revoke",
            }
        )
    )
    assert d["success"] is True
    assert "**삭제**" in captured["body"]
    assert "198.51.100.0/24" in captured["body"]  # 지워지는 내용이 보인다


def test_pr_body_detail_service_teardown_warns(monkeypatch):
    # service_enabled true→false는 destroy 경고가 body에 실린다
    captured = _fake_pr_capture(monkeypatch)
    monkeypatch.setattr(
        C,
        "_fetch_current",
        lambda path, ref="main": {
            "_raw": "service_enabled   = true\n"
            'ec2_instance_type = "t3.micro"\n'
            'db_instance_class = "db.t3.micro"\n'
        },
    )
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-service",
                "op": "set_value",
                "value": {"service_enabled": False},
                "reason": "teardown",
            }
        )
    )
    assert d["success"] is True
    assert "`service_enabled`: `True` → `False`" in captured["body"]
    assert "되돌릴 수 없는 변경" in captured["body"]


# --- usage 필드: 만든 것의 접근·사용법을 결정적으로 반환한다 --------------


def test_usage_field_present_ingress(monkeypatch):
    _fake_pr_capture(monkeypatch)
    d = _d(C.open_tfvars_pr(dict(_ENTRY_ARGS)))
    assert d["success"] is True
    assert d.get("usage") and "SSH" in d["usage"]


def test_usage_field_present_scale(monkeypatch):
    _fake_pr_capture(monkeypatch)
    d = _d(
        C.open_tfvars_pr(
            {"surface": "dev-disk", "op": "set_value", "value": 20, "reason": "t"}
        )
    )
    assert d["success"] is True
    assert d.get("usage") and "df -h /data" in d["usage"]


# db_grants usage는 env별 표준 계정을 안내해야 한다(module main.tf 부트스트랩과 정합):
# dev=상시 readonly, prod=rds-temp-user 임시 role. master(dbadmin)를 학생 접속
# 경로로 제시하면 "dbadmin 공유 금지" 설계와 어긋난다. 아래 두 테스트가 회귀 차단.
def test_usage_db_grants_dev_routes_to_readonly_not_master(monkeypatch):
    _fake_pr_capture(monkeypatch)
    monkeypatch.setattr(C, "_fetch_current", lambda path, ref="main": {"db_grants": {}})
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-db-access",
                "op": "set_entry",
                "entry_key": "k",
                "entry": {"cidr": "203.0.113.0/24", "expires_at": ""},
                "reason": "t",
            }
        )
    )
    assert d["success"] is True
    u = d["usage"]
    assert "readonly" in u
    assert "dbadmin" not in u  # dev 표준 경로는 master 계정을 안내하지 않는다


def test_usage_db_grants_prod_routes_to_temp_user_not_master(monkeypatch):
    _fake_pr_capture(monkeypatch)
    monkeypatch.setattr(C, "_fetch_current", lambda path, ref="main": {"db_grants": {}})
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "prod-db-access",
                "op": "set_entry",
                "entry_key": "k",
                "entry": {
                    "cidr": "203.0.113.0/24",
                    "expires_at": "2026-12-01T00:00:00Z",
                },
                "reason": "t",
            }
        )
    )
    assert d["success"] is True
    u = d["usage"]
    assert "rds-temp-user" in u
    assert "dbadmin 공유 금지" in u
    assert "-U dbadmin" not in u  # 학생에게 master 계정으로 접속시키지 않는다


# --- CIDR strict: IaC variables.tf validation(plan → guard 게이트)과 정합 ------


def test_rejects_host_bit_cidr(monkeypatch):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C, "_fetch_current", lambda path, ref="main": {"ec2_ssh_allowlist": {}}
    )
    args = dict(_ENTRY_ARGS)
    args["entry"] = {"cidr": "10.0.1.5/24", "ports": [22], "description": "d"}
    d = _d(C.open_tfvars_pr(args))
    assert d["success"] is False and "invalid CIDR" in d["error"]


def test_rejects_ec2_ssh_description_unsupported_by_aws(monkeypatch):
    _fake_pr_capture(monkeypatch)
    args = dict(_ENTRY_ARGS)
    args["entry"] = {"cidr": "203.0.113.10/32", "description": "진"}
    d = _d(C.open_tfvars_pr(args))
    assert d["success"] is False and "security group description" in d["error"]


def test_ec2_ssh_description_enforces_final_aws_length(monkeypatch):
    _fake_pr_capture(monkeypatch)
    base = {"surface": "dev-ec2-ssh", "op": "set_entry", "entry_key": "k"}
    allowed = _d(
        C.open_tfvars_pr(
            {**base, "entry": {"cidr": "203.0.113.10/32", "description": "a" * 249}}
        )
    )
    rejected = _d(
        C.open_tfvars_pr(
            {**base, "entry": {"cidr": "203.0.113.10/32", "description": "a" * 250}}
        )
    )
    assert allowed["success"] is True
    assert (
        rejected["success"] is False
        and "security group description" in rejected["error"]
    )


# --- no-op 감지: main과 동일한 변경은 PR을 열지 않는다 -------------------------
# 같은 변경이 다른 PR로 먼저 머지되면 봇 PR은 빈 diff가 되어 auto-merge/tf-apply의
# paths 필터에 안 걸린다 — 빈 diff PR은 아예 열지 않고 no_op으로 보고해야 한다.


def test_rejects_ec2_ssh_duplicate_cidr_under_a_different_key(monkeypatch):
    _fake_pr_capture(monkeypatch)
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C,
        "_fetch_current",
        lambda path, ref="main": {
            "ec2_ssh_allowlist": {
                "existing-ssh": {"cidr": "203.0.113.10/32", "description": "jin"}
            }
        },
    )

    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-ec2-ssh",
                "op": "set_entry",
                "entry_key": "new-ssh",
                "entry": {"cidr": "203.0.113.10/32", "description": "jin"},
            }
        )
    )

    assert d["success"] is False
    assert "duplicate" in d["error"].lower()
    assert "existing-ssh" in d["remediation"]


def test_set_entry_identical_to_main_is_noop(monkeypatch):
    entry = {"cidr": "203.0.113.10/32", "description": "jin"}
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C,
        "_fetch_current",
        lambda path, ref="main": {"ec2_ssh_allowlist": {"k": dict(entry)}},
    )
    opened = []
    monkeypatch.setattr(
        C.github_app, "open_multi_file_pr", lambda *a, **k: opened.append(a)
    )
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-ec2-ssh",
                "op": "set_entry",
                "entry_key": "k",
                "entry": entry,
            }
        )
    )
    assert d["success"] is True and d["no_op"] is True
    assert not opened  # PR API가 아예 호출되지 않아야 한다
    assert "NO PR" in d["followup"]


def test_set_entry_differing_value_still_opens_pr(monkeypatch):
    captured = _fake_pr_capture(monkeypatch)
    monkeypatch.setattr(
        C,
        "_fetch_current",
        lambda path, ref="main": {
            "ec2_ssh_allowlist": {
                "k": {"cidr": "203.0.113.10/32", "description": "old"}
            }
        },
    )
    d = _d(C.open_tfvars_pr(dict(_ENTRY_ARGS)))
    assert d["success"] is True and "no_op" not in d
    assert captured["branch"].startswith("ops/agent-")


def test_disk_same_size_is_noop(monkeypatch):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C,
        "_fetch_current",
        lambda path, ref="main": {"_raw": "data_volume_size_gb = 20\n"},
    )
    d = _d(C.open_tfvars_pr({"surface": "dev-disk", "op": "set_value", "value": 20}))
    assert d["success"] is True and d["no_op"] is True


def test_service_same_values_is_noop(monkeypatch):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    raw = (
        "service_enabled   = false\n"
        'ec2_instance_type = "t3.micro"\n'
        'db_instance_class = "db.t3.micro"\n'
    )
    monkeypatch.setattr(C, "_fetch_current", lambda path, ref="main": {"_raw": raw})
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "dev-service",
                "op": "set_value",
                "value": {"service_enabled": False},
            }
        )
    )
    assert d["success"] is True and d["no_op"] is True


# --- 422 remediation: 권한 오답 대신 중복 PR 안내 -----------------------------


def test_422_remediation_mentions_duplicate(monkeypatch):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C, "_fetch_current", lambda path, ref="main": {"ec2_ssh_allowlist": {}}
    )

    def raise_422(*a, **k):
        raise C.github_app.GitHubAppError("open pr -> 422 Reference already exists")

    monkeypatch.setattr(C.github_app, "open_multi_file_pr", raise_422)
    d = _d(C.open_tfvars_pr(dict(_ENTRY_ARGS)))
    assert d["success"] is False
    assert "already exists" in d["remediation"] or "correlation_id" in d["remediation"]


def test_packages_surface_writes_yaml_list(monkeypatch):
    # 2-08 packages surface (2026-07-16 e2e C1-I9 배선): base=main, ansible vars 파일.
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C, "_fetch_current", lambda path, ref="main": {"_raw": "extra_packages: []\n"}
    )
    monkeypatch.setattr(C, "_repo", lambda: "wo-o/ops-agent-iac")
    captured = {}

    def fake_open(repo, files, branch, title, body, base="main"):
        captured.update(files=files, base=base)
        return {"number": 50, "html_url": "https://x/pull/50", "author": "b[bot]"}

    monkeypatch.setattr(C.github_app, "open_multi_file_pr", fake_open)
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "packages",
                "op": "set_value",
                "value": ["fail2ban", "auditd"],
                "reason": "보안 패치에 fail2ban/auditd 포함",
            }
        )
    )
    assert d["success"] is True
    assert captured["base"] == "main"
    content = captured["files"]["ansible/patch-extra-packages.yml"]
    assert "- auditd\n" in content and "- fail2ban\n" in content


def test_packages_surface_rejects_unsafe_name(monkeypatch):
    monkeypatch.setattr(C, "check_change_requirements", lambda: True)
    monkeypatch.setattr(
        C, "_fetch_current", lambda path, ref="main": {"_raw": "extra_packages: []\n"}
    )
    d = _d(
        C.open_tfvars_pr(
            {
                "surface": "packages",
                "op": "set_value",
                "value": ["fail2ban; rm -rf /"],
            }
        )
    )
    assert d["success"] is False and "invalid package" in d["error"]
