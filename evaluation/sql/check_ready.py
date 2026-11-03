"""Check bootstrap and cloud connectivity without submitting SQL queries."""
import argparse
import json
from pathlib import Path

import boto3
from botocore.exceptions import WaiterError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=["server", "driver"])
    parser.add_argument("--state-dir", type=Path, default=Path("results/sql-ec2"))
    args = parser.parse_args()
    state = json.loads((args.state_dir / ("instance.json" if args.target == "server" else "driver-instance.json")).read_text())
    ssm = boto3.client("ssm", region_name=state["region"])
    info = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [state["instance_id"]]}])["InstanceInformationList"]
    if not info or info[0]["PingStatus"] != "Online":
        raise SystemExit("SSM not Online yet; retry this check later. Do not relaunch.")
    base = "/opt/sebs-sql" if args.target == "server" else "/opt/sebs-driver"
    script = f"set -eu\nexport HOME=/root\ntest -f {base}/bootstrap.complete\n{base}/venv/bin/python -c \"import duckdb; print('DuckDB:', duckdb.__version__)\"\n"
    if args.target == "driver":
        script += '''cd /opt/sebs-driver/repo
/opt/sebs-driver/venv/bin/python - <<'REMOTE'
import json
from pathlib import Path
import boto3
from evaluation.sql.replay_service import ServiceClient, verify_snapshot
from evaluation.sql.lambda_client import LambdaClient
plan = json.loads(Path("comparison/private-driver.json").read_text())
trace = json.loads(Path("comparison/trace.json").read_text())
token = boto3.client("ssm", region_name=plan["region"]).get_parameter(Name=plan["token_parameter"], WithDecryption=True)["Parameter"]["Value"]
verify_snapshot(ServiceClient(plan["server_endpoint"], token).health(), trace)
client = LambdaClient(plan["lambda_function"], trace["input"], plan["region"])
client.verify(client.health(), trace)
print("Private EC2 health, manifest and Lambda configuration: OK. No SQL submitted.")
REMOTE
'''
    command_id = ssm.send_command(InstanceIds=[state["instance_id"]], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script], "executionTimeout": ["120"]})["Command"]["CommandId"]
    print("Readiness command:", command_id, flush=True)
    try:
        ssm.get_waiter("command_executed").wait(CommandId=command_id, InstanceId=state["instance_id"],
            WaiterConfig={"Delay": 2, "MaxAttempts": 20})
    except WaiterError:
        pass
    result = ssm.get_command_invocation(CommandId=command_id, InstanceId=state["instance_id"])
    print("Status:", result["Status"])
    print(result.get("StandardOutputContent", ""))
    print(result.get("StandardErrorContent", ""))
    if result["Status"] != "Success":
        raise SystemExit("Not ready. Check cloud-init/SSM output and retry readiness, not provisioning.")


if __name__ == "__main__":
    main()
