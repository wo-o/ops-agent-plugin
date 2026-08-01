"""환경을 위한 AWS read-only 클라이언트.

read 경계는 IAM read-only 정책이 강제한다: boto3 세션을 얻는 방법은 정확히 하나뿐이다 —
전체 ARN 이 OPS_AWS_READ_ROLE 에 들어있는 <project>-hermes-readonly 역할을 assume 하는 것.
플러그인은 로컬 원본 자격증명 체인으로 AWS 를 호출하는 일이 절대 없으므로, 도구가 무엇을
하든 read 경계는 IAM 레벨에서 유지된다.

앱 플릿은 tag:Role=app(+Name 프리픽스 스코프)으로, 데이터 볼륨은 Name 태그
(<prefix>-*-data-*)로 조회한다.
aws CLI 로 shell 을 실행하는 일이 절대 없으며, 자유 형식 CLI 도구도 절대 노출하지 않는다.

boto3 는 지연 import 되므로, 설치돼 있지 않아도 모듈이 로드되고 유닛테스트도 된다.
"""

from __future__ import annotations

from .. import settings


def check_aws() -> bool:
    """Visibility 게이트: boto3 import 가능하고 read 역할 ARN 이 설정돼 있을 것."""
    if not settings.aws_read_role_arn():
        return False
    try:
        import importlib.util

        return importlib.util.find_spec("boto3") is not None
    except Exception:
        return False


def _client(service: str, region: str | None = None):
    """호출마다 read 롤을 assume 해서 새 boto3 클라이언트를 만든다(공개 API만; 세션
    자격증명 1시간). 역할 체크는 boto3 import 보다 먼저 실행되므로 boto3 없이도
    설정 오류 메시지가 깔끔하게 나온다."""
    role_arn = settings.aws_read_role_arn()
    if not role_arn:
        raise RuntimeError(
            "OPS_AWS_READ_ROLE is not set; the plugin only calls AWS through "
            "the assumed read-only role"
        )
    import boto3
    from botocore.config import Config

    region = region or settings.aws_region()
    creds = (
        boto3.Session(region_name=region)
        .client("sts")
        .assume_role(
            RoleArn=role_arn,
            RoleSessionName="ops-readonly",
            DurationSeconds=3600,
        )["Credentials"]
    )
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
    return session.client(
        service,
        config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}),
    )


def _app_filter(prefix: str) -> list[dict]:
    # 앱 플릿은 tag:Role=app으로 고른다 — iac(modules/service outputs.tf)가 문서화한
    # 정본 셀렉터로, ansible 인벤토리·Prometheus ec2_sd와 같은 앵커라 이름 형식이
    # 바뀌어도 함께가 아니면 깨지지 않는다. Name 프리픽스는 프로젝트 스코프만 좁힌다
    # (bastion은 Role=bastion, 모니터링·Hermes 호스트는 Role 태그 없음 → 자연 제외).
    return [
        {"Name": "tag:Role", "Values": ["app"]},
        {"Name": "tag:Name", "Values": [f"{prefix}-*"]},
    ]


def _data_volume_filter(prefix: str) -> list[dict]:
    # 앱 플릿의 데이터 볼륨: iac가 <name>-data-<n>으로 명명한다(modules/service
    # main.tf: "${local.name}-data-${count.index}" → ops-agent-iac-<env>-data-0).
    # `data` 앞에 붙던 `-*-`(예전 값)는 env-scoped prefix(ops-agent-iac-prod)로
    # 호출될 때 prefix와 data 사이 세그먼트를 강제해 0건이 됐다. `-*data-*`는 그 자리
    # 세그먼트를 optional로 둬 env prefix(빈 매칭)와 project prefix(env 세그먼트 매칭)
    # 양쪽을 커버한다(루트 볼륨은 Name 태그가 없어 자연 제외).
    return [{"Name": "tag:Name", "Values": [f"{prefix}-*data-*"]}]


def _has_keep_tag(tags: list[dict] | None) -> bool:
    for t in tags or []:
        if t.get("Key", "").lower() == "keep" and str(t.get("Value", "")).lower() in (
            "true",
            "1",
            "yes",
        ):
            return True
    return False


