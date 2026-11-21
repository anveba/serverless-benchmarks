"""Record and change only the owned PostgreSQL gp3 volume's throughput."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from sebs.storage.aws_postgresql import AWSPostgreSQL
from sebs.storage.sql_config import sql_config


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def snapshot(volume):
    return {key: volume[key] for key in ("VolumeId", "VolumeType", "Size", "Iops", "Throughput")}


def modification(ec2, volume_id):
    try:
        entries = ec2.describe_volumes_modifications(VolumeIds=[volume_id])["VolumesModifications"]
    except ClientError as error:
        # A never-modified volume is reported as an API error, not always an empty list.
        if error.response.get("Error", {}).get("Code") == "InvalidVolumeModification.NotFound":
            return None
        raise
    if len(entries) > 1:
        entries.sort(key=lambda item: item["StartTime"], reverse=True)
    return entries[0] if entries else None


def submit(ec2, volume, folder, target, action="apply", expected=125):
    marker = folder / (action + "-request.json")
    if marker.exists():
        request = json.loads(marker.read_text())
        if request["before"]["VolumeId"] != volume["VolumeId"] or request["target_throughput"] != target:
            raise ValueError("Saved change targets another volume/configuration")
        print("Request already recorded; inspecting status without resubmitting.")
        return request
    if volume["VolumeType"] != "gp3" or volume["Throughput"] != expected:
        raise ValueError("Current gp3 throughput differs from expected value; no change submitted")
    if type(target) is not int or not 125 <= target <= min(2000, volume["Iops"] // 4):
        raise ValueError("Requested throughput is outside the volume's gp3 IOPS/throughput limits")
    if target == expected:
        raise ValueError("Requested throughput already matches the expected setting")
    previous = modification(ec2, volume["VolumeId"])
    if previous and previous["ModificationState"] in ("modifying", "optimizing"):
        raise ValueError("Previous EBS modification has not completed; no new change submitted")
    request = {"requested_at": datetime.now(timezone.utc).isoformat(),
               "before": snapshot(volume), "target_throughput": target,
               "request_recorded_before_submission": True}
    # ModifyVolume has no idempotency token. Never automatically resubmit an ambiguous call.
    save(marker, request)
    response = ec2.modify_volume(VolumeId=volume["VolumeId"], Throughput=target)
    save(folder / (action + "-response.json"), response["VolumeModification"])
    return request


def status(ec2, volume_id, target):
    volume = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    change = modification(ec2, volume_id)
    ready = bool(change and change["ModificationState"] == "completed"
                 and change.get("TargetThroughput") == target
                 and volume["Throughput"] == target)
    return {"checked_at": datetime.now(timezone.utc).isoformat(),
            "volume": snapshot(volume), "modification": change,
            "target_throughput": target, "ready_for_measurement": ready}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["apply", "status", "restore"])
    parser.add_argument("--storage-configuration", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("results/postgresql-aws-sf10-disk500"))
    parser.add_argument("--throughput", type=int, default=500)
    args = parser.parse_args()
    config = sql_config(json.loads(args.storage_configuration.read_text())["sql"])
    if config.provider != "aws":
        parser.error("An existing AWS SQL storage attachment is required")
    session = boto3.Session()
    _, stack, outputs = AWSPostgreSQL(config, session).info()
    ec2 = session.client("ec2", region_name=config.region,
                         config=Config(retries={"total_max_attempts": 1}))
    instance = ec2.describe_instances(InstanceIds=[outputs["InstanceId"]])["Reservations"][0]["Instances"][0]
    roots = [item["Ebs"]["VolumeId"] for item in instance["BlockDeviceMappings"]
             if item["DeviceName"] == instance["RootDeviceName"] and "Ebs" in item]
    if len(roots) != 1:
        raise ValueError("Expected exactly one root volume on the owned PostgreSQL instance")
    volume_id = roots[0]
    volume = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    if not any(a["InstanceId"] == outputs["InstanceId"] and a["State"] == "attached"
               for a in volume["Attachments"]):
        raise ValueError("Volume is not attached to the owned PostgreSQL instance")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    owner_path = args.state_dir / "owner.json"
    owner = {"stack_id": stack["StackId"], "instance_id": outputs["InstanceId"],
             "volume_id": volume_id, "region": config.region}
    if owner_path.exists() and json.loads(owner_path.read_text()) != owner:
        raise ValueError("State directory belongs to another SQL storage deployment")
    if args.action == "apply":
        if not owner_path.exists():
            save(owner_path, owner)
        info_path = args.state_dir / "instance-ebs-limits.json"
        if not info_path.exists():
            info = ec2.describe_instance_types(InstanceTypes=[instance["InstanceType"]])["InstanceTypes"][0]
            save(info_path, {"instance_type": instance["InstanceType"], "ebs": info.get("EbsInfo")})
        print("Requesting throughput treatment; additional provisioned EBS throughput is billable.", flush=True)
        request = submit(ec2, volume, args.state_dir, args.throughput)
        target = request["target_throughput"]
    else:
        initial = json.loads((args.state_dir / "apply-request.json").read_text())
        if initial["before"]["VolumeId"] != volume_id:
            raise ValueError("Saved experiment volume differs from owned database volume")
        if args.action == "restore":
            target = initial["before"]["Throughput"]
            for key in ("VolumeType", "Size", "Iops"):
                if volume[key] != initial["before"][key]:
                    raise ValueError("Other volume allocation changed; inspect before restoring")
            print("Requesting restoration of original EBS throughput.", flush=True)
            submit(ec2, volume, args.state_dir, target, action="restore",
                   expected=initial["target_throughput"])
        else:
            restored = args.state_dir / "restore-request.json"
            target = json.loads(restored.read_text())["target_throughput"] if restored.exists() else initial["target_throughput"]
    result = status(ec2, volume_id, target)
    save(args.state_dir / "status.json", result)
    print(json.dumps(result, indent=2, default=str))
    print("Wait for ready_for_measurement=true before running the new treatment.")
    print("No SQL queries submitted; instance, database memory, disk size and IOPS unchanged.")


if __name__ == "__main__":
    main()
