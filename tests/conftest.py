"""테스트 부트스트랩: 하이픈이 들어간 플러그인 디렉터리를 import하고 state/env를 격리한다.

플러그인은 플러그인 디렉터리 이름에 하이픈이 올 수 있는데(Hermes는 import 이름이 아니라
경로로 플러그인을 로드한다), 이 이름은 유효한 Python 식별자가 아니다 — 그래서 여기서
importlib로 "ops_plugin" 패키지로 명시적으로 로드한다. 이후 테스트는 평범하게
`import ops_plugin.*` 하면 된다.

State(audit JSONL)는 일회용 임시 디렉터리를 가리키게 하고, 클라이언트가 읽는 모든
credential 환경변수를 제거해서 테스트를 hermetic하게 만든다: 실제 credential도, 모델도,
boto3/httpx도 필요 없다.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1]  # tests의 상위 = 플러그인 루트

# 패키지(및 settings 모듈)를 import하기 전에 hermetic env를 먼저 설정한다
os.environ["OPS_STATE_DIR"] = tempfile.mkdtemp(prefix="ops-test-state-")
for var in (
    "OPS_AWS_READ_ROLE",
    "OPS_AWS_REGION",
    "OPS_PROJECT_PREFIX",
    "OPS_GRAFANA_URL",
    "OPS_GRAFANA_PUBLIC_URL",
    "OPS_GRAFANA_TOKEN",
    "GITHUB_TOKEN",
    "OPS_GITHUB_REPO",
    "OPS_PAGERDUTY_TOKEN",
    "OPS_PAGERDUTY_FROM_EMAIL",
    "OPS_CLOUDFLARE_READ_TOKEN",
    "OPS_CLOUDFLARE_ZONE_ID",
    "OPS_GITHUB_APP_ID",
    "OPS_GITHUB_INSTALLATION_ID",
    "OPS_GITHUB_PRIVATE_KEY",
    "OPS_GITHUB_PRIVATE_KEY_PATH",
):
    os.environ.pop(var, None)

if "ops_plugin" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "ops_plugin",
        _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ops_plugin"] = module
    spec.loader.exec_module(module)