# --- 읽기 작업 (전부 Name 태그로 프로젝트 앱 플릿에 스코프) -------------------
def describe_instances_by_lab(prefix: str) -> list[dict]:
    return _describe_instances(_app_filter(prefix))


def describe_bastions_by_lab(prefix: str) -> list[dict]:
    # bastion은 tag:Role=bastion (modules/service main.tf). RDS 접근 안내의 SSH
    # 터널 명령이 bastion public IP를 요구해 앱 플릿(Role=app)과 별도 키로 노출한다.
    return _describe_instances(
        [
            {"Name": "tag:Role", "Values": ["bastion"]},
            {"Name": "tag:Name", "Values": [f"{prefix}-*"]},
        ]
    )


def _describe_instances(filters: list[dict]) -> list[dict]:
    ec2 = _client("ec2")
    out: list[dict] = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=filters, PaginationConfig={"MaxItems": 100}):
        for r in page.get("Reservations", []):
            for i in r.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                out.append(
                    {
                        "id": i["InstanceId"],
                        "name": tags.get("Name"),
                        "type": i["InstanceType"],
                        "state": i["State"]["Name"],
                        "az": i.get("Placement", {}).get("AvailabilityZone"),
                        "public_ip": i.get("PublicIpAddress"),
                        "public_dns": i.get("PublicDnsName") or None,
                        "launched": (
                            i["LaunchTime"].isoformat() if i.get("LaunchTime") else None
                        ),
                    }
                )
    return out[:100]


def describe_volumes_by_lab(prefix: str) -> list[dict]:
    ec2 = _client("ec2")
    vols = ec2.describe_volumes(Filters=_data_volume_filter(prefix)).get("Volumes", [])
    # `name` carries the env segment (ops-agent-iac-<env>-data-<n>). Without it a
    # project-wide prefix call returns mixed dev/prod volumes distinguishable only by
    # cross-referencing attached_to against the instance list — a join the model got
    # wrong in practice (reported prod 40GiB as dev's size and skipped a needed PR).
    return [
        {
            "id": v["VolumeId"],
            "name": {t["Key"]: t["Value"] for t in v.get("Tags", [])}.get("Name"),
            "size_gb": v["Size"],
            "type": v.get("VolumeType"),
            "state": v.get("State"),
            "attached_to": [
                a.get("InstanceId")
                for a in v.get("Attachments", [])
                if a.get("InstanceId")
            ],
        }
        for v in vols
    ][:100]


def resolve_target_group_arns_by_lab(prefix: str) -> list[str]:
    """이 프로젝트 앱 플릿의 ALB target group (read-only). target group 이름이
    <prefix>-...-be/-fe 규칙이라 이름으로 매칭한다(태그 불필요)."""
    elb = _client("elbv2")
    arns: list[str] = []
    for page in elb.get_paginator("describe_target_groups").paginate():
        arns.extend(tg["TargetGroupArn"] for tg in page.get("TargetGroups", []))
    # target group 이름으로 매칭 (ELB 이름은 arn 마지막 세그먼트에서 뽑는다)
    matched: list[str] = []
    for arn in arns:
        tg_name = (
            arn.split(":targetgroup/")[-1].split("/")[0]
            if ":targetgroup/" in arn
            else ""
        )
        if tg_name.startswith(f"{prefix}-"):
            matched.append(arn)
    return matched


def describe_load_balancers_by_lab(prefix: str) -> list[dict]:
    """이 프로젝트의 ALB (read-only). 모듈이 ALB 이름을 <prefix>-<env>-alb로 만들므로
    target group과 같은 이름 prefix 매칭. dns_name은 DNS 레코드(CNAME)의 content 원천."""
    elb = _client("elbv2")
    out: list[dict] = []
    for page in elb.get_paginator("describe_load_balancers").paginate():
        for lb in page.get("LoadBalancers", []):
            if str(lb.get("LoadBalancerName", "")).startswith(f"{prefix}-"):
                out.append(
                    {
                        "name": lb.get("LoadBalancerName"),
                        "dns_name": lb.get("DNSName"),
                        "arn": lb.get("LoadBalancerArn"),
                        "state": lb.get("State", {}).get("Code"),
                        "scheme": lb.get("Scheme"),
                    }
                )
    return out[:20]


