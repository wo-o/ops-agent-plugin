#!/usr/bin/env python3
"""GitHub Actions security-patch 로그에서 감사 근거만 추출한다.

사용법:
  python scripts/extract-security-patch-evidence.py <owner/repo> <run-id>

사전 조건: 인증된 gh CLI.
"""

from __future__ import annotations

import re
import subprocess
import sys

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def fail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def main() -> None:
    if len(sys.argv) != 3:
        fail("사용법: extract-security-patch-evidence.py <owner/repo> <run-id>")

    repo, run_id = sys.argv[1:]
    proc = subprocess.run(
        ["gh", "run", "view", run_id, "--repo", repo, "--log"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        fail(proc.stderr.strip() or f"gh run view 실패(exit={proc.returncode})", proc.returncode)

    lines = [ANSI_RE.sub("", line) for line in proc.stdout.splitlines()]
    windows: list[tuple[int, int]] = []

    for index, line in enumerate(lines):
        lowered = line.lower()
        if "checking out the ref" in lowered:
            windows.append((index, min(len(lines), index + 6)))
        elif "git log -1 --format=%h" in lowered:
            # 명령 다음 줄에 실제 SHA가 출력되므로 문맥을 반드시 포함한다.
            windows.append((index, min(len(lines), index + 4)))
        elif any(
            token in lowered
            for token in (
                "install requested extra packages",
                "extra_pkgs=",
                "auditd",
                "fail2ban",
            )
        ):
            windows.append((index, min(len(lines), index + 4)))
        elif "play recap" in lowered:
            # 여러 대상의 failed/unreachable 결과가 잘리지 않도록 넉넉히 보존한다.
            windows.append((index, min(len(lines), index + 12)))

    selected: set[int] = set()
    for start, end in windows:
        selected.update(range(start, end))

    if not selected:
        fail("security-patch 증거 패턴을 찾지 못했습니다.", 1)

    for index in sorted(selected):
        print(lines[index])


if __name__ == "__main__":
    main()
