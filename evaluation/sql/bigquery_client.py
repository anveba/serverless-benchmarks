"""BigQuery backend for the replay runner."""
import datetime
import importlib.util
import threading
import time
import uuid

import google.auth
from google.auth.transport.requests import AuthorizedSession

from evaluation.sql.service import ROOT, manifest_fingerprint
from sebs.faas.function import ExecutionResult

QUERY_FILE = ROOT / "benchmarks/700.analytics/710.sql-query/python/queries.py"
spec = importlib.util.spec_from_file_location("sebs_bigquery_queries", QUERY_FILE)
queries = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queries)

API = "https://bigquery.googleapis.com/bigquery/v2"
# safety limit, SF1 queries scan < 200 MB
MAXIMUM_BYTES_BILLED = 10 * 2**30
INTEGER_TYPES = ("INTEGER", "INT64")
NUMBER_TYPES = ("NUMERIC", "BIGNUMERIC", "FLOAT", "FLOAT64")


def convert(value, field_type):
    # BigQuery returns strings, convert to what the function returns
    if value is None:
        return None
    if field_type in INTEGER_TYPES:
        return int(value)
    if field_type in NUMBER_TYPES:
        return float(value)
    return value


class BigQueryClient:
    backend = "bigquery"

    def __init__(self, state, poll_interval=0.1, timeout=900):
        self.state = state
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.endpoint = f"gcp-bigquery://{state['location']}/{state['dataset']}"
        self.base = f"{API}/projects/{state['project']}"
        self.credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        # one session per worker thread
        self.local = threading.local()

    def session(self):
        if not hasattr(self.local, "session"):
            self.local.session = AuthorizedSession(self.credentials)
        return self.local.session

    def request(self, method, path, **kwargs):
        response = self.session().request(method, self.base + path, timeout=60, **kwargs)
        if not response.ok:
            raise RuntimeError(f"BigQuery request failed ({response.status_code}): {response.text[:300]}")
        return response.json()

    def health(self):
        dataset = self.request("GET", f"/datasets/{self.state['dataset']}")
        return {"dataset": self.state["dataset"], "location": dataset["location"],
                "input_fingerprint": self.state["input_fingerprint"], "query_cache": False,
                "maximum_bytes_billed": MAXIMUM_BYTES_BILLED}

    def verify(self, health, trace):
        if health["input_fingerprint"] != manifest_fingerprint(trace["input"]):
            raise ValueError("BigQuery tables were created for different input files; rerun bigquery_setup")
        if health["location"].lower() != self.state["location"].lower():
            raise ValueError("BigQuery dataset is in a different location")

    def fetch_rows(self, job_id):
        rows, fields, token = [], None, None
        while True:
            params = {"location": self.state["location"], "maxResults": 10000}
            if token:
                params["pageToken"] = token
            page = self.request("GET", f"/queries/{job_id}", params=params)
            if fields is None:
                fields = [field["type"] for field in page["schema"]["fields"]]
            for row in page.get("rows", []):
                rows.append([convert(cell["v"], field_type)
                             for cell, field_type in zip(row["f"], fields)])
            token = page.get("pageToken")
            if not token:
                return rows

    def invoke(self, query):
        begin = datetime.datetime.now()
        start = time.perf_counter()
        # the job id makes retried inserts safe
        job_id = f"sebs_sql_{uuid.uuid4().hex}"
        reference = {"projectId": self.state["project"], "jobId": job_id,
                     "location": self.state["location"]}
        self.request("POST", "/jobs", json={
            "jobReference": reference,
            "configuration": {"query": {
                "query": queries.QUERIES[query],
                "useLegacySql": False,
                "useQueryCache": False,
                "defaultDataset": {"projectId": self.state["project"],
                                   "datasetId": self.state["dataset"]},
                "maximumBytesBilled": str(MAXIMUM_BYTES_BILLED),
            }},
        })

        polls = 0
        while True:
            job = self.request("GET", f"/jobs/{job_id}", params={"location": self.state["location"]})
            polls += 1
            if job["status"]["state"] == "DONE":
                break
            if time.perf_counter() - start > self.timeout:
                self.request("POST", f"/jobs/{job_id}/cancel",
                             params={"location": self.state["location"]})
                break
            time.sleep(self.poll_interval)
        completed = time.perf_counter()

        error = job["status"].get("errorResult")
        done = job["status"]["state"] == "DONE" and error is None
        rows = self.fetch_rows(job_id) if done else None
        result = ExecutionResult.from_times(begin, datetime.datetime.now())
        result.request_id = job_id
        result.stats.cold_start = None

        statistics = job.get("statistics", {})
        details = statistics.get("query", {})
        times = {key: int(statistics[key]) for key in ("creationTime", "startTime", "endTime")
                 if key in statistics}
        metrics = {
            "queue_ms": times["startTime"] - times["creationTime"] if len(times) == 3 else None,
            "execution_ms": times["endTime"] - times["startTime"] if len(times) == 3 else None,
            "slot_ms": int(details["totalSlotMs"]) if "totalSlotMs" in details else None,
            "bytes_processed": int(details.get("totalBytesProcessed", 0)),
            "bytes_billed": int(details.get("totalBytesBilled", 0)),
            "cache_hit": details.get("cacheHit", False),
            "polls": polls,
            "result_fetch_s": time.perf_counter() - completed,
        }
        if not done:
            result.stats.failure = True
            result.output = {"error": "BigQuery job failed", "query": query, "request_id": job_id,
                             "provider_metrics": metrics,
                             "reason": str(error.get("message", "timeout") if error else "timeout")[:500]}
            return result
        if metrics["cache_hit"]:
            raise ValueError("BigQuery returned a cached result although the cache is disabled")
        result.output = {"result": {"queries": {query: rows}}, "query": query,
                         "request_id": job_id, "provider_metrics": metrics}
        return result
