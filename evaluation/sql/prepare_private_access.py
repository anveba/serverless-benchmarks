"""Prepare private EC2 driver access without launching another instance.

Run locally with deployment credentials; keep generated state under results/.
"""
import argparse
import json
import secrets
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, WaiterError


def role_for_ec2(iam, name):
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
             "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        role = iam.get_role(RoleName=name)["Role"]
        statements = role["AssumeRolePolicyDocument"].get("Statement", [])
        if not any(s.get("Effect") == "Allow" and
                   s.get("Principal", {}).get("Service") == "ec2.amazonaws.com" and
                   s.get("Action") == "sts:AssumeRole" for s in statements):
            raise ValueError(f"Existing {name} does not have the expected EC2 trust policy")
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))
    iam.attach_role_policy(RoleName=name,
        PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    try:
        profile = iam.get_instance_profile(InstanceProfileName=name)["InstanceProfile"]
    except iam.exceptions.NoSuchEntityException:
        profile = iam.create_instance_profile(InstanceProfileName=name)["InstanceProfile"]
    names = [r["RoleName"] for r in profile["Roles"]]
    if not names:
        iam.add_role_to_instance_profile(InstanceProfileName=name, RoleName=name)
    elif names != [name]:
        raise ValueError(f"Unexpected roles in profile {name}: {names}")


def remote_configuration(parameter_name, private_ip):
    return f'''set -eu
/opt/sebs-sql/venv/bin/python - <<'REMOTE'
import os
from pathlib import Path
import subprocess
import time
import boto3
from botocore.exceptions import ClientError

ssm = boto3.client("ssm", region_name="us-east-1")
for attempt in range(15):
    try:
        value = ssm.get_parameter(Name={parameter_name!r}, WithDecryption=True)["Parameter"]["Value"]
        break
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("AccessDenied", "AccessDeniedException") or attempt == 14:
            raise
        time.sleep(2)
path = "/etc/sebs-sql.env"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as output:
    output.write("SEBS_SQL_SERVICE_TOKEN=" + value + "\\n")
os.chmod(path, 0o600)
unit = Path("/etc/systemd/system/sebs-sql.service")
text = unit.read_text()
old = "--host 127.0.0.1"
new = "--host " + {private_ip!r}
if old not in text and new not in text:
    raise RuntimeError("Unexpected service bind address; unit left unchanged")
text = text.replace(old, new)
if "EnvironmentFile=/etc/sebs-sql.env" not in text:
    text = text.replace("[Service]\\n", "[Service]\\nEnvironmentFile=/etc/sebs-sql.env\\n", 1)
unit.write_text(text)
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "restart", "sebs-sql.service"], check=True)
print("Service configured for private-IP access with bearer authentication.")
REMOTE
systemctl is-active sebs-sql.service
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("results/sql-ec2"))
    parser.add_argument("--lambda-function", help="Deployed SQL Lambda name; otherwise read processed workload results")
    args = parser.parse_args()
    state = json.loads((args.state_dir / "instance.json").read_text())
    deployment = json.loads((args.state_dir / "deployment.json").read_text())
    region = state["region"]
    if region != "us-east-1":
        raise ValueError("This setup currently targets the recorded us-east-1 deployment")
    ec2 = boto3.client("ec2", region_name=region)
    iam = boto3.client("iam")
    ssm = boto3.client("ssm", region_name=region)
    instance = ec2.describe_instances(InstanceIds=[state["instance_id"]])["Reservations"][0]["Instances"][0]
    if instance["State"]["Name"] != "running":
        raise ValueError("SQL service instance must be running")
    account = boto3.client("sts").get_caller_identity()["Account"]
    driver_role = "sebs-sql-driver-role"
    role_for_ec2(iam, driver_role)
    group_name = "sebs-sql-driver"
    groups = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [instance["VpcId"]]},
        {"Name": "group-name", "Values": [group_name]},
    ])["SecurityGroups"]
    if groups:
        if groups[0]["IpPermissions"]:
            raise ValueError("Existing driver group has inbound rules; inspect it first")
        group_id = groups[0]["GroupId"]
    else:
        group_id = ec2.create_security_group(GroupName=group_name,
            Description="SQL load driver; SSM management, no inbound ports",
            VpcId=instance["VpcId"])["GroupId"]
    parameter = f"/sebs/sql-comparison/{state['instance_id']}/service-token"
    try:
        ssm.get_parameter(Name=parameter)
    except ssm.exceptions.ParameterNotFound:
        ssm.put_parameter(Name=parameter, Type="SecureString", Value=secrets.token_hex(32))
    parameter_arn = f"arn:aws:ssm:{region}:{account}:parameter{parameter}"
    secret_policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": "ssm:GetParameter", "Resource": parameter_arn}]}
    server_profile = instance["IamInstanceProfile"]["Arn"].split("/")[-1]
    server_roles = iam.get_instance_profile(InstanceProfileName=server_profile)["InstanceProfile"]["Roles"]
    if len(server_roles) != 1:
        raise ValueError("Expected one role on the SQL server")
    iam.put_role_policy(RoleName=server_roles[0]["RoleName"], PolicyName="ReadSqlServiceToken",
                        PolicyDocument=json.dumps(secret_policy))
    bucket = deployment["bucket"]
    # Derive the function name from the recorded, processed Lambda workload.
    if args.lambda_function:
        function = args.lambda_function
    else:
        lambda_results = json.loads(Path("results/sql-aws-workload-01/sql-workload/invocations-processed.json").read_text())
        functions = list(lambda_results["_invocations"])
        if len(functions) != 1:
            raise ValueError("Expected one Lambda function in the recorded workload")
        function = functions[0]
    driver_policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{bucket}"},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/*"},
        {"Effect": "Allow", "Action": ["s3:PutObject"],
         "Resource": f"arn:aws:s3:::{bucket}/evaluation/sql/driver-results/*"},
        {"Effect": "Allow", "Action": ["lambda:InvokeFunction", "lambda:GetFunctionConfiguration"],
         "Resource": f"arn:aws:lambda:{region}:{account}:function:{function}"},
        *secret_policy["Statement"],
    ]}
    iam.put_role_policy(RoleName=driver_role, PolicyName="SqlComparisonDriver",
                        PolicyDocument=json.dumps(driver_policy))
    try:
        ec2.authorize_security_group_ingress(GroupId=state["security_group_id"], IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080,
            "UserIdGroupPairs": [{"GroupId": group_id}],
        }])
    except ClientError as error:
        if error.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise
    plan = {"region": region, "server_instance_id": state["instance_id"],
            "server_private_ip": instance["PrivateIpAddress"],
            "server_endpoint": f"http://{instance['PrivateIpAddress']}:8080",
            "vpc_id": instance["VpcId"], "subnet_id": instance["SubnetId"],
            "driver_security_group_id": group_id, "driver_instance_profile": driver_role,
            "token_parameter": parameter, "lambda_function": function, "bucket": bucket}
    state_path = args.state_dir / "private-driver.json"
    # Save identifiers only; never write the bearer token or AWS keys locally.
    state_path.write_text(json.dumps(plan, indent=2) + "\n")
    command_id = ssm.send_command(InstanceIds=[state["instance_id"]],
        DocumentName="AWS-RunShellScript", Parameters={"commands": [
            remote_configuration(parameter, instance["PrivateIpAddress"])]})["Command"]["CommandId"]
    plan["private_access_command_id"] = command_id
    state_path.write_text(json.dumps(plan, indent=2) + "\n")
    try:
        ssm.get_waiter("command_executed").wait(CommandId=command_id,
            InstanceId=state["instance_id"], WaiterConfig={"Delay": 2, "MaxAttempts": 20})
    except WaiterError:
        pass
    command = ssm.get_command_invocation(CommandId=command_id, InstanceId=state["instance_id"])
    print("Private service update:", command["Status"])
    print(command["StandardOutputContent"])
    print(command["StandardErrorContent"])
    if command["Status"] != "Success":
        print("Command ID:", command_id, "Inspect before repeating.")
        raise SystemExit(1)
    retired = []
    for page in ssm.get_paginator("describe_sessions").paginate(State="Active", Filters=[
        {"key": "Target", "value": state["instance_id"]},
    ]):
        for session in page["Sessions"]:
            if session.get("DocumentName") in {
                "AWS-StartPortForwardingSession", "AWS-StartPortForwardingSessionToRemoteHost"
            }:
                ssm.terminate_session(SessionId=session["SessionId"])
                retired.append(session["SessionId"])
    print("Retired old port-forwarding sessions:", len(retired))
    print("Driver security group:", group_id)
    print("Private service endpoint:", plan["server_endpoint"])
    print("Saved:", state_path)
    print("No driver instance launched yet. Existing SQL service and results retained.")


if __name__ == "__main__":
    main()
