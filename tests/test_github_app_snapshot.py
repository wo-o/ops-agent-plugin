from ops_plugin.clients import github_app


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_snapshot_preserves_base_owned_environment_surface(monkeypatch):
    monkeypatch.setattr(github_app, "_headers", lambda: {"Authorization": "Bearer x"})
    tree_posts = []

    def fake_http(method, url, headers=None, body=None, **kwargs):
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return _Resp(200, {"object": {"sha": "base-head"}})
        if method == "GET" and url.endswith("/git/commits/base-head"):
            return _Resp(200, {"tree": {"sha": "base-root"}})
        if method == "GET" and url.endswith("/git/ref/heads/dev"):
            return _Resp(200, {"object": {"sha": "dev-head"}})
        if method == "GET" and url.endswith("/git/commits/dev-head"):
            return _Resp(200, {"tree": {"sha": "dev-root"}})
        if method == "GET" and url.endswith("/git/trees/base-root"):
            return _Resp(
                200,
                {
                    "tree": [
                        {"path": "modules", "type": "tree", "sha": "base-modules"},
                        {"path": "ansible", "type": "tree", "sha": "base-ansible"},
                    ]
                },
            )
        if method == "GET" and url.endswith("/git/trees/dev-root"):
            return _Resp(
                200,
                {
                    "tree": [
                        {"path": "modules", "type": "tree", "sha": "dev-modules"},
                        {"path": "ansible", "type": "tree", "sha": "dev-ansible"},
                    ]
                },
            )
        if method == "GET" and url.endswith("/git/trees/base-ansible"):
            return _Resp(
                200,
                {
                    "tree": [
                        {
                            "path": "patch-extra-packages.yml",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "prod-packages",
                        }
                    ]
                },
            )
        if method == "GET" and url.endswith("/git/trees/dev-ansible"):
            return _Resp(
                200,
                {
                    "tree": [
                        {
                            "path": "patch-extra-packages.yml",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "dev-packages",
                        },
                        {
                            "path": "new-playbook.yml",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "new-playbook",
                        },
                    ]
                },
            )
        if method == "POST" and url.endswith("/git/trees"):
            tree_posts.append(body)
            sha = "filtered-ansible" if len(tree_posts) == 1 else "promotion-root"
            return _Resp(201, {"sha": sha})
        if method == "POST" and url.endswith("/git/commits"):
            return _Resp(201, {"sha": "promotion-commit"})
        if method == "POST" and url.endswith("/git/refs"):
            return _Resp(201, {})
        raise AssertionError(f"unexpected request: {method} {url} {body}")

    monkeypatch.setattr(github_app, "http_request", fake_http)

    result = github_app.create_snapshot_branch(
        "wo-o/ops-agent-iac",
        "dev",
        "main",
        ("modules", "ansible"),
        "promote/test",
        "promote",
        preserve_paths=("ansible/patch-extra-packages.yml",),
    )

    assert result == {
        "changed": True,
        "branch": "promote/test",
        "sha": "promotion-commit",
    }
    assert tree_posts[0] == {
        "base_tree": "dev-ansible",
        "tree": [
            {
                "path": "patch-extra-packages.yml",
                "mode": "100644",
                "type": "blob",
                "sha": "prod-packages",
            }
        ],
    }
    assert tree_posts[1]["tree"] == [
        {"path": "modules", "mode": "040000", "type": "tree", "sha": "dev-modules"},
        {
            "path": "ansible",
            "mode": "040000",
            "type": "tree",
            "sha": "filtered-ansible",
        },
    ]