def describe_security_groups_by_lab(prefix: str) -> list[dict]:
    """이 프로젝트 SG의 ingress CIDR 룰 (read-only). group-name <prefix>-* 로 매칭.
    에이전트가 SSH/DB 개방(ec2_ssh_allowlist 등)을 머지·apply 한 뒤 요청한 CIDR:port
    룰이 SG에 실제 반영됐는지 자가검증하는 용도. IpRanges(CIDR)만 압축해 담는다 —
    SG-to-SG 참조(UserIdGroupPairs)는 allowlist 검증 대상이 아니라 노이즈라 제외."""
    ec2 = _client("ec2")
    sgs = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [f"{prefix}-*"]}]
    ).get("SecurityGroups", [])
    out: list[dict] = []
    for g in sgs:
        ingress: list[dict] = []
        for perm in g.get("IpPermissions", []):
            proto = perm.get("IpProtocol")
            proto = "all" if proto == "-1" else proto
            for r in perm.get("IpRanges", []):
                cidr = r.get("CidrIp")
                if not cidr:
                    continue
                ingress.append(
                    {
                        "protocol": proto,
                        "from_port": perm.get("FromPort"),
                        "to_port": perm.get("ToPort"),
                        "cidr": cidr,
                    }
                )
        out.append({"id": g["GroupId"], "name": g.get("GroupName"), "ingress": ingress})
    return out[:50]


def describe_db_instances_by_lab(prefix: str) -> list[dict]:
    """이 프로젝트의 RDS 인스턴스 (read-only). 모듈이 식별자를 <prefix>-<env>로 만들므로
    DBInstanceIdentifier prefix 매칭. endpoint는 앱/bastion이 붙는 주소, 없으면 아직
    생성 중이거나 삭제됨. publicly_accessible=false 확인은 외부 직접 경로 없음의 근거."""
    rds = _client("rds")
    out: list[dict] = []
    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page.get("DBInstances", []):
            db_id = str(db.get("DBInstanceIdentifier", ""))
            # RDS id is <project>-<env> (e.g. ops-agent-iac-prod) — when the agent
            # scopes by passing prefix=<project>-<env>, that id equals the prefix with
            # no trailing "-", so a bare startswith(prefix + "-") would drop it while
            # EC2/ALB/SG (which have a further "-app-0"/"-*" suffix) still match.
            if not (db_id == prefix or db_id.startswith(f"{prefix}-")):
                continue
            endpoint = db.get("Endpoint") or {}
            out.append(
                {
                    "id": db.get("DBInstanceIdentifier"),
                    "status": db.get("DBInstanceStatus"),
                    "engine": db.get("Engine"),
                    "instance_class": db.get("DBInstanceClass"),
                    "endpoint": endpoint.get("Address"),
                    "port": endpoint.get("Port"),
                    "multi_az": db.get("MultiAZ"),
                    "publicly_accessible": db.get("PubliclyAccessible"),
                }
            )
    return out[:20]


def alb_target_health(target_group_arn: str) -> list[dict]:
    elb = _client("elbv2")
    desc = elb.describe_target_health(TargetGroupArn=target_group_arn)
    return [
        {
            "target": d["Target"]["Id"],
            "port": d["Target"].get("Port"),
            "state": d["TargetHealth"]["State"],
            "reason": d["TargetHealth"].get("Reason"),
        }
        for d in desc.get("TargetHealthDescriptions", [])
    ]


def cost_summary(period_days: int, group_by: str = "SERVICE") -> dict:
    """직전 기간에 대한 Cost Explorer amortized 비용 (학생 계정 전체 기준 — 랩은 규모가 너무
    작아서 태그별 비용 할당이 기본으로 활성화되지 않는다)."""
    from datetime import date, timedelta

    # Cost Explorer 는 글로벌 엔드포인트가 하나뿐이다 (us-east-1 자격증명 스코프).
    ce = _client("ce", region="us-east-1")
    end = date.today()
    start = end - timedelta(days=period_days)
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["AmortizedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": group_by}],
    )
    totals: dict[str, float] = {}
    for day in resp.get("ResultsByTime", []):
        for grp in day.get("Groups", []):
            key = grp["Keys"][0]
            amt = float(grp["Metrics"]["AmortizedCost"]["Amount"])
            totals[key] = totals.get(key, 0.0) + amt
    top = dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:20])
    return {
        "period_days": period_days,
        "group_by": group_by,
        "total": round(sum(totals.values()), 2),
        "top": {k: round(v, 2) for k, v in top.items()},
    }


