"""Read-only preflight for a driver comparing direct PostgreSQL with Lambda/SQL."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import boto3
from botocore.exceptions import ClientError

from sebs.storage.aws_postgresql import AWSPostgreSQL
from sebs.storage.sql_config import sql_config


ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    "function/function.py": "benchmarks/700.analytics/720.sql-postgresql/python/function.py",
    "function/queries.py": "benchmarks/700.analytics/720.sql-postgresql/python/queries.py",
    "function/sql.py": "benchmarks/wrappers/common/python/sql.py",
}


def verify_package(archive, code_sha, root=ROOT):
    actual = base64.b64encode(hashlib.sha256(archive.read_bytes()).digest()).decode()
    if actual != code_sha:
        raise ValueError("Cached package differs from the deployed Lambda; refresh through SeBS")
    with ZipFile(archive) as package:
        for member, source in SOURCES.items():
            if package.read(member) != (root / source).read_bytes():
                raise ValueError("Deployed benchmark source differs from checkout: " + source)
    return actual


def verify_lambda(health, outputs, config, trace):
    if health.get("Runtime") != "python3.12" or health.get("Architectures") != ["x86_64"]:
        raise ValueError("Cloud comparison expects the tested Python 3.12/x64 function")
    if health.get("MemorySize") != trace["settings"]["memory"]:
        raise ValueError("Function memory differs from the recorded trace")
    if health.get("State") != "Active" or health.get("LastUpdateStatus") != "Successful":
        raise ValueError("Function is not ready")
    vpc = health.get("VpcConfig", {})
    if config.subnet_id not in vpc.get("SubnetIds", []) or outputs["LambdaGroupId"] not in vpc.get("SecurityGroupIds", []):
        raise ValueError("Function does not use the SQL backend's private network")
    if health.get("Role") != outputs["LambdaRoleArn"]:
        raise ValueError("Function does not use the SQL backend's execution role")
    runtime = json.loads(health.get("Environment", {}).get("Variables", {}).get("SEBS_SQL_CONFIG", "{}"))
    if runtime != {"type": "postgresql", "region": config.region, "secret_arn": outputs["SecretArn"]}:
        raise ValueError("Function SQL storage differs from the requested attachment")


def standard_vcpu_use(instances, types):
    """Conservative estimate for ordinary shared-tenancy Standard-family instances."""
    return sum(types[i["InstanceType"]]["VCpuInfo"]["DefaultVCpus"]
        for i in instances
        if not i.get("InstanceLifecycle")
        and i["InstanceType"][0] in "acdhimrtz"
        and i.get("Placement", {}).get("Tenancy", "default") == "default")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-configuration", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("cache-postgresql-aws"))
    parser.add_argument("--state-dir", type=Path, default=Path("results/postgresql-aws-cloud"))
    args = parser.parse_args()
    trace = json.loads(args.trace.read_text())
    payload = trace["input"]
    if payload.get("scale_factor") != 1 or payload.get("connection_policy") != "fresh":
        parser.error("Use the validated SF1 fresh-connection workload trace")
    results_file = args.trace.parent / "invocations-processed.json"
    results = json.loads(results_file.read_text())
    names = list(results["_invocations"])
    if len(names) != 1:
        parser.error("Expected exactly one deployed Lambda in processed workload results")
    function_name = names[0]
    config = sql_config(json.loads(args.storage_configuration.read_text())["sql"])
    if config.provider != "aws":
        parser.error("An existing AWS PostgreSQL storage attachment is required")
    session = boto3.Session()
    _, stack, outputs = AWSPostgreSQL(config, session).info()
    if stack["StackStatus"] != "CREATE_COMPLETE":
        raise ValueError("SQL storage stack is not ready")
    health = session.client("lambda", region_name=config.region).get_function_configuration(FunctionName=function_name)
    verify_lambda(health, outputs, config, trace)
    archive = args.cache / "720.sql-postgresql/aws/python/code_package/default/3.12/x64/720.sql-postgresql.zip"
    verify_package(archive, health["CodeSha256"])
    ec2 = session.client("ec2", region_name=config.region)
    server = ec2.describe_instances(InstanceIds=[outputs["InstanceId"]])["Reservations"][0]["Instances"][0]
    if server["State"]["Name"] != "running":
        raise ValueError("Existing PostgreSQL server must be running")
    subnet = ec2.describe_subnets(SubnetIds=[config.subnet_id])["Subnets"][0]
    driver_type = server["InstanceType"]
    images = ec2.describe_images(ImageIds=[server["ImageId"]])["Images"]
    if not images or images[0]["State"] != "available" or images[0]["Architecture"] != "x86_64":
        raise ValueError("Server AMI is no longer launchable as an x64 driver; select another verified Ubuntu AMI")
    offerings = ec2.describe_instance_type_offerings(LocationType="availability-zone", Filters=[
        {"Name": "instance-type", "Values": [driver_type]},
        {"Name": "location", "Values": [subnet["AvailabilityZone"]]}])["InstanceTypeOfferings"]
    if not offerings:
        raise ValueError("Driver instance type is not offered in the database availability zone")
    groups = ec2.describe_security_groups(GroupIds=[outputs["LambdaGroupId"]])["SecurityGroups"]
    if groups[0]["VpcId"] != subnet["VpcId"] or groups[0]["IpPermissions"]:
        raise ValueError("Expected the existing Lambda client security group with no inbound rules")
    routes = ec2.describe_route_tables(Filters=[{"Name": "association.subnet-id", "Values": [config.subnet_id]}])["RouteTables"]
    if not routes:
        routes = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [subnet["VpcId"]]},
                                                   {"Name": "association.main", "Values": ["true"]}])["RouteTables"]
    if not any(r.get("DestinationCidrBlock") == "0.0.0.0/0" and r.get("State") == "active"
               and r.get("GatewayId", "").startswith("igw-") for table in routes for r in table["Routes"]):
        raise ValueError("Driver bootstrap needs the existing subnet's Internet Gateway route")
    instances = [i for page in ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "pending"]}])
        for reservation in page["Reservations"] for i in reservation["Instances"]]
    types = {}
    names = sorted({driver_type, *(i["InstanceType"] for i in instances)})
    for offset in range(0, len(names), 100):
        types.update({t["InstanceType"]: t for t in ec2.describe_instance_types(InstanceTypes=names[offset:offset+100])["InstanceTypes"]})
    quota, quota_error = None, None
    try:
        for page in session.client("service-quotas", region_name=config.region).get_paginator("list_service_quotas").paginate(ServiceCode="ec2"):
            for entry in page["Quotas"]:
                if entry["QuotaName"].startswith("Running On-Demand Standard ("):
                    quota = entry["Value"]
    except ClientError as error:
        quota_error = error.response["Error"]["Code"]
    used = standard_vcpu_use(instances, types)
    requested = types[driver_type]["VCpuInfo"]["DefaultVCpus"]
    driver_eligible = types[driver_type].get("FreeTierEligible")
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "region": config.region,
        "server_instance_id": outputs["InstanceId"], "subnet_id": config.subnet_id,
        "vpc_id": subnet["VpcId"], "availability_zone": subnet["AvailabilityZone"],
        "driver_instance_type": driver_type, "driver_vcpus": requested,
        "driver_ami": images[0]["ImageId"], "driver_ami_name": images[0]["Name"],
        "driver_free_tier_eligible": driver_eligible,
        "estimated_standard_vcpus_in_use": used, "standard_vcpu_quota": quota,
        "quota_error": quota_error, "quota_headroom_ok": None if quota is None else used + requested <= quota,
        "lambda_function": function_name, "lambda_code_sha256": health["CodeSha256"],
        "lambda_memory_mb": health["MemorySize"], "lambda_timeout_s": health["Timeout"],
        "source_archive": str(archive.resolve()), "source_files_verified": list(SOURCES),
        "input": payload, "staging_bucket": outputs["StagingBucket"],
        "lambda_security_group_id": outputs["LambdaGroupId"],
        "secret_arn": outputs["SecretArn"], "stack_id": stack["StackId"],
        "benchmark_queries_executed": 0, "resources_created": 0}
    args.state_dir.mkdir(parents=True, exist_ok=True)
    target = args.state_dir / "preflight.json"
    target.write_text(json.dumps(report, indent=2) + "\n")
    print("Existing SQL storage, Lambda configuration and exact source package: verified")
    print("Driver candidate:", driver_type, images[0]["ImageId"], subnet["AvailabilityZone"])
    print("Free-tier eligible:", driver_eligible)
    print("Standard vCPU quota:", quota, "estimated in use:", used, "driver needs:", requested)
    print("Quota headroom:", report["quota_headroom_ok"], "quota lookup error:", quota_error)
    print("Saved:", target)
    print("No driver launched; no SQL queries submitted or cloud resources changed.")


if __name__ == "__main__":
    main()
