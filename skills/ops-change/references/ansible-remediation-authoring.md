# 등록형 Ansible 조치 플레이북 작성

등록부(manifest)를 통해 허용된 이름만 실행하는 Ansible 운영 조치를 새로 만들거나
수정할 때의 안전한 순서, 배포 뒤 검증, 실패 수정 원칙.

## 작업 순서

1. 실행 등록부(`ansible/playbooks.yml`)와 대상 인벤토리, 유사한 기존 플레이북을 먼저 읽는다.
2. 기존 파일을 변경할 경우 현재 전문을 읽은 뒤, 전체 파일 교체 방식(`ops_github_open_code_pr`)에
   맞춰 최소 diff를 만든다.
3. 플레이북은 `become: true`와 `serial: 1`을 기본으로 하고, 대상 host group을 명시적으로 제한한다.
4. 새 플레이북은 파일명과 일치하는 안전한 이름으로 manifest에 등록한다(`ansible/<name>.yml`
   + `ansible/playbooks.yml`의 `- name: <name>`). manifest가 추가 변수 주입을 막는 구조이므로,
   런타임 입력값 대신 안전한 고정 기본값을 플레이북 `vars`에 둔다(`-e` 파라미터 없음 — injection 차단).
5. PR의 lint/문법 검사(guard의 ansible syntax)와 머지를 확인한 뒤에만 실제 run을 dispatch한다.
6. run이 실패하면 실패 로그에서 정확한 task와 원인을 확인하고, 이미 적용됐을 수 있는 단계와
   미적용 단계를 분리한다. 수정 PR을 머지한 뒤 실제 run을 다시 실행하고 성공까지 확인한다.
7. run 성공 뒤 서비스 health나 target health 등 독립적인 read-only 근거로 회귀 여부를 확인한다.

## SSH 설정 변경 — handler block 함정

SSH 보안 설정을 바꿀 때는 공개키 접근을 유지하면서, 설정이 유효한 경우에만 daemon을
재시작해야 한다.

- `/etc/ssh/sshd_config.d/`의 전용 drop-in을 사용해 관리 범위를 분리한다.
- 권장 순서: (1) drop-in 배포 → (2) `/usr/sbin/sshd -t`로 전체 SSH daemon 설정 검증 →
  (3) 검증 성공 뒤 `ssh` 서비스 재시작.
- 이 세 단계를 같은 play의 명시적 독립 task 순서로 둔다. **handler에 block을 사용해 검증과
  재시작을 묶지 않는다.** 일부 Ansible 실행 환경에서는 block handler를 listener로 해석하지
  않아 설정 배포 단계에서 `requested handler ... was not found`가 발생해 run이 중단된다(실측).
  검증과 재시작을 독립 task로 두면 검증 실패 시 restart가 실행되지 않는 안전성도 유지된다.
- SSH 재시작은 `serial: 1`과 함께 사용해 한 번에 한 호스트만 영향을 받게 한다.

검증된 골격:

```yaml
- name: Configure SSH hardening drop-in
  ansible.builtin.copy:
    dest: /etc/ssh/sshd_config.d/99-ops-hardening.conf
    owner: root
    group: root
    mode: "0644"
    content: |
      PermitRootLogin no
      PasswordAuthentication no
      KbdInteractiveAuthentication no

- name: Validate SSH daemon configuration
  ansible.builtin.command:
    cmd: /usr/sbin/sshd -t
  changed_when: false

- name: Restart SSH daemon after successful validation
  ansible.builtin.systemd:
    name: ssh
    state: restarted
```

## 로그 정리 조치

- 활성 로그를 직접 삭제하지 말고, 보존 기간을 넘긴 회전 로그(`*.gz`, `*.old`, 숫자 suffix)만
  대상으로 제한한다. 기본 예시는 14일 초과 회전 로그.
- systemd journal은 파일 단위로 지우지 않고 `journalctl --vacuum-time=<기간>`(예: `14d`)을 쓴다.
- 정리 조치가 성공해도 실제 삭제 파일 수나 회수한 디스크 용량을 별도로 관측하지 못했다면,
  실행 성공과 공간 회수량을 구분해 보고한다.

## 완료 판단 · 보고 경계

1. 코드 PR의 Ansible syntax/guard 성공 및 merge 확인.
2. `ansible-ops` 실제 run의 `completed/success` 확인.
3. 서비스 target health를 별도 조회.
4. 로그 삭제 수·절감 용량을 run 로그나 전용 관측에서 얻지 못했으면 그 수치를 보고하지 않는다.

- PR merge, Ansible workflow 성공, 서비스 health는 서로 다른 증거다. 한 결과로 다른 결과를
  추정하지 않는다.
- 첫 실행이 실패했지만 이후 수정·재실행이 성공한 경우, 최초 실패 원인과 최종 성공 run URL을
  모두 남긴다.
- app health가 정상이라고 해서 Bastion을 포함한 모든 호스트의 SSH 설정을 외부에서 독립
  검증했다고 표현하지 않는다. Ansible 성공 범위와 health 검증 범위를 분리한다.
