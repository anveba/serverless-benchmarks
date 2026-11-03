"""Submit, collect, and summarize SQL comparison runs; retire recorded compute."""
import argparse
import json
import math
from pathlib import Path
from statistics import mean, median

import boto3
from botocore.exceptions import ClientError

INTERVALS = {"moderate": 2, "burst": 0, "sparse": 30}
QUERIES = ["q6", "q1", "q12", "q3", "q21"]


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def remote_script(workload, repetitions, count, prefix, label):
    """Self-contained so the first deployed driver also supports this command."""
    settings = dict(workload=workload, repetitions=repetitions, count=count,
                    interval=INTERVALS[workload], prefix=prefix, label=label)
    source = '''import copy, json, subprocess
from pathlib import Path
import boto3
settings = SETTINGS
plan = json.loads(Path("comparison/private-driver.json").read_text())
source = json.loads(Path("comparison/trace.json").read_text())
trace = copy.deepcopy(source)
requests = [{"at": i * settings["interval"], "query": ["q6", "q1", "q12", "q3", "q21"][i % 5]}
            for i in range(settings["count"])]
trace["requests"] = requests
trace["settings"]["requests"] = requests
trace["settings"]["max-in-flight"] = 2
root = Path("results") / settings["label"]
trace_path = root / "schedules" / (settings["workload"] + ".json")
trace_path.parent.mkdir(parents=True, exist_ok=True)
trace_path.write_text(json.dumps(trace, indent=2))
runs = [(r, b) for r in range(1, settings["repetitions"] + 1)
        for b in (["ec2", "lambda"] if r % 2 else ["lambda", "ec2"])]
for r, b in runs:
    if (root / settings["workload"] / f"repeat-{r}" / b).exists():
        raise SystemExit("Output exists; refusing duplicate replay")
s3 = boto3.client("s3", region_name=plan["region"])
for r, b in runs:
    output = root / settings["workload"] / f"repeat-{r}" / b
    print(f"BEGIN repetition={r} backend={b}", flush=True)
    result = subprocess.run(["/opt/sebs-driver/venv/bin/python", "-m", "evaluation.sql.cloud_replay",
        "--backend", b, "--trace", str(trace_path), "--output-dir", str(output)])
    if output.exists():
        for path in sorted(output.rglob("*.json")):
            s3.upload_file(str(path), plan["bucket"], settings["prefix"] + path.relative_to(root).as_posix())
    if result.returncode:
        raise SystemExit("Replay failed; available results uploaded; inspect before rerunning")
    print(f"SAVED repetition={r} backend={b}", flush=True)
print("WORKLOAD COMPLETE", flush=True)
'''.replace("SETTINGS", repr(settings), 1)
    return ("set -eu\nexport HOME=/root\ncd /opt/sebs-driver/repo\n"
            "/opt/sebs-driver/venv/bin/python - <<'REMOTE'\n" + source + "\nREMOTE\n")