def find_unused_candidates() -> dict:
    """보고 전용 미사용 리소스 후보 (idle != unused). keep=true 태그가 붙은 것은 전부
    제외한다 (시드에는 바로 이 false-positive 케이스를 가르치려고 keep 태그가 달린
    볼륨이 하나 들어있다); 절대 삭제하지 않는다.

    시드(2-0-setup)에 맞춘 고정 스캔 대상: 미연결 EBS, 미연결 EIP, 고아
    <project>-* 보안 그룹, Lab 태그가 붙은 스냅샷, 그리고 <project>- 이름 프리픽스
    아래에서 마지막 사용 기록이 없는 IAM 역할. 프리픽스는 settings.project_prefix()
    (OPS_PROJECT_PREFIX, 기본 ops-agent-iac)를 따른다."""
    out: dict[str, list] = {}
    ec2 = _client("ec2")

    vols = ec2.describe_volumes(
        Filters=[{"Name": "status", "Values": ["available"]}]
    ).get("Volumes", [])
    out["unattached_ebs"] = [
        {
            "id": v["VolumeId"],
            "size_gb": v["Size"],
            "created": v["CreateTime"].isoformat(),
        }
        for v in vols
        if not _has_keep_tag(v.get("Tags"))
    ][:100]

    addrs = ec2.describe_addresses().get("Addresses", [])
    out["unassociated_eip"] = [
        {"ip": a["PublicIp"], "alloc": a.get("AllocationId")}
        for a in addrs
        if not a.get("AssociationId") and not _has_keep_tag(a.get("Tags"))
    ][:100]

    # 고아 SG: 어떤 network interface 에도 연결되지 않은 <project>-* 그룹.
    attached_sg_ids: set[str] = set()
    for page in ec2.get_paginator("describe_network_interfaces").paginate():
        for eni in page.get("NetworkInterfaces", []):
            for g in eni.get("Groups", []):
                attached_sg_ids.add(g.get("GroupId"))
    sgs = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [f"{settings.project_prefix()}-*"]}]
    ).get("SecurityGroups", [])
    out["orphan_security_groups"] = [
        {"id": g["GroupId"], "name": g["GroupName"]}
        for g in sgs
        if g["GroupId"] not in attached_sg_ids and not _has_keep_tag(g.get("Tags"))
    ][:100]

    snaps = ec2.describe_snapshots(
        OwnerIds=["self"], Filters=[{"Name": "tag-key", "Values": ["Lab"]}]
    ).get("Snapshots", [])
    out["snapshots"] = [
        {
            "id": s["SnapshotId"],
            "size_gb": s.get("VolumeSize"),
            "started": s["StartTime"].isoformat() if s.get("StartTime") else None,
        }
        for s in snaps
        if not _has_keep_tag(s.get("Tags"))
    ][:100]

    # IaC 리포의 롤은 기본 path("/")에 <project>- 이름 프리픽스로 생성되므로
    # PathPrefix 대신 이름 프리픽스로 거른다 (list_roles에는 이름 필터가 없다).
    iam = _client("iam")
    prefix = f"{settings.project_prefix()}-"
    roles: list = []
    for page in iam.get_paginator("list_roles").paginate():
        roles.extend(
            r for r in page.get("Roles", []) if r["RoleName"].startswith(prefix)
        )
    out["unused_iam_roles"] = [
        {
            "name": r["RoleName"],
            "created": r["CreateDate"].isoformat() if r.get("CreateDate") else None,
            "last_used": (r.get("RoleLastUsed") or {}).get("LastUsedDate"),
        }
        for r in roles
        if not (r.get("RoleLastUsed") or {}).get("LastUsedDate")
    ][:100]

    return out
