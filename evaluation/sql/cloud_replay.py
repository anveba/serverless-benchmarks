"""Replay the same trace from the cloud driver against EC2, Lambda or Athena."""
import argparse
import json
from pathlib import Path

import boto3

from evaluation.sql.athena_client import AthenaClient
from evaluation.sql.lambda_client import LambdaClient
from evaluation.sql.replay_service import ServiceClient, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=["ec2", "lambda", "athena"])
    parser.add_argument("--plan", type=Path, default=Path("comparison/private-driver.json"))
    parser.add_argument("--trace", type=Path, default=Path("comparison/trace.json"))
    parser.add_argument("--athena-state", type=Path, default=Path("comparison/athena.json"))
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    trace = json.loads(args.trace.read_text())
    if args.backend == "athena":
        client = AthenaClient(json.loads(args.athena_state.read_text()))
        raise SystemExit(0 if run(trace, client, args.output_dir) else 1)
    plan = json.loads(args.plan.read_text())
    if args.backend == "ec2":
        token = boto3.client("ssm", region_name=plan["region"]).get_parameter(
            Name=plan["token_parameter"], WithDecryption=True)["Parameter"]["Value"]
        client = ServiceClient(plan["server_endpoint"], token)
    else:
        client = LambdaClient(plan["lambda_function"], trace["input"], plan["region"])
    raise SystemExit(0 if run(trace, client, args.output_dir) else 1)


if __name__ == "__main__":
    main()