def summarize(root, ec2_rate, lambda_rate, request_rate):
    report = {"pricing_assumptions": {"ec2_usd_per_hour": ec2_rate,
        "lambda_usd_per_gb_second": lambda_rate, "lambda_usd_per_million_requests": request_rate},
        "cost_scope": "Measured workload windows only; excludes S3, EBS, IPv4, driver, logs, setup and inter-run uptime; no credits or discounts",
        "workloads": {}}
    for workload in INTERVALS:
        if not (root / workload).exists():
            continue
        report["workloads"][workload] = {}
        counts = []
        expected_trace = None
        for backend in ("ec2", "lambda"):
            rows, durations, gb = [], [], 0.0
            directories = sorted((root / workload).glob("repeat-*/" + backend))
            if not directories:
                raise ValueError(f"Missing {workload}/{backend}")
            for directory in directories:
                summary = read(directory / "summary.json")
                requests = read(directory / "requests.json")
                responses = read(directory / "responses.json")
                trace = read(directory / "trace.json")["trace"]
                identity = json.dumps({"requests": trace["requests"], "input": trace.get("input"),
                    "max_in_flight": trace.get("settings", {}).get("max-in-flight")}, sort_keys=True)
                if expected_trace is None:
                    expected_trace = identity
                elif identity != expected_trace:
                    raise ValueError(f"Mismatched input/schedule/concurrency: {directory}")
                n = len(trace["requests"])
                if not (summary["submitted"] == summary["valid"] == len(requests) == len(responses) == n
                        and summary["execution_failures"] == 0
                        and sorted(r["index"] for r in requests) == list(range(n))
                        and all(r.get("valid") and not r["failure"] for r in requests)):
                    raise ValueError(f"Incomplete/invalid run: {directory}")
                rows.extend(requests)
                durations.append(summary["observation_seconds"])
                if backend == "lambda":
                    gb += sum(r["provider_metrics"]["gb_seconds"] for r in responses.values())
            values = sorted(r["total_latency_s"] for r in rows)
            cost = (sum(durations) * ec2_rate / 3600 if backend == "ec2"
                    else gb * lambda_rate + len(rows) * request_rate / 1e6)
            metrics = dict(runs=len(directories), requests=len(rows), mean_latency_s=mean(values),
                median_latency_s=median(values), p95_latency_s=values[math.ceil(.95 * len(values))-1],
                mean_queue_s=mean(r["queue_delay_s"] for r in rows),
                mean_service_latency_s=mean(r["service_latency_s"] for r in rows),
                mean_run_duration_s=mean(durations), observation_seconds=sum(durations),
                cold_starts=sum(r["cold_start"] is True for r in rows) if backend == "lambda" else None,
                billed_gb_seconds=gb if backend == "lambda" else None,
                estimated_window_cost_usd=cost, estimated_cost_per_100_requests_usd=cost / len(rows) * 100)
            report["workloads"][workload][backend] = metrics
            counts.append((len(directories), len(rows)))
            print(f"{workload:8} {backend:6} n={len(rows)} mean={mean(values):.3f}s "
                  f"p95={metrics['p95_latency_s']:.3f}s cost/100=${metrics['estimated_cost_per_100_requests_usd']:.5f}")
        if counts[0] != counts[1]:
            raise ValueError(f"Unequal backend sample counts for {workload}")
    if not report["workloads"]:
        raise ValueError("No downloaded workloads found")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("results/sql-ec2"))
    parser.add_argument("--label", default="workloads-100")
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("workload", choices=INTERVALS)
    submit.add_argument("--repetitions", type=int, required=True)
    submit.add_argument("--count", type=int, default=100)
    sub.add_parser("status")
    sub.add_parser("download")
    report = sub.add_parser("report")
    report.add_argument("--ec2-hourly", type=float, required=True)
    report.add_argument("--lambda-gb-second", type=float, required=True)
    report.add_argument("--lambda-million-requests", type=float, required=True)
    cleanup = sub.add_parser("cleanup", help="Preview or terminate only the two recorded instances")
    cleanup.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.label or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in args.label):
        parser.error("label must contain only letters, digits, hyphens and underscores")
    folder = args.state_dir
    if args.command == "report":
        result = summarize(folder / args.label, args.ec2_hourly, args.lambda_gb_second,
                           args.lambda_million_requests)
        write(folder / args.label / "comparison-cost-summary.json", result)
        return
    driver, plan = read(folder / "driver-instance.json"), read(folder / "private-driver.json")
    region = driver["region"]
    ssm = boto3.client("ssm", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    prefix = f"evaluation/sql/driver-results/{driver['instance_id']}/{args.label}/"
    if args.command == "submit":
        if args.repetitions < 1 or args.count < 1:
            parser.error("count and repetitions must be positive")
        record = folder / f"{args.label}-{args.workload}-command.json"
        if record.exists():
            raise ValueError("Command record exists; inspect status rather than resubmitting")
        # Also guard runs submitted by the original hand-written commands.
        for path in folder.glob("*command.json"):
            old = read(path)
            if not old.get("command_id") or old.get("instance_id") != driver["instance_id"]:
                continue
            status = ssm.get_command_invocation(CommandId=old["command_id"],
                InstanceId=driver["instance_id"])["Status"]
            if status not in {"Success", "Failed", "Cancelled", "TimedOut"}:
                raise ValueError(f"Another recorded command is {status}: {path}")
        timeout = max(3600, math.ceil((args.count-1)*INTERVALS[args.workload]*2*args.repetitions + 3600))
        if timeout > 172800:
            raise ValueError("Run exceeds SSM execution timeout; reduce count or repetitions")
        script = remote_script(args.workload, args.repetitions, args.count, prefix, args.label)
        command_id = ssm.send_command(InstanceIds=[driver["instance_id"]],
            DocumentName="AWS-RunShellScript", TimeoutSeconds=600,
            Parameters={"commands": [script], "executionTimeout": [str(timeout)]})["Command"]["CommandId"]
        write(record, dict(command_id=command_id, instance_id=driver["instance_id"], region=region))
        print("Submitted once:", command_id, "Record:", record)
    elif args.command == "status":
        for path in sorted(folder.glob("*command.json")):
            state = read(path)
            if state.get("instance_id") != driver["instance_id"] or not state.get("command_id"):
                continue
            result = ssm.get_command_invocation(CommandId=state["command_id"], InstanceId=driver["instance_id"])
            print(path.name, result["Status"])
            if result["Status"] in {"Failed", "TimedOut", "Cancelled"}:
                print(result.get("StandardErrorContent", ""))
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=plan["bucket"], Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("/summary.json"):
                    value = json.loads(s3.get_object(Bucket=plan["bucket"], Key=obj["Key"])["Body"].read())
                    print(obj["Key"][len(prefix):], f"valid={value['valid']}/{value['submitted']}")
    elif args.command == "download":
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=plan["bucket"], Prefix=prefix):
            for obj in page.get("Contents", []):
                relative = Path(obj["Key"][len(prefix):])
                if relative.is_absolute() or ".." in relative.parts or not relative.name:
                    raise ValueError("Unexpected S3 path")
                path = folder / args.label / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(plan["bucket"], obj["Key"], str(path))
                print("Downloaded:", path)
    elif args.command == "cleanup":
        server = read(folder / "instance.json")
        if server["instance_id"] != driver["server_instance_id"] or server["instance_id"] != plan["server_instance_id"]:
            raise ValueError("Server/driver state mismatch")
        # Validate all published workloads and billing locally before destroying compute.
        summarize(folder / args.label, 0, 0, 0)
        for workload, count in (("moderate", 3), ("burst", 3), ("sparse", 2)):
            for backend in ("ec2", "lambda"):
                if len(list((folder / args.label / workload).glob(f"repeat-*/{backend}/summary.json"))) != count:
                    raise ValueError("Cleanup requires all recorded experiment results downloaded")
        ec2 = boto3.client("ec2", region_name=region)
        ids = [server["instance_id"], driver["instance_id"]]
        instances = [i for r in ec2.describe_instances(InstanceIds=ids)["Reservations"] for i in r["Instances"]]
        print("Account:", boto3.client("sts").get_caller_identity()["Account"])
        for instance in instances:
            print("Terminate:", instance["InstanceId"], instance["State"]["Name"],
                  "volumes:", [m.get("Ebs") for m in instance.get("BlockDeviceMappings", [])])
            if any(not m["Ebs"].get("DeleteOnTermination") for m in instance.get("BlockDeviceMappings", []) if "Ebs" in m):
                raise ValueError("A volume would survive termination; inspect it first")
        print("Disable Lambda invocations and delete Function URL:", plan["lambda_function"])
        print("Retain S3 inputs/results, CloudWatch logs, IAM, security groups and SSM parameter.")
        print("S3/log storage remains billable until separately deleted; see README cleanup.")
        if not args.apply:
            print("Preview only. Add --apply after inspecting the identifiers.")
            return
        lam = boto3.client("lambda", region_name=region)
        try:
            lam.put_function_concurrency(FunctionName=plan["lambda_function"], ReservedConcurrentExecutions=0)
            try:
                lam.delete_function_url_config(FunctionName=plan["lambda_function"])
            except ClientError as error:
                if error.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        ec2.terminate_instances(InstanceIds=ids)
        write(folder / "cleanup-request.json", dict(instance_ids=ids, region=region,
            lambda_function=plan["lambda_function"], action="terminate requested; verify completion in EC2"))
        print("Termination requested. Verify both instances reach terminated; preserve local state/results.")


if __name__ == "__main__":
    main()
