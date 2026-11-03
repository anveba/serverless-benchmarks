"""Install the previously uploaded service bundle on the comparison EC2 instance."""
import argparse
import json
from pathlib import Path

import boto3
from botocore.exceptions import WaiterError


def installation_script(deployment):
    # JSON strings are also valid Python literals for these string-valued fields.
    bucket = json.dumps(deployment["bucket"])
    key = json.dumps(deployment["key"])
    digest = json.dumps(deployment["sha256"])
    return f'''set -eu
/opt/sebs-sql/venv/bin/python - <<'REMOTE'
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path
import boto3

base = Path("/opt/sebs-sql")
if not (base / "bootstrap.complete").exists():
    raise RuntimeError("Instance bootstrap has not completed")
archive = base / "service.tar.gz"
boto3.client("s3", region_name="us-east-1").download_file({bucket}, {key}, str(archive))
if hashlib.sha256(archive.read_bytes()).hexdigest() != {digest}:
    raise RuntimeError("Bundle checksum mismatch")
repo = base / "repo"
repo.mkdir(exist_ok=True)
with tarfile.open(archive, "r:gz") as bundle:
    bundle.extractall(repo, filter="data")
print("Bundle verified and extracted.", flush=True)

subprocess.run([
    "runuser", "-u", "ubuntu", "--", str(base / "venv/bin/python"), "-c",
    "import duckdb; assert duckdb.__version__ == '2.0.0'; "
    "duckdb.install_extension('httpfs'); print('httpfs installed for service user')",
], check=True)
unit = """[Unit]
Description=SeBS persistent SQL comparison service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/sebs-sql/repo
Environment=PYTHONUNBUFFERED=1
Environment=AWS_DEFAULT_REGION=us-east-1
Environment=AWS_REGION=us-east-1
ExecStart=/opt/sebs-sql/venv/bin/python -m evaluation.sql.service --manifest /opt/sebs-sql/repo/manifest.json --region us-east-1 --host 127.0.0.1 --port 8080 --workers 2 --threads 2 --memory-limit 1536MB
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""
Path("/etc/systemd/system/sebs-sql.service").write_text(unit)
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "enable", "sebs-sql.service"], check=True)
subprocess.run(["systemctl", "restart", "sebs-sql.service"], check=True)
print("Service enabled and started.", flush=True)
REMOTE
curl --fail --silent --show-error --retry 15 --retry-connrefused --retry-delay 1 http://127.0.0.1:8080/health
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("results/sql-ec2"))
    args = parser.parse_args()
    state = json.loads((args.state_dir / "instance.json").read_text())
    deployment_path = args.state_dir / "deployment.json"
    deployment = json.loads(deployment_path.read_text())
    ssm = boto3.client("ssm", region_name=state["region"])
    command_id = ssm.send_command(
        InstanceIds=[state["instance_id"]], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [installation_script(deployment)], "executionTimeout": ["600"]},
    )["Command"]["CommandId"]
    deployment["install_command_id"] = command_id
    deployment_path.write_text(json.dumps(deployment, indent=2) + "\n")
    print("Install command:", command_id, flush=True)
    try:
        ssm.get_waiter("command_executed").wait(
            CommandId=command_id, InstanceId=state["instance_id"],
            WaiterConfig={"Delay": 2, "MaxAttempts": 60},
        )
    except WaiterError:
        pass
    result = ssm.get_command_invocation(CommandId=command_id, InstanceId=state["instance_id"])
    print("Status:", result["Status"])
    print(result["StandardOutputContent"])
    print(result["StandardErrorContent"])
    if result["Status"] != "Success":
        print("Inspect this command ID before rerunning; it may still be executing.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
