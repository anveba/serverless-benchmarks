"""Direct Lambda adapter for the same replay runner as the EC2 service."""
import base64
import copy
import datetime
import json
import time

import boto3
from botocore.config import Config

from sebs.aws.aws import AWS
from sebs.faas.function import ExecutionResult


class LambdaClient:
    backend = "lambda"

    def __init__(self, function_name, input_config, region="us-east-1"):
        self.function_name = function_name
        self.input = copy.deepcopy(input_config)
        self.endpoint = f"aws-lambda://{region}/{function_name}"
        # Construct once before worker threads start. Boto3 clients support
        # concurrent requests; a boto3 Session is not shared between workers.
        self.client = boto3.client("lambda", region_name=region, config=Config(
            connect_timeout=10, read_timeout=960, retries={"total_max_attempts": 1},
        ))

    def health(self):
        config = self.client.get_function_configuration(FunctionName=self.function_name)
        return {key: config.get(key) for key in [
            "FunctionName", "Runtime", "Architectures", "MemorySize", "Timeout",
            "CodeSha256", "LastModified", "State", "LastUpdateStatus",
        ]}

    def verify(self, health, trace):
        if health["Runtime"] != "python3.12" or health["Architectures"] != ["x86_64"]:
            raise ValueError("Expected the tested Python 3.12/x64 Lambda")
        if health["MemorySize"] != trace["settings"]["memory"]:
            raise ValueError("Lambda memory differs from the trace; configure it before replay")
        if health["State"] != "Active" or health["LastUpdateStatus"] != "Successful":
            raise ValueError("Lambda deployment is not ready")

    def invoke(self, query):
        payload = copy.deepcopy(self.input)
        payload["queries"] = [query]
        begin = datetime.datetime.now()
        started = time.perf_counter_ns()
        response = self.client.invoke(FunctionName=self.function_name,
            Payload=json.dumps(payload).encode(), LogType="Tail")
        # Read the complete response before closing the client latency interval.
        try:
            raw = response["Payload"].read()
        finally:
            response["Payload"].close()
        result = ExecutionResult.from_times(begin, datetime.datetime.now())
        result.times.client = (time.perf_counter_ns() - started) // 1000
        result.request_id = response["ResponseMetadata"]["RequestId"]
        # A failed execution is still billed. Preserve its native REPORT too.
        report = next((line.strip() for line in base64.b64decode(response.get("LogResult", "")).decode(errors="replace").splitlines()
                       if line.startswith("REPORT RequestId:")), None)
        report_error = None
        if report:
            try:
                AWS.parse_aws_report(report, result)
            except (IndexError, KeyError, ValueError, TypeError) as error:
                # Meter parsing must not discard a received query response.
                # Keep the report for offline repair and leave usage unknown.
                report_error = type(error).__name__
        measured = report is not None and report_error is None
        metrics = {
            "native_report_available": measured,
            "native_report_parse_error": report_error,
            "execution_us": result.provider_times.execution if measured else None,
            "initialization_us": result.provider_times.initialization if measured else None,
            "billed_ms": result.billing.billed_time if measured else None,
            "gb_seconds": result.billing.gb_seconds if measured else None,
            "memory_mb": result.billing.memory if measured else None,
            "max_memory_mb": result.stats.memory_used,
        }
        if response["StatusCode"] != 200 or "FunctionError" in response:
            result.stats.failure = True
            result.stats.cold_start = (result.provider_times.initialization > 0) if measured else None
            result.output = {"error": "Lambda execution failed", "query": query,
                             "request_id": result.request_id, "provider_metrics": metrics,
                             "native_report": report}
            return result
        envelope = json.loads(raw)
        body = envelope["body"]
        if isinstance(body, str):
            body = json.loads(body)
        result.parse_benchmark_output(body)
        # Normalize the shape to the persistent service response for the same
        # validator and output collector; preserve native provider metrics.
        result.output = body["result"]
        result.output["request_id"] = result.request_id
        result.output["query"] = query
        result.output["provider_metrics"] = metrics
        result.output["native_report"] = report
        return result
