"""Create the SQL EC2 server, role, bundle and persistent launch state (us-east-1)."""
import argparse
import hashlib
import io
import json
import tarfile
import uuid
from pathlib import Path

import boto3

from evaluation.sql.prepare_private_access import role_for_ec2

ROOT = Path(__file__).resolve().parents[2]


def server_bootstrap():
    return '''#!/bin/bash
set -euxo pipefail
export HOME=/root
apt-get update
apt-get install -y python3-venv curl snapd
if ! systemctl is-active --quiet snap.amazon-ssm-agent.amazon-ssm-agent.service; then
    snap install amazon-ssm-agent --classic || snap list amazon-ssm-agent
    systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service
fi
mkdir -p /opt/sebs-sql/repo
python3 -m venv /opt/sebs-sql/venv
/opt/sebs-sql/venv/bin/pip install duckdb==2.0.0 boto3
touch /opt/sebs-sql/bootstrap.complete
chown -R ubuntu:ubuntu /opt/sebs-sql
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, default=Path("results/sql-aws-workload-01/sql-workload/trace.json"))
    parser.add_argument("--state-dir", type=Path, default=Path("results/sql-ec2"))
    parser.add_argument("--subnet", help="Public subnet with outbound internet; otherwise select default us-east-1a")
    parser.add_argument("--instance-type", default="m7i-flex.large")
    args = parser.parse_args()
    folder = args.state_dir
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "instance.json").exists():
        raise ValueError("Server state exists; inspect it instead of launching a duplicate")
    trace = json.loads(args.trace.read_text())
    if trace["input"]["scale_factor"] != 1:
        raise ValueError("This comparison setup requires SF1")
    bucket = trace["input"]["bucket"]["bucket"]
    region = "us-east-1"
    ec2, iam = boto3.client("ec2", region_name=region), boto3.client("iam")
    request_path = folder / "server-launch-request.json"
    if request_path.exists():
        request = json.loads(request_path.read_text())
    else:
        if args.subnet:
            subnets = ec2.describe_subnets(SubnetIds=[args.subnet])["Subnets"]
        else:
            subnets = ec2.describe_subnets(Filters=[
                {"Name": "default-for-az", "Values": ["true"]},
                {"Name": "availability-zone", "Values": ["us-east-1a"]}])["Subnets"]
        if len(subnets) != 1:
            raise ValueError("Supply --subnet for a public subnet with an internet-gateway route")
        subnet = subnets[0]
        offerings = ec2.describe_instance_type_offerings(LocationType="availability-zone", Filters=[
            {"Name": "location", "Values": [subnet["AvailabilityZone"]]},
            {"Name": "instance-type", "Values": [args.instance_type]}])["InstanceTypeOfferings"]
        if not offerings:
            raise ValueError("Instance type unavailable in selected AZ")
        role = "sebs-sql-ec2-role"
        role_for_ec2(iam, role)
        iam.put_role_policy(RoleName=role, PolicyName="ReadSqlBenchmarkInputs", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}"},
                {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/*"}]}))
        groups = ec2.describe_security_groups(Filters=[
            {"Name": "vpc-id", "Values": [subnet["VpcId"]]},
            {"Name": "group-name", "Values": ["sebs-sql-server"]}])["SecurityGroups"]
        if groups:
            if groups[0]["IpPermissions"]:
                raise ValueError("Existing server security group has ingress; inspect before reuse")
            group = groups[0]["GroupId"]
        else:
            group = ec2.create_security_group(GroupName="sebs-sql-server", VpcId=subnet["VpcId"],
                Description="Private SQL service; SSM administration")['GroupId']
        images = ec2.describe_images(Owners=["099720109477"], Filters=[
            {"Name": "name", "Values": ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]}])["Images"]
        image = max(images, key=lambda i: i["CreationDate"])
        request = dict(ImageId=image["ImageId"], InstanceType=args.instance_type, MinCount=1, MaxCount=1,
            ClientToken=str(uuid.uuid4()), IamInstanceProfile={"Name": role},
            NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet["SubnetId"],
                "Groups": [group], "AssociatePublicIpAddress": True}],
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            BlockDeviceMappings=[{"DeviceName": image["RootDeviceName"],
                "Ebs": {"VolumeSize": 30, "VolumeType": "gp3", "DeleteOnTermination": True}}],
            UserData=server_bootstrap(), TagSpecifications=[{"ResourceType": "instance", "Tags": [
                {"Key": "Name", "Value": "sebs-sql-server"}]}])
        request_path.write_text(json.dumps(request, indent=2) + "\n")
    archive = folder / "service.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name in ["evaluation/__init__.py", "evaluation/sql/__init__.py", "evaluation/sql/service.py",
                     "benchmarks/700.analytics/710.sql-query/python/queries.py"]:
            bundle.add(ROOT / name, arcname=name, recursive=False)
        data = json.dumps(trace).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    key = f"evaluation/sql/ec2/{digest}/service.tar.gz"
    boto3.client("s3", region_name=region).upload_file(str(archive), bucket, key)
    (folder / "deployment.json").write_text(json.dumps(dict(bucket=bucket, key=key, sha256=digest), indent=2)+"\n")
    print("Launching SQL server; consumes AWS usage/credits. Exact request persisted.", flush=True)
    instance = ec2.run_instances(**request)["Instances"][0]
    state = dict(instance_id=instance["InstanceId"], region=region, instance_type=instance["InstanceType"],
        ami=instance["ImageId"], security_group_id=request["NetworkInterfaces"][0]["Groups"][0],
        launch_time=instance["LaunchTime"].isoformat())
    (folder / "instance.json").write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps(state, indent=2))
    print("Wait for SSM Online and /opt/sebs-sql/bootstrap.complete before deploy_service.")


if __name__ == "__main__":
    main()
