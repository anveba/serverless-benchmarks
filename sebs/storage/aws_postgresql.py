# Copyright 2020-2026 ETH Zurich and the SeBS authors. All rights reserved.
"""AWS resources for the PostgreSQL storage backend, independent of any workload."""
import json
from pathlib import Path
import time
import uuid
import hashlib
import io
import tarfile

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from sebs.storage.aws_postgresql_bootstrap import user_data


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def template(config):
    ref = lambda name: {"Ref": name}
    get = lambda name, attr: {"Fn::GetAtt": [name, attr]}
    policy = lambda statements: {"Version": "2012-10-17", "Statement": statements}
    trust = lambda service: policy(
        [
            {
                "Effect": "Allow",
                "Principal": {"Service": service},
                "Action": "sts:AssumeRole",
            }
        ]
    )
    resources = {
        "StagingBucket": {
            "Type": "AWS::S3::Bucket",
            "Properties": {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                }
            },
        },
        "Secret": {
            "Type": "AWS::SecretsManager::Secret",
            "Properties": {
                "Description": "SeBS PostgreSQL read-only connection; EC2 bootstrap publishes value",
                "SecretString": '{"pending":true}',
            },
        },
        "FunctionGroup": {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": "SeBS PostgreSQL Lambda clients; no inbound access",
                "VpcId": ref("VpcId"),
            },
        },
        "DatabaseGroup": {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": "Private PostgreSQL; only benchmark Lambda clients",
                "VpcId": ref("VpcId"),
                "SecurityGroupIngress": [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 5432,
                        "ToPort": 5432,
                        "SourceSecurityGroupId": ref("FunctionGroup"),
                    }
                ],
            },
        },
        "EndpointGroup": {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": "Secrets Manager endpoint for benchmark clients and bootstrap",
                "VpcId": ref("VpcId"),
                "SecurityGroupIngress": [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 443,
                        "ToPort": 443,
                        "SourceSecurityGroupId": ref(group),
                    }
                    for group in ["FunctionGroup", "DatabaseGroup"]
                ],
            },
        },
        "SecretsEndpoint": {
            "Type": "AWS::EC2::VPCEndpoint",
            "Properties": {
                "VpcId": ref("VpcId"),
                "VpcEndpointType": "Interface",
                "PrivateDnsEnabled": True,
                "ServiceName": {
                    "Fn::Sub": "com.amazonaws.${AWS::Region}.secretsmanager"
                },
                "SubnetIds": [ref("SubnetId")],
                "SecurityGroupIds": [ref("EndpointGroup")],
            },
        },
        "FunctionRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": trust("lambda.amazonaws.com"),
                "ManagedPolicyArns": [
                    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole",
                ],
                "Policies": [
                    {
                        "PolicyName": "ReadPostgresCredentials",
                        "PolicyDocument": policy(
                            [
                                {
                                    "Effect": "Allow",
                                    "Action": ["secretsmanager:GetSecretValue"],
                                    "Resource": ref("Secret"),
                                }
                            ]
                        ),
                    }
                ],
            },
        },
        "DatabaseRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": trust("ec2.amazonaws.com"),
                "ManagedPolicyArns": [
                    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
                ],
                "Policies": [
                    {
                        "PolicyName": "BootstrapAndLoad",
                        "PolicyDocument": policy(
                            [
                                {
                                    "Effect": "Allow",
                                    "Action": ["secretsmanager:PutSecretValue"],
                                    "Resource": ref("Secret"),
                                },
                                {
                                    "Effect": "Allow",
                                    "Action": ["s3:GetObject"],
                                    "Resource": {
                                        "Fn::Join": [
                                            "",
                                            [
                                                {
                                                    "Fn::GetAtt": [
                                                        "StagingBucket",
                                                        "Arn",
                                                    ]
                                                },
                                                "/*",
                                            ],
                                        ]
                                    },
                                },
                                {
                                    "Effect": "Allow",
                                    "Action": ["s3:PutObject"],
                                    "Resource": {
                                        "Fn::Join": [
                                            "",
                                            [
                                                {
                                                    "Fn::GetAtt": [
                                                        "StagingBucket",
                                                        "Arn",
                                                    ]
                                                },
                                                "/reports/*",
                                            ],
                                        ]
                                    },
                                },
                            ]
                        ),
                    }
                ],
            },
        },
        "DatabaseProfile": {
            "Type": "AWS::IAM::InstanceProfile",
            "Properties": {"Roles": [ref("DatabaseRole")]},
        },
        "Database": {
            "Type": "AWS::EC2::Instance",
            "DependsOn": ["SecretsEndpoint"],
            "Properties": {
                "ImageId": ref("ImageId"),
                "InstanceType": ref("InstanceType"),
                "IamInstanceProfile": ref("DatabaseProfile"),
                "MetadataOptions": {
                    "HttpTokens": "required",
                    "HttpEndpoint": "enabled",
                },
                "NetworkInterfaces": [
                    {
                        "DeviceIndex": "0",
                        "SubnetId": ref("SubnetId"),
                        "GroupSet": [ref("DatabaseGroup")],
                        "AssociatePublicIpAddress": True,
                    }
                ],
                "BlockDeviceMappings": [
                    {
                        "DeviceName": "/dev/sda1",
                        "Ebs": {
                            "VolumeSize": 64,
                            "VolumeType": "gp3",
                            "DeleteOnTermination": True,
                        },
                    }
                ],
                "Tags": [
                    {
                        "Key": "Name",
                        "Value": {"Fn::Sub": "${AWS::StackName}-postgresql"},
                    }
                ],
                "UserData": {
                    "Fn::Base64": {
                        "Fn::Sub": [
                            user_data(config.image, config.cpus, config.memory_mb),
                            {"Region": ref("AWS::Region"), "SecretArn": ref("Secret")},
                        ]
                    }
                },
            },
        },
    }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "SeBS self-hosted PostgreSQL (one EC2, no RDS)",
        "Parameters": {
            "VpcId": {"Type": "AWS::EC2::VPC::Id"},
            "SubnetId": {"Type": "AWS::EC2::Subnet::Id"},
            "ImageId": {"Type": "AWS::EC2::Image::Id"},
            "InstanceType": {"Type": "String"},
        },
        "Resources": resources,
        "Outputs": {
            "InstanceId": {"Value": ref("Database")},
            "SecretArn": {"Value": ref("Secret")},
            "LambdaRoleArn": {"Value": get("FunctionRole", "Arn")},
            "LambdaGroupId": {"Value": ref("FunctionGroup")},
            "PrivateIp": {"Value": get("Database", "PrivateIp")},
            "EndpointId": {"Value": ref("SecretsEndpoint")},
            "StagingBucket": {"Value": ref("StagingBucket")},
        },
    }


