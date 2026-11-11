"""Replay an existing Lambda arrival trace against the persistent SQL service."""
import argparse
import datetime
import json
import os
import platform
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from evaluation.sql.service import ROOT, manifest_fingerprint
from sebs.benchmark import load_benchmark_input
from sebs.experiments.sql_workload import replay, validate_trace
from sebs.faas.function import ExecutionResult


class ServiceClient:
    def __init__(self, endpoint, token="", timeout=360):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("endpoint must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Do not include credentials, query strings, or fragments in endpoint")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = "Bearer " + token

    def health(self):
        request = urllib.request.Request(self.endpoint + "/health", headers=self.headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def invoke(self, query):
        request = urllib.request.Request(
            self.endpoint + "/query", data=json.dumps({"query": query}).encode(),
            headers=self.headers,
        )
        begin = datetime.datetime.now()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            # A returned HTTP error is a known failure, unlike a lost response.
            result = ExecutionResult.from_times(begin, datetime.datetime.now())
            result.stats.failure = True
            result.stats.cold_start = None
            result.output = {"http_status": error.code}
            error.close()
            return result
        result = ExecutionResult.from_times(begin, datetime.datetime.now())
        if body.get("query") != query or not isinstance(body.get("request_id"), str):
            raise ValueError("Service response does not match the submitted query")
        result.request_id = body["request_id"]
        result.stats.cold_start = None  # Lambda cold-start semantics do not apply.
        result.output = body
        return result


def verify_snapshot(health, trace):
    if health.get("duckdb") != "2.0.0":
        raise ValueError("Service must use DuckDB 2.0.0")
    if health.get("input_fingerprint") != manifest_fingerprint(trace["input"]):
        raise ValueError("Service input manifest differs from the Lambda trace; restart with the same manifest")
    if health.get("connection_policy") != "fresh_per_request" or health.get("external_file_cache") is not False:
        raise ValueError("Service cache/connection policy differs from the initial comparison policy")


def run(trace, client, output_dir):
    requests = validate_trace(trace["requests"])
    limit = trace["settings"]["max-in-flight"]
    if type(limit) is not int or limit < 1:
        raise ValueError("Trace max-in-flight must be a positive integer")
    health = client.health()
    # Managed backends check their own configuration; the service checks its DuckDB snapshot.
    if hasattr(client, "verify"):
        client.verify(health, trace)
    else:
        verify_snapshot(health, trace)
    output_dir.mkdir(parents=True, exist_ok=False)
    metadata = {"backend": getattr(client, "backend", "persistent_service"), "endpoint": client.endpoint,
                "driver": {"hostname": socket.gethostname(), "platform": platform.platform(),
                           "python": platform.python_version()},
                "service": health, "trace": trace, "max_in_flight": limit,
                "validation_in_timing": False, "automatic_retries": False,
                "warmup": False, "cold_start_semantics": getattr(type(client), "cold_start_semantics", "not_applicable")}
    (output_dir / "trace.json").write_text(json.dumps(metadata, indent=2) + "\n")
    records, responses = [], {}

    def save(record, result):
        records.append(record)
        if result is not None:
            responses[str(record["index"])] = result.output
        (output_dir / "requests.json").write_text(json.dumps(records, indent=2) + "\n")
        (output_dir / "responses.json").write_text(json.dumps(responses, indent=2) + "\n")

    begin = time.time()
    outcomes = replay(requests, client.invoke, limit, save)
    end = time.time()
    validator = load_benchmark_input(str(ROOT / "benchmarks/700.analytics/710.sql-query"))
    for record, result in outcomes:
        record.update(valid=False, validation_error=None)
        if result is not None and not result.stats.failure:
            try:
                error = validator.validate_output(
                    None, {"scale_factor": trace["input"]["scale_factor"],
                           "queries": [record["query"]]},
                    {"result": result.output["result"]}, "python",
                )
                record.update(valid=error is None, validation_error=error)
            except Exception as error:
                record["validation_error"] = f"{type(error).__name__}: {error}"
        print(f"{record['index']:2} {record['query']:4} valid={record['valid']} "
              f"failed={record['failure']} queue={record['queue_delay_s']:.3f}s "
              f"total={record['total_latency_s']:.3f}s", flush=True)
    records = [record for record, _ in outcomes]
    (output_dir / "requests.json").write_text(json.dumps(records, indent=2) + "\n")
    summary = {"submitted": len(requests), "completed_records": len(records),
               "valid": sum(record["valid"] for record in records),
               "execution_failures": sum(record["failure"] for record in records),
               "observation_begin_unix": begin, "observation_end_unix": end,
               "observation_seconds": end-begin,
               "cost": None, "cost_note": "Full observation cost not computed; Lambda billing is in responses when returned"}
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return all(record["valid"] and not record["failure"] for record in records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8080")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    client = ServiceClient(args.endpoint, os.environ.get("SEBS_SQL_SERVICE_TOKEN", ""))
    success = run(json.loads(args.trace.read_text()), client, args.output_dir)
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
