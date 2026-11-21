"""Read historical EC2/EBS metrics for existing PostgreSQL benchmark runs."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import boto3

from sebs.storage.aws_postgresql import AWSPostgreSQL
from sebs.storage.sql_config import sql_config


def observation_window(run_dir, queries=("q6", "q1", "q12", "q3", "q21")):
    records = []
    for query in queries:
        path = run_dir / query / "experiments.json"
        if not path.exists():
            raise ValueError("Missing saved query run: " + str(path))
        data = json.loads(path.read_text())
        records.append({"query": query, "begin": data["begin_time"],
                        "end": data["end_time"],
                        "failures": sum(r["stats"]["failure"]
                            for group in data["_invocations"].values()
                            for r in group.values())})
    return records


def metric_queries(instance, volumes):
    detailed = instance.get("Monitoring", {}).get("State") == "enabled"
    queries = [{"Id": "cpu", "MetricStat": {"Metric": {
        "Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
        "Dimensions": [{"Name": "InstanceId", "Value": instance["InstanceId"]}]},
        "Period": 60 if detailed else 300, "Stat": "Average"}}]
    for key, name, stat, period in (
        ("inst_ebs_iops_exceeded", "InstanceEBSIOPSExceededCheck", "Maximum", 60),
        ("inst_ebs_bandwidth_exceeded", "InstanceEBSThroughputExceededCheck", "Maximum", 60),
        ("inst_ebs_io_balance", "EBSIOBalance%", "Minimum", 300),
        ("inst_ebs_byte_balance", "EBSByteBalance%", "Minimum", 300),
    ):
        queries.append({"Id": key, "MetricStat": {"Metric": {
            "Namespace": "AWS/EC2", "MetricName": name,
            "Dimensions": [{"Name": "InstanceId", "Value": instance["InstanceId"]}]},
            "Period": period, "Stat": stat}})
    for index, volume in enumerate(volumes):
        for key, name, stat in (
            ("read_bytes", "VolumeReadBytes", "Sum"),
            ("write_bytes", "VolumeWriteBytes", "Sum"),
            ("read_ops", "VolumeReadOps", "Sum"),
            ("write_ops", "VolumeWriteOps", "Sum"),
            ("read_seconds", "VolumeTotalReadTime", "Sum"),
            ("write_seconds", "VolumeTotalWriteTime", "Sum"),
            ("queue", "VolumeQueueLength", "Average"),
            ("iops_exceeded", "VolumeIOPSExceededCheck", "Maximum"),
            ("throughput_exceeded", "VolumeThroughputExceededCheck", "Maximum"),
        ):
            queries.append({"Id": f"v{index}_{key}", "MetricStat": {
                "Metric": {"Namespace": "AWS/EBS", "MetricName": name,
                    "Dimensions": [{"Name": "VolumeId", "Value": volume["VolumeId"]}]},
                "Period": 60, "Stat": stat}})
    return queries



def collect_failure_logs(session, run_dir, observations, region):
    logs = session.client("logs", region_name=region)
    collected = {}
    for observation in observations:
        if not observation["failures"]:
            continue
        query = observation["query"]
        data = json.loads((run_dir / query / "experiments.json").read_text())
        events = []
        for function, invocations in data["_invocations"].items():
            if not any(r["stats"]["failure"] for r in invocations.values()):
                continue
            for page in logs.get_paginator("filter_log_events").paginate(
                logGroupName=f"/aws/lambda/{function}",
                startTime=max(0, int((observation["begin"] - 10) * 1000)),
                endTime=int((observation["end"] + 10) * 1000),
            ):
                events.extend(page["events"])
        collected[query] = events
    return collected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-configuration", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--queries", nargs="+", choices=["q6", "q1", "q12", "q3", "q21"],
                        default=["q6", "q1", "q12", "q3", "q21"])
    parser.add_argument("--failure-logs", action="store_true", help="Also save existing failed-invocation logs")
    args = parser.parse_args()
    observations = observation_window(args.run_dir, args.queries)
    config = sql_config(json.loads(args.storage_configuration.read_text())["sql"])
    if config.provider != "aws":
        parser.error("Historical cloud metrics require an existing AWS SQL storage attachment")
    session = boto3.Session()
    _, _, outputs = AWSPostgreSQL(config, session).info()
    ec2 = session.client("ec2", region_name=config.region)
    instance = ec2.describe_instances(InstanceIds=[outputs["InstanceId"]])["Reservations"][0]["Instances"][0]
    volume_ids = [v["Ebs"]["VolumeId"] for v in instance["BlockDeviceMappings"] if "Ebs" in v]
    volumes = ec2.describe_volumes(VolumeIds=volume_ids)["Volumes"]
    queries = metric_queries(instance, volumes)
    # Align complete periods; these include padding and are not per-query attribution.
    begin = math.floor(min(r["begin"] for r in observations) / 300) * 300
    end = math.ceil(max(r["end"] for r in observations) / 300) * 300
    cloudwatch = session.client("cloudwatch", region_name=config.region)
    params = {"StartTime": datetime.fromtimestamp(begin, timezone.utc),
              "EndTime": datetime.fromtimestamp(end, timezone.utc),
              "MetricDataQueries": queries, "ScanBy": "TimestampAscending"}
    collected = {}
    messages = []
    while True:
        response = cloudwatch.get_metric_data(**params)
        messages.extend(response.get("Messages", []))
        for entry in response["MetricDataResults"]:
            target = collected.setdefault(entry["Id"], {"timestamps": [], "values": [], "statuses": [], "messages": []})
            target["timestamps"].extend(t.isoformat() for t in entry.get("Timestamps", []))
            target["values"].extend(entry.get("Values", []))
            target["statuses"].append(entry["StatusCode"])
            target["messages"].extend(entry.get("Messages", []))
        if not response.get("NextToken"):
            break
        params["NextToken"] = response["NextToken"]
    report = {"region": config.region, "observations": observations,
              "window_begin_utc": params["StartTime"].isoformat(),
              "window_end_utc": params["EndTime"].isoformat(),
              "instance": {k: instance.get(k) for k in ("InstanceId", "InstanceType", "Monitoring")},
              "volumes": [{k: v.get(k) for k in ("VolumeId", "VolumeType", "Size", "Iops", "Throughput")}
                          for v in volumes],
              "queries": queries, "metrics": collected, "messages": messages,
              "note": "Padded workload window. CPU periods may be 5 min. No per-query attribution; missing points are not zero."}
    target = args.run_dir / "diagnostics" / "historical-metrics.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    print("Instance:", json.dumps(report["instance"]))
    print("Volumes:", json.dumps(report["volumes"]))
    print("Window UTC:", report["window_begin_utc"], "to", report["window_end_utc"])
    for query in queries:
        metric = collected.get(query["Id"], {})
        print(query["Id"], "period_s=", query["MetricStat"]["Period"],
              "statuses=", metric.get("statuses", []))
        for timestamp, value in zip(metric.get("timestamps", []), metric.get("values", [])):
            print(" ", timestamp, value)
        if not metric.get("values"):
            print("  NO DATA (not zero)")
    print(report["note"])
    print("Saved:", target)
    if args.failure_logs:
        failures = collect_failure_logs(session, args.run_dir, observations, config.region)
        path = target.parent / "failure-logs.json"
        path.write_text(json.dumps(failures, indent=2) + "\n")
        for name, events in failures.items():
            print("FAILURE LOGS", name)
            for event in events:
                if "[ERROR]" in event["message"] or "REPORT RequestId" in event["message"]:
                    print(event["message"])
        print("Saved failure logs:", path)
    print("No SQL queries submitted or cloud resources changed.")


if __name__ == "__main__":
    main()