def preflight(region):
    ec2 = boto3.client("ec2", region_name=region)
    print(
        "Account:",
        boto3.client("sts").get_caller_identity()["Account"],
        "Region:",
        region,
    )
    for vpc in ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])[
        "Vpcs"
    ]:
        print("Default VPC:", vpc["VpcId"])
        for subnet in ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}]
        )["Subnets"]:
            print("Subnet:", subnet["SubnetId"], "AZ:", subnet["AvailabilityZone"])
    images = ec2.describe_images(
        Owners=["099720109477"],
        Filters=[
            {
                "Name": "name",
                "Values": [
                    "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
                ],
            },
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
        ],
    )["Images"]
    if not images:
        raise ValueError("No Ubuntu 24.04 x64 AMI found")
    image = max(images, key=lambda item: item["CreationDate"])
    print("Ubuntu AMI:", image["ImageId"], image["Name"])
    print(
        "No resources created. Choose a subnet with an Internet Gateway route for EC2 bootstrap."
    )


def provision(config, session=None):
    session = session or boto3.Session()
    folder = Path(config.state_dir)
    region, subnet_id, image_id, instance_type = (
        config.region,
        config.subnet_id,
        config.ami_id,
        config.instance_type,
    )
    ec2 = session.client("ec2", region_name=region)
    subnet = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
    tables = ec2.describe_route_tables(
        Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
    )["RouteTables"]
    if not tables:
        tables = ec2.describe_route_tables(
            Filters=[
                {"Name": "vpc-id", "Values": [subnet["VpcId"]]},
                {"Name": "association.main", "Values": ["true"]},
            ]
        )["RouteTables"]
    if not any(
        route.get("DestinationCidrBlock") == "0.0.0.0/0"
        and route.get("GatewayId", "").startswith("igw-")
        and route.get("State") == "active"
        for table in tables
        for route in table["Routes"]
    ):
        raise ValueError(
            "Choose a subnet with an active Internet Gateway default route for EC2 bootstrap"
        )
    info = ec2.describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"][
        0
    ]
    if (
        info["MemoryInfo"]["SizeInMiB"] < config.memory_mb + 1024
        or info["VCpuInfo"]["DefaultVCpus"] < config.cpus
        or "x86_64" not in info["ProcessorInfo"]["SupportedArchitectures"]
    ):
        raise ValueError(
            "Choose an x64 instance with at least 2 vCPUs and 8 GiB (container uses 2 vCPUs / 6 GiB)"
        )
    for attr in ["enableDnsSupport", "enableDnsHostnames"]:
        result = ec2.describe_vpc_attribute(VpcId=subnet["VpcId"], Attribute=attr)
        if not result[attr[0].upper() + attr[1:]]["Value"]:
            raise ValueError("The VPC must have DNS support and hostnames enabled")
    parameters = {
        "VpcId": subnet["VpcId"],
        "SubnetId": subnet_id,
        "ImageId": image_id,
        "InstanceType": instance_type,
    }
    plan_path = folder / "stack.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        if plan["region"] != region or plan["parameters"] != parameters:
            raise ValueError("Saved stack request differs; use another state directory")
    else:
        plan = {
            "stack_name": "sebs-postgres-" + uuid.uuid4().hex[:10],
            "region": region,
            "request_token": str(uuid.uuid4()),
            "parameters": parameters,
        }
        write_json(plan_path, plan)
    cf = session.client("cloudformation", region_name=region)
    try:
        stack = cf.describe_stacks(StackName=plan["stack_name"])["Stacks"][0]
    except ClientError as error:
        if (
            error.response["Error"]["Code"] != "ValidationError"
            or "does not exist" not in error.response["Error"]["Message"]
        ):
            raise
    else:
        print(
            "Stack already submitted:",
            stack["StackStatus"],
            "; use status, do not submit another stack.",
        )
        return
    endpoints = ec2.describe_vpc_endpoints(
        Filters=[
            {"Name": "vpc-id", "Values": [subnet["VpcId"]]},
            {
                "Name": "service-name",
                "Values": [f"com.amazonaws.{region}.secretsmanager"],
            },
        ]
    )["VpcEndpoints"]
    if any(
        e.get("PrivateDnsEnabled")
        and e["State"] not in ("deleted", "deleting", "failed", "rejected")
        for e in endpoints
    ):
        raise ValueError(
            "This VPC already has a private-DNS Secrets Manager endpoint. Use a separate test VPC or adapt its existing endpoint access before provisioning."
        )
    document = template(config)
    write_json(folder / "stack-template.json", document)
    print(
        "Creating one EC2 PostgreSQL server, 64 GiB disk, private endpoint and secret; AWS usage/charges apply.",
        flush=True,
    )
    result = cf.create_stack(
        StackName=plan["stack_name"],
        TemplateBody=json.dumps(document),
        Parameters=[
            {"ParameterKey": key, "ParameterValue": value}
            for key, value in parameters.items()
        ],
        Capabilities=["CAPABILITY_IAM"],
        ClientRequestToken=plan["request_token"],
        Tags=[{"Key": "sebs-experiment", "Value": "sql-postgresql"}],
    )
    plan["stack_id"] = result["StackId"]
    write_json(plan_path, plan)
    print("Stack submitted. Run status; no SQL benchmark query executed.")


