# security-patch 실행 증거 수집·보고

`<env>-packages` 변경 뒤 `security-patch`를 실행할 때 패키지 목록 반영, 실제 workflow 실행, 서비스 건강 상태를 서로 다른 증거로 다룬다.

## 필수 대조

1. PR이 MERGED이고 `guard`·`ansible syntax check`가 success인지 확인한다. packages 변경에서는 Terraform `plan=skipped`, `apply_runs=[]`가 정상일 수 있다.
2. 머지 뒤 `ops_github_read_file(path="ansible/patch-extra-packages.yml", ref=<prod면 main, dev면 dev>)`로 파일을 다시 읽어 최종 `extra_packages` 전체 목록이 요청과 일치하는지 확인한다. PR 요약만으로 브랜치의 최종 설정을 추정하지 않는다. 이 재조회는 **설정 반영** 증거이며, 호스트 설치 증거는 뒤의 Ansible 로그와 분리한다.
3. PR merge commit SHA를 기록한다. 상태 응답에 없으면 read-only `gh pr view <n> --repo <owner>/<repo> --json mergeCommit --jq '.mergeCommit.oid'`로 읽는다.
4. real `security-patch`를 한 번 dispatch하고, 반환된 `head_sha`가 merge commit과 같은지 확인한다. 다르면 stale run이므로 완료 근거로 쓰지 않는다.
   - `<env>-packages` 호출이 `no_op: true`여서 새 PR·merge commit이 없다면 존재하지 않는 merge SHA를 찾지 않는다. 대신 dispatch가 반환한 `head_sha`와 workflow 로그의 최종 checkout 뒤 `git log -1 --format=%H` 출력이 서로 같고, 요청 환경 브랜치가 맞는지(`prod=main`, `dev=dev`) 대조한다. 설정 파일의 현재 전체 목록, 실행 checkout SHA, `extra_pkgs` 결과를 함께 증거로 남긴다.
5. workflow가 `completed/success`가 될 때까지 폴링한다.
6. 인증된 GitHub CLI가 있으면 run 로그에서 아래만 추린다.
   - 최종 checkout의 fetch/ref와 `git log -1 --format=%H`
     - GitHub Actions 로그는 명령 줄과 SHA 출력 줄이 분리된다. 키워드 일치 줄만 필터링하면 `git log -1 --format=%H` 명령은 남고 바로 다음 줄의 실제 SHA가 빠질 수 있으므로, 이 항목은 일치 줄 뒤 최소 1~3줄의 문맥을 함께 보존한다.
   - `patched=<bool> rebooted=<bool> extra_pkgs=<n>` 결과
   - 요청 패키지 이름이 실제 package task 또는 로드된 설정에 나타나는지
   - `PLAY RECAP`
7. `ops_get_service_health`에서 요청 환경의 app만 이름의 `-dev-`/`-prod-`로 분리해 각 인스턴스가 running이고 같은 환경 target group이 healthy인지 확인한다.

## 로그 해석 경계

- `patched=True`가 대상별 결과에 있으면 그 대상에서 기본 보안 업데이트 단계가 변경을 적용했다는 실행 증거로 쓸 수 있다. 모든 결과가 `patched=True`이면 `workflow 대상 모두 보안 업데이트 적용`이라고 보고할 수 있지만, 설치된 업데이트 개수·CVE 목록·호스트가 장래 시점까지 완전히 최신이라는 뜻으로 확대하지 않는다.
- `rebooted=False`가 모든 실행 대상에서 확인되면 최종 보고에 `재부팅 불필요`를 실행 결과로 덧붙일 수 있다. 다만 이는 패키지 설치 여부나 서비스 health의 독립 증거가 아니므로 `extra_pkgs`, 설치 task, `PLAY RECAP`, health 조회를 대신하지 않는다.
- `patched=False`는 기본 보안 업데이트 단계에서 새로 적용된 업데이트가 없었다는 실행 결과이지, 전체 `security-patch` run 실패나 추가 패키지 미설치를 뜻하지 않는다. 같은 대상에서 `Install requested extra packages`가 `changed`이고 `extra_pkgs=<n>`이며 `PLAY RECAP`이 `failed=0`, `unreachable=0`이면 `기본 보안 업데이트 변경 없음 · 추가 패키지 처리 성공`으로 분리해 보고한다. 반대로 `patched=False`만으로 서버가 최신 상태라고 단정하지 않는다.

