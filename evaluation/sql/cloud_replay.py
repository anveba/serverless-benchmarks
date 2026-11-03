"""Replay the same trace from the cloud driver against EC2 or Lambda."""
import argparse
import json
from pathlib import Path

import boto3

from evaluation.sql.lambda_client import LambdaClient
from evaluation.sql.replay_service import ServiceClient, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=["ec2", "lambda"])
    parser.add_argument("--plan", type=Path, default=Path("comparison/private-driver.json"))
    parser.add_argument("--trace", type=Path, default=Path("comparison/trace.json"))
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    trace = json.loads(args.trace.read_text())
    if args.backend == "ec2":
        token = boto3.client("ssm", region_name=plan["region"]).get_parameter(
            Name=plan["token_parameter"], WithDecryption=True)["Parameter"]["Value"]
        client = ServiceClient(plan["server_endpoint"], token)
    else:
        client = LambdaClient(plan["lambda_function"], trace["input"], plan["region"])
    raise SystemExit(0 if run(trace, client, args.output_dir) else 1)


if __name__ == "__main__":
    main()