def stack_info(folder, session=None):
    session = session or boto3.Session()
    plan = json.loads((folder / "stack.json").read_text())
    cf = session.client("cloudformation", region_name=plan["region"])
    stack = cf.describe_stacks(StackName=plan.get("stack_id", plan["stack_name"]))[
        "Stacks"
    ][0]
    outputs = {
        item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])
    }
    return plan, stack, outputs


class AWSPostgreSQL:
    def __init__(self, config, session=None):
        self.config = config
        self.session = session or boto3.Session()
        self.folder = Path(config.state_dir)
        self.ssm = self.session.client(
            "ssm",
            region_name=config.region,
            config=Config(
                connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1}
            ),
        )

    def info(self):
        return stack_info(self.folder, self.session)

    def command(self, operation, script, timeout=3600):
        """Checkpoint before sending: an ambiguous send must never be auto-resubmitted."""
        path = self.folder / "commands" / (operation + ".json")
        plan, stack, outputs = self.info()
        if path.exists():
            state = json.loads(path.read_text())
            if not state.get("command_id"):
                raise RuntimeError(
                    "SSM submission outcome unknown; inspect the storage commands before retrying: "
                    + str(path)
                )
        else:
            state = {"instance_id": outputs["InstanceId"], "command_id": None}
            write_json(path, state)
            response = self.ssm.send_command(
                InstanceIds=[state["instance_id"]],
                DocumentName="AWS-RunShellScript",
                TimeoutSeconds=600,
                Parameters={"commands": [script], "executionTimeout": [str(timeout)]},
            )
            state["command_id"] = response["Command"]["CommandId"]
            write_json(path, state)
        deadline = time.monotonic() + timeout + 30
        while time.monotonic() < deadline:
            try:
                result = self.ssm.get_command_invocation(
                    CommandId=state["command_id"], InstanceId=state["instance_id"]
                )
            except self.ssm.exceptions.InvocationDoesNotExist:
                time.sleep(2)
                continue
            if result["Status"] == "Success":
                write_json(
                    path.with_suffix(".result.json"),
                    {
                        key: result.get(key, "")
                        for key in [
                            "Status",
                            "StandardOutputContent",
                            "StandardErrorContent",
                        ]
                    },
                )
                return result["StandardOutputContent"]
            if result["Status"] in ("Cancelled", "TimedOut", "Failed", "Cancelling"):
                write_json(
                    path.with_suffix(".result.json"),
                    {
                        key: result.get(key, "")
                        for key in [
                            "Status",
                            "StandardOutputContent",
                            "StandardErrorContent",
                        ]
                    },
                )
                raise RuntimeError(
                    "SQL storage SSM operation "
                    + result["Status"]
                    + "; inspect "
                    + str(path.with_suffix(".result.json"))
                )
            time.sleep(3)
        raise TimeoutError(
            "SSM is still running. Keep the same configuration/state; command ID is in "
            + str(path)
        )

    def start(self):
        provision(self.config, self.session)
        deadline = time.monotonic() + self.config.startup_timeout
        last = None
        while time.monotonic() < deadline:
            plan, stack, outputs = self.info()
            state = stack["StackStatus"]
            if state != last:
                print("PostgreSQL stack:", state, flush=True)
                last = state
            if state == "CREATE_COMPLETE":
                self.config.instance_id = outputs["InstanceId"]
                self.config.host = outputs["PrivateIp"]
                ec2 = self.session.client("ec2", region_name=self.config.region)
                instance = ec2.describe_instances(InstanceIds=[outputs["InstanceId"]])[
                    "Reservations"
                ][0]["Instances"][0]
                if instance["State"]["Name"] == "stopped":
                    ec2.start_instances(InstanceIds=[outputs["InstanceId"]])
                hosts = self.ssm.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [outputs["InstanceId"]]}]
                )["InstanceInformationList"]
                if hosts and hosts[0]["PingStatus"] == "Online":
                    # Wait inside SSM; readiness is not an analytical query.
                    script = """set -eu
for attempt in $(seq 1 180); do
  if test -f /opt/sebs-postgres/bootstrap.complete && docker exec sebs-postgresql pg_isready -U postgres -d sebs >/dev/null 2>&1; then echo POSTGRES_READY; exit 0; fi
  if test -f /opt/sebs-postgres/bootstrap.failed; then echo BOOTSTRAP_FAILED; exit 1; fi
  sleep 5
done
exit 1
"""
                    self.command("readiness-" + uuid.uuid4().hex, script, timeout=960)
                    return
            elif state.endswith(("FAILED", "ROLLBACK_COMPLETE")) or "ROLLBACK" in state:
                raise RuntimeError(
                    "SQL storage stack "
                    + state
                    + "; inspect CloudFormation events for "
                    + plan["stack_name"]
                )
            time.sleep(5)
        raise TimeoutError(
            "SQL storage provisioning continues; rerun start with the same state/configuration"
        )

    def status(self):
        if not (self.folder / "stack.json").exists():
            return {"provider": "aws", "state": "not-created"}
        plan, stack, outputs = self.info()
        result = {
            "provider": "aws",
            "state": stack["StackStatus"],
            "stack_id": stack["StackId"],
        }
        if outputs and stack["StackStatus"] == "CREATE_COMPLETE":
            result["instance_id"] = outputs.get("InstanceId")
            ec2 = self.session.client("ec2", region_name=self.config.region)
            instance = ec2.describe_instances(InstanceIds=[outputs["InstanceId"]])[
                "Reservations"
            ][0]["Instances"][0]
            result["instance_state"] = instance["State"]["Name"]
        return result

    def envs(self, external=True):
        plan, stack, outputs = self.info()
        if stack["StackStatus"] != "CREATE_COMPLETE":
            raise RuntimeError(
                "PostgreSQL storage is not ready; run sebs storage start sql"
            )
        return {
            "SEBS_SQL_CONFIG": json.dumps(
                {
                    "type": "postgresql",
                    "secret_arn": outputs["SecretArn"],
                    "region": self.config.region,
                }
            )
        }

    def function_options(self):
        plan, stack, outputs = self.info()
        return {
            "Role": outputs["LambdaRoleArn"],
            "Environment": {"Variables": self.envs()},
            "VpcConfig": {
                "SubnetIds": [self.config.subnet_id],
                "SecurityGroupIds": [outputs["LambdaGroupId"]],
            },
        }

    def prepare_dataset(self, dataset, replace=False):
        """The benchmark supplies files; this backend handles S3/SSM transport."""
        import copy
        import sebs.storage.postgresql_loader as loader

        plan, stack, outputs = self.info()
        if stack["StackStatus"] != "CREATE_COMPLETE":
            raise RuntimeError("Start PostgreSQL storage before invoking a benchmark")
        cached_report = self.folder / "datasets" / (dataset["namespace"] + ".json")
        if cached_report.exists() and not replace:
            previous = json.loads(cached_report.read_text())
            if previous.get("fingerprint") == dataset["fingerprint"]:
                return previous
        bucket = outputs["StagingBucket"]
        s3 = self.session.client("s3", region_name=self.config.region)
        remote = copy.deepcopy(dataset)
        remote["bucket"] = bucket
        for table, value in remote["tables"].items():
            for item in value["files"]:
                source = item.pop("path")
                key = (
                    "datasets/"
                    + dataset["fingerprint"]
                    + "/"
                    + table
                    + "/"
                    + item["sha256"]
                    + ".parquet"
                )
                s3.upload_file(source, bucket, key)
                item["key"] = key
        # A single self-contained generic loader; no evaluation or benchmark module is shipped.
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, value in [
                ("loader.py", Path(loader.__file__).read_bytes()),
                ("dataset.json", json.dumps(remote).encode()),
            ]:
                entry = tarfile.TarInfo(name)
                entry.size = len(value)
                archive.addfile(entry, io.BytesIO(value))
        raw = stream.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        key = "loaders/" + digest + ".tar.gz"
        s3.put_object(Bucket=bucket, Key=key, Body=raw)
        report_key = (
            "reports/" + dataset["namespace"] + "/" + dataset["fingerprint"] + ".json"
        )
        operation = "load-" + dataset["namespace"] + "-" + dataset["fingerprint"]
        if replace:
            operation += "-" + uuid.uuid4().hex
        script = f"""set -eu
export HOME=/root
test -f /opt/sebs-postgres/bootstrap.complete
/opt/sebs-postgres/venv/bin/python - <<'REMOTE'
import boto3
import hashlib
from pathlib import Path
import subprocess
import tarfile
client=boto3.client('s3',region_name={self.config.region!r})
root=Path('/opt/sebs-postgres/datasets')/{dataset['namespace']!r}
root.mkdir(parents=True,exist_ok=True)
p=root/'loader.tar.gz'
client.download_file({bucket!r},{key!r},str(p))
if hashlib.sha256(p.read_bytes()).hexdigest() != {digest!r}:
    raise RuntimeError('Loader bundle hash differs')
with tarfile.open(p) as bundle:
    if any(not m.isfile() or m.name not in ('loader.py','dataset.json') for m in bundle.getmembers()):
        raise RuntimeError('Unexpected loader bundle member')
    bundle.extractall(root,filter='data')
report=root/'report.json'
subprocess.run(['/opt/sebs-postgres/venv/bin/python',str(root/'loader.py'),'--dataset',str(root/'dataset.json'),
    '--region',{self.config.region!r},'--report',str(report)]+{(['--replace'] if replace else [])!r},check=True)
client.upload_file(str(report),{bucket!r},{report_key!r})
REMOTE
"""
        self.command(operation, script)
        local_report = self.folder / "datasets" / (dataset["namespace"] + ".json")
        local_report.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, report_key, str(local_report))
        return json.loads(local_report.read_text())

    def stop(self):
        plan, stack, outputs = self.info()
        if outputs.get("InstanceId"):
            self.session.client("ec2", region_name=self.config.region).stop_instances(
                InstanceIds=[outputs["InstanceId"]]
            )
        return {
            "state": "stopping",
            "note": "EBS, endpoint and secret remain billable; storage remove --delete-data deletes the stack",
        }

    def remove(self, delete_data=False, dry_run=False):
        if not delete_data and not dry_run:
            raise ValueError(
                "Removing AWS PostgreSQL destroys its database. Pass --delete-data explicitly."
            )
        plan, stack, outputs = self.info()
        if dry_run:
            return {
                "stack": stack["StackId"],
                "action": "delete database, disk, staging bucket, endpoint, secret and roles",
            }
        lam = self.session.client("lambda", region_name=self.config.region)
        for page in lam.get_paginator("list_functions").paginate():
            owned = [
                f["FunctionName"]
                for f in page["Functions"]
                if f.get("Role") == outputs.get("LambdaRoleArn")
            ]
            if owned:
                raise RuntimeError(
                    "Remove SQL Lambda functions through SeBS first: "
                    + ", ".join(owned)
                )
        if outputs.get("LambdaGroupId"):
            interfaces = self.session.client(
                "ec2", region_name=self.config.region
            ).describe_network_interfaces(
                Filters=[{"Name": "group-id", "Values": [outputs["LambdaGroupId"]]}]
            )[
                "NetworkInterfaces"
            ]
            if interfaces:
                raise RuntimeError(
                    "Lambda interfaces are still draining. Keep the role and retry removal after AWS removes them."
                )
        if outputs.get("StagingBucket"):
            bucket = self.session.resource("s3", region_name=self.config.region).Bucket(
                outputs["StagingBucket"]
            )
            bucket.objects.all().delete()
        self.session.client(
            "cloudformation", region_name=self.config.region
        ).delete_stack(StackName=stack["StackId"])
        return {
            "state": "DELETE_IN_PROGRESS",
            "stack_id": stack["StackId"],
            "note": "Poll storage status until DELETE_COMPLETE",
        }
