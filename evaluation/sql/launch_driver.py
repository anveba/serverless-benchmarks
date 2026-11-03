"""Bundle current comparison code and launch a separate cloud workload driver."""
import argparse
import hashlib
import io
import json
import tarfile
import uuid
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[2]


def make_bundle(destination, plan, trace_path=None):
    """Use a source allowlist; never include keys, local configs, caches, or results."""
    files = set()
    for directory in ["sebs", "benchmarks", "dockerfiles", "tools", "evaluation"]:
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                if path.suffix not in {".pyc", ".pyo", ".log", ".zip", ".gz"}:
                    files.add(path)
    for name in ["pyproject.toml", "README.md", "LICENSE.md", "configs/systems.json"]:
        files.add(ROOT / name)
    for path in (ROOT / "configs").glob("*.example.json"):
        files.add(path)
    trace = json.loads((trace_path or ROOT / "results/sql-aws-workload-01/sql-workload/trace.json").read_text())
    with tarfile.open(destination, "w:gz") as archive:
        for path in sorted(files):
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
        for name, value in [("comparison/private-driver.json", plan), ("comparison/trace.json", trace)]:
            data = json.dumps(value, indent=2).encode()
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def bootstrap(bucket, key, digest):
    return f'''#!/bin/bash
set -euxo pipefail
export HOME=/root
apt-get update
apt-get install -y python3-venv python3-dev build-essential libcurl4-openssl-dev libssl-dev curl
mkdir -p /opt/sebs-driver
python3 -m venv /opt/sebs-driver/venv
/opt/sebs-driver/venv/bin/pip install boto3
/opt/sebs-driver/venv/bin/python - <<'REMOTE'
import hashlib
from pathlib import Path
import tarfile
import boto3
archive = Path("/opt/sebs-driver/code.tar.gz")
boto3.client("s3", region_name="us-east-1").download_file({bucket!r}, {key!r}, str(archive))
assert hashlib.sha256(archive.read_bytes()).hexdigest() == {digest!r}, "Driver bundle checksum mismatch"
repo = Path("/opt/sebs-driver/repo")
repo.mkdir(exist_ok=True)
with tarfile.open(archive, "r:gz") as bundle:
    bundle.extractall(repo, filter="data")
REMOTE
/opt/sebs-driver/venv/bin/pip install -e /opt/sebs-driver/repo
cd /opt/sebs-driver/repo
/opt/sebs-driver/venv/bin/python -c "from evaluation.sql.cloud_replay import main; print('Cloud replay imports passed')"
touch /opt/sebs-driver/bootstrap.complete
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("results/sql-ec2"))
    parser.add_argument("--trace", type=Path, default=ROOT / "results/sql-aws-workload-01/sql-workload/trace.json")
    args = parser.parse_args()
    state_path = args.state_dir / "driver-instance.json"
    if state_path.exists():
        raise RuntimeError("Driver state exists. Inspect it rather than launching a duplicate.")
    plan = json.loads((args.state_dir / "private-driver.json").read_text())
    server = json.loads((args.state_dir / "instance.json").read_text())
    ec2 = boto3.client("ec2", region_name=plan["region"])
    token_path = args.state_dir / "driver-launch-token.txt"
    if not token_path.exists():
        token_path.write_text(str(uuid.uuid4()))
    # Persist the exact request so retries do not rebuild a different archive or
    # accidentally change parameters under the same idempotency token.
    request_path = args.state_dir / "driver-launch-request.json"
    if request_path.exists():
        request = json.loads(request_path.read_text())
    else:
        archive = args.state_dir / "driver-code.tar.gz"
        digest = make_bundle(archive, plan, args.trace)
        key = f"evaluation/sql/driver-code/{digest}/code.tar.gz"
        boto3.client("s3", region_name=plan["region"]).upload_file(str(archive), plan["bucket"], key)
        image = ec2.describe_images(ImageIds=[server["ami"]])["Images"][0]
        request = dict(ImageId=server["ami"], InstanceType="m7i-flex.large", MinCount=1, MaxCount=1,
            ClientToken=token_path.read_text().strip(),
            IamInstanceProfile={"Name": plan["driver_instance_profile"]},
            NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": plan["subnet_id"],
                                "Groups": [plan["driver_security_group_id"]], "AssociatePublicIpAddress": True}],
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            BlockDeviceMappings=[{"DeviceName": image["RootDeviceName"],
                "Ebs": {"VolumeSize": 30, "VolumeType": "gp3", "DeleteOnTermination": True}}],
            UserData=bootstrap(plan["bucket"], key, digest),
            TagSpecifications=[{"ResourceType": "instance", "Tags": [
                {"Key": "Name", "Value": "sebs-sql-workload-driver"}]}])
        request_path.write_text(json.dumps(request, indent=2) + "\n")
        (args.state_dir / "driver-bundle.json").write_text(json.dumps({
            "bucket": plan["bucket"], "key": key, "sha256": digest}, indent=2) + "\n")
    print("Launching one m7i-flex.large cloud driver; this consumes account usage/credits.", flush=True)
    instance = ec2.run_instances(**request)["Instances"][0]
    state = {"instance_id": instance["InstanceId"], "region": plan["region"],
             "instance_type": instance["InstanceType"], "ami": instance["ImageId"],
             "launch_time": instance["LaunchTime"].isoformat(),
             "server_instance_id": plan["server_instance_id"]}
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps(state, indent=2))
    print("Wait for SSM Online and bootstrap.complete before running queries.")


if __name__ == "__main__":
    main()