- self-hosted runner cleanup의 이전 `HEAD is now at ...`는 최종 checkout SHA가 아니다. 뒤의 fetch/ref와 마지막 `git log -1`을 기준으로 판정한다.
- 패키지명만 grep하면 cleanup에 남은 이전 브랜치의 PR 제목(예: `extra_packages = [...]`)도 잡힌다. 이 줄은 설치 증거에서 제외하고, 최종 checkout 이후의 package task·로드된 설정·`extra_pkgs` 결과만 사용한다.
- `PLAY RECAP`을 필터링할 때 헤더 한 줄만 남기지 말고 뒤 최소 8줄을 함께 보존한다. 대상이 여러 대면 짧은 문맥 필터가 마지막 대상의 `failed`/`unreachable` 결과를 잘라낼 수 있다.
- `extra_pkgs=2`는 추가 패키지 두 개가 playbook에 전달·처리됐다는 실행 증거이지 host package database 독립 조회가 아니다.
- `PLAY RECAP`의 `changed=3` 같은 값은 해당 호스트에서 변경된 Ansible task 수이지 설치된 패키지 수가 아니다. 패키지 목록 개수는 `extra_pkgs=<n>`과 설정 목록으로만 대조하고, `changed`를 패키지 수나 app 수로 해석하지 않는다.
- `extra_pkgs` 결과 줄 수는 app 인스턴스 수가 아니다. playbook 대상에 Bastion 등 app 외 호스트가 포함될 수 있다.
- 패키지 이름이 PR 제목이나 설정 목록에만 있고 실제 package task 로그에는 없으면 `설정 목록 반영 + extra_pkgs=2 실행 확인`이라고 쓴다. `auditd/fail2ban 설치를 독립 확인`했다고 표현하지 않는다.
- 로그에 `TASK [Install requested extra packages (patch surface)]`와 대상별 `changed`가 보이고 이어서 `extra_pkgs=<n>`이 확인되면, `추가 패키지 설치 task changed · extra_pkgs=<n> 처리 결과 <m>건`이라고 실행 증거를 구체적으로 적을 수 있다. 다만 task가 개별 패키지명을 출력하지 않았다면 설정 목록과 실행 증거를 분리하고, 패키지별 설치를 host package database에서 독립 확인했다고 표현하지 않는다.
- health 응답의 전체 집계가 dev/prod를 섞을 수 있으므로 요청 환경의 app과 target만 열거한다.

## 실행 대상 수와 app 수가 다를 때

`security-patch`는 app 외 호스트까지 대상으로 삼을 수 있어, 로그의 `extra_pkgs=<n>` 결과 건수가
`ops_get_service_health`에서 확인한 app 수보다 많을 수 있다. 이때 대상의 역할을 추정하지 않는다.

- 로그는 `workflow 로그에서 extra_pkgs=<n> 처리 결과 <m>건 확인`으로만 기록한다.
- app 상태는 별도로 `<env> app <a>대 running · target <a>/<a> healthy`처럼 기록한다.
- `<m>`과 `<a>`가 달라도 곧바로 누락·중복으로 판정하지 않는다. `PLAY RECAP`의 모든 대상이
  `failed=0`, `unreachable=0`인지 확인하고, app 정상 여부는 health 조회로 독립 검증한다.
- 패키지 이름이 실제 package task 로그에 없고 설정 파일과 `extra_pkgs` 개수만 확인되면
  `설정 목록 반영 + extra_pkgs=<n> 실행 성공 · 호스트 package database 독립 조회 미지원`으로
  증거 경계를 명시한다.

## 권장 Slack 타임라인

```text
✅ 보안 패치 완료 — 추가 패키지 목록 반영, 실제 실행, 서비스 정상 상태 확인

🛠 <env> · 보안 패치 및 추가 패키지
요청: 보안 패치 + <packages>

① PR 열림 ........... ✅ #<n>
② guard ............. ✅ 통과 (ansible syntax success · Terraform plan skipped)
③ 자동 머지 ......... ✅ MERGED
④ security-patch .... ✅ Ansible workflow success
⑤ 반영 확인 ......... ✅ <env> app <n>대 running · target <n>/<n> healthy
   workflow 로그에서 설정 목록 <packages> + extra_pkgs=<n> 처리 결과 <m>건 확인
   실행 commit <run-head-sha> = 패키지 PR merge commit <merge-sha>

🟢 *<env-app-name>* — running
    <instance-type> · <instance-id> · ALB target healthy

🔗 PR: <pr_url>
🔗 Ansible: <run_url>
```

패키지 이름이 실제 task 로그에서 확인되지 않았으면 ⑤의 들여쓴 줄에 `호스트 package database 독립 조회 미지원`을 덧붙여 증거 범위를 분명히 한다. 집계만으로 끝내지 말고 요청 환경의 app 인스턴스를 각각 이름·instance type·instance ID·ALB target 상태로 열거한다. 단, app 공인 IP는 보안 패치 검증에 필요하지 않으므로 사용자가 접속 정보를 요청하지 않았다면 기본 보고에서 생략한다. workflow의 `head_sha`와 merge commit이 일치했다는 사실도 짧게 남겨, 패키지 목록 머지 전 stale run이 성공 근거로 오인되지 않게 한다.