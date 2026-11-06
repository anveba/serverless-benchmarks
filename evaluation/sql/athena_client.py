"""Athena backend for the replay runner."""
import datetime
import importlib.util
import time
import uuid

import boto3
from botocore.config import Config

from evaluation.sql.service import ROOT, manifest_fingerprint
from sebs.faas.function import ExecutionResult

QUERY_FILE = ROOT / "benchmarks/700.analytics/710.sql-query/python/queries.py"
spec = importlib.util.spec_from_file_location("sebs_athena_queries", QUERY_FILE)
queries = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queries)

# billed per MB scanned (rounded up), 10 MB minimum per query
MEGABYTE = 2**20
MINIMUM_BILLED_BYTES = 10 * MEGABYTE
FINAL_STATES = ("SUCCEEDED", "FAILED", "CANCELLED")
INTEGER_TYPES = ("tinyint", "smallint", "integer", "int", "bigint")
NUMBER_TYPES = ("double", "real", "float")


def billed_bytes(scanned):
    return max(MINIMUM_BILLED_BYTES, -(-scanned // MEGABYTE) * MEGABYTE)


def convert(value, column_type):
    # Athena returns strings, convert to what the function returns
    if value is None:
        return None
    column_type = column_type.lower()
    if column_type in INTEGER_TYPES:
        return int(value)
    if column_type.startswith("decimal") or column_type in NUMBER_TYPES:
        return float(value)
    return value


class AthenaClient:
    backend = "athena"

    def __init__(self, state, poll_interval=0.1, timeout=900):
        self.state = state
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.endpoint = f"aws-athena://{state['region']}/{state['workgroup']}"
        # retries are safe, starting a query uses a request token
        self.client = boto3.client("athena", region_name=state["region"], config=Config(
            connect_timeout=10, read_timeout=60, retries={"mode": "standard", "max_attempts": 5},
        ))

    def health(self):
        workgroup = self.client.get_work_group(WorkGroup=self.state["workgroup"])["WorkGroup"]
        configuration = workgroup["Configuration"]
        return {
            "workgroup": workgroup["Name"],
            "state": workgroup["State"],
            "engine_version": configuration["EngineVersion"]["EffectiveEngineVersion"],
            "enforce_configuration": configuration["EnforceWorkGroupConfiguration"],
            "scanned_bytes_limit": configuration.get("BytesScannedCutoffPerQuery"),
            "database": self.state["database"],
            "input_fingerprint": self.state["input_fingerprint"],
            "result_reuse": False,
        }

    def verify(self, health, trace):
        if health["input_fingerprint"] != manifest_fingerprint(trace["input"]):
            raise ValueError("Athena tables were created for different input files; rerun athena_setup")
        if health["state"] != "ENABLED" or not health["enforce_configuration"]:
            raise ValueError("Athena workgroup is disabled or does not enforce its configuration")

    def fetch_rows(self, query_id):
        columns, rows = None, []
        pages = self.client.get_paginator("get_query_results").paginate(QueryExecutionId=query_id)
        for page in pages:
            result = page["ResultSet"]
            if columns is None:
                columns = [column["Type"] for column in result["ResultSetMetadata"]["ColumnInfo"]]
                # first row is the header
                result_rows = result["Rows"][1:]
            else:
                result_rows = result["Rows"]
            for row in result_rows:
                rows.append([convert(cell.get("VarCharValue"), column_type)
                             for cell, column_type in zip(row["Data"], columns)])
        return rows

    def invoke(self, query):
        begin = datetime.datetime.now()
        start = time.perf_counter()
        query_id = self.client.start_query_execution(
            QueryString=queries.QUERIES[query],
            QueryExecutionContext={"Database": self.state["database"]},
            WorkGroup=self.state["workgroup"],
            ClientRequestToken=str(uuid.uuid4()),
            ResultReuseConfiguration={"ResultReuseByAgeConfiguration": {"Enabled": False}},
        )["QueryExecutionId"]

        polls = 0
        while True:
            execution = self.client.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
            polls += 1
            if execution["Status"]["State"] in FINAL_STATES:
                break
            if time.perf_counter() - start > self.timeout:
                self.client.stop_query_execution(QueryExecutionId=query_id)
                break
            time.sleep(self.poll_interval)
        completed = time.perf_counter()

        state = execution["Status"]["State"]
        rows = self.fetch_rows(query_id) if state == "SUCCEEDED" else None
        result = ExecutionResult.from_times(begin, datetime.datetime.now())
        result.request_id = query_id
        result.stats.cold_start = None  # Lambda cold-start semantics do not apply.

        statistics = execution.get("Statistics", {})
        scanned = statistics.get("DataScannedInBytes", 0)
        metrics = {
            "engine_ms": statistics.get("EngineExecutionTimeInMillis"),
            "queue_ms": statistics.get("QueryQueueTimeInMillis"),
            "planning_ms": statistics.get("QueryPlanningTimeInMillis"),
            "service_processing_ms": statistics.get("ServiceProcessingTimeInMillis"),
            "total_execution_ms": statistics.get("TotalExecutionTimeInMillis"),
            "data_scanned_bytes": scanned,
            "billed_bytes": billed_bytes(scanned),
            "reused_previous_result": statistics.get("ResultReuseInformation", {}).get(
                "ReusedPreviousResult", False),
            "polls": polls,
            "result_fetch_s": time.perf_counter() - completed,
            "engine_version": execution.get("EngineVersion", {}).get("EffectiveEngineVersion"),
        }
        if state != "SUCCEEDED":
            result.stats.failure = True
            result.output = {"error": f"Athena query {state.lower()}", "query": query,
                             "request_id": query_id, "provider_metrics": metrics,
                             "reason": execution["Status"].get("StateChangeReason", "")[:500]}
            return result
        if metrics["reused_previous_result"]:
            raise ValueError("Athena reused an earlier result although reuse is disabled")
        result.output = {"result": {"queries": {query: rows}}, "query": query,
                         "request_id": query_id, "provider_metrics": metrics}
        return result
