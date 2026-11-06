#!/usr/bin/env python3
"""Run a 710.sql-query query in parallel over several local function instances.

Usage: sebs local start 710.sql-query <size> out.json --deployments N, then pass out.json.
"""

import argparse
import json
import queue
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests

REQUEST_TIMEOUT = 900


class Workers:
    def __init__(self, urls: List[str], bucket: dict, access_mode: str):
        self.idle: queue.Queue = queue.Queue()
        for url in urls:
            self.idle.put(url)
        self.count = len(urls)
        self.bucket = bucket
        self.access_mode = access_mode
        self.stages: List[dict] = []

    def invoke(self, payload: dict) -> dict:
        url = self.idle.get()
        try:
            begin = time.perf_counter()
            response = requests.post(f"http://{url}", json=payload, timeout=REQUEST_TIMEOUT)
            client_time = time.perf_counter() - begin
        finally:
            self.idle.put(url)
        if response.status_code != 200:
            raise RuntimeError(f"Invocation failed on {url}: {response.text[-2000:]}")
        output = response.json()["result"]
        output["measurement"]["client_time"] = client_time * 1e6
        return output

    def run_stage(self, name: str, tasks: List[dict]) -> List[dict]:
        payloads = [
            {
                "operation": "stage",
                "bucket": self.bucket,
                "access_mode": self.access_mode,
                **task,
            }
            for task in tasks
        ]
        begin = time.perf_counter()
        with ThreadPoolExecutor(max_workers=self.count) as pool:
            outputs = list(pool.map(self.invoke, payloads))
        wall_time = time.perf_counter() - begin

        measurements = [output["measurement"] for output in outputs]
        task_times = sorted(m["client_time"] / 1e3 for m in measurements)
        written = 0
        for output in outputs:
            result = output["result"]
            written += 1 if "key" in result else 0
            written += sum(len(keys) for keys in result.get("partitions", {}).values())
        self.stages.append(
            {
                "stage": name,
                "tasks": len(tasks),
                "wall_time_ms": wall_time * 1e3,
                "task_time_ms": {
                    "min": task_times[0],
                    "median": task_times[len(task_times) // 2],
                    "max": task_times[-1],
                },
                "bytes_read": sum(m["bytes_read"] for m in measurements),
                "get_requests": sum(m["get_requests"] for m in measurements),
                "head_requests": sum(m["head_requests"] for m in measurements),
                "objects_written": written,
            }
        )
        return outputs


def split_files(files: List[str], count: int) -> List[List[str]]:
    groups: List[List[str]] = [[] for _ in range(min(count, len(files)))]
    for idx, file in enumerate(files):
        groups[idx % len(groups)].append(file)
    return groups


def run_merge(workers: Workers, query: str, partials: List[str]) -> List[list]:
    output = workers.run_stage(
        "final",
        [
            {
                "stage": f"{query}.final",
                "inputs": {"partials": partials},
                "output": {"type": "rows"},
            }
        ],
    )
    return output[0]["result"]["rows"]


def run_scan_aggregate(workers: Workers, query: str, tables: dict, run_id: str) -> List[list]:
    tasks = [
        {
            "stage": f"{query}.partial",
            "inputs": {"lineitem": files},
            "output": {"type": "object", "prefix": f"{run_id}/partial-{idx}"},
        }
        for idx, files in enumerate(split_files(tables["lineitem"], workers.count))
    ]
    outputs = workers.run_stage("partial", tasks)
    return run_merge(workers, query, [output["result"]["key"] for output in outputs])


def shuffle_table(
    workers: Workers, query: str, table: str, files: List[str], run_id: str, partitions: int
) -> Dict[str, List[str]]:
    tasks = [
        {
            "stage": f"{query}.partition_{table}",
            "inputs": {table: group},
            "output": {
                "type": "partitions",
                "prefix": f"{run_id}/shuffle-{table}-{idx}",
                "key": "orderkey",
                "count": partitions,
            },
        }
        for idx, group in enumerate(split_files(files, workers.count))
    ]
    merged: Dict[str, List[str]] = {}
    for output in workers.run_stage(f"partition_{table}", tasks):
        for partition, keys in output["result"]["partitions"].items():
            merged.setdefault(partition, []).extend(keys)
    return merged


def run_shuffle_join(
    workers: Workers, query: str, tables: dict, run_id: str, partitions: int
) -> List[list]:
    lineitem = shuffle_table(workers, query, "lineitem", tables["lineitem"], run_id, partitions)
    orders = shuffle_table(workers, query, "orders", tables["orders"], run_id, partitions)

    # skip partitions that are empty on either side
    tasks = [
        {
            "stage": f"{query}.join",
            "inputs": {"lineitem": lineitem[partition], "orders": orders[partition]},
            "output": {"type": "object", "prefix": f"{run_id}/join-{partition}"},
        }
        for partition in sorted(lineitem.keys() & orders.keys(), key=int)
    ]
    outputs = workers.run_stage("join", tasks)
    return run_merge(workers, query, [output["result"]["key"] for output in outputs])


def rows_match(rows: List[list], baseline: List[list], tolerance: float = 1e-6) -> bool:
    if len(rows) != len(baseline):
        return False
    for row, baseline_row in zip(rows, baseline):
        for value, reference in zip(row, baseline_row):
            if isinstance(value, (int, float)) and isinstance(reference, (int, float)):
                if abs(value - reference) > tolerance * max(abs(reference), 1):
                    return False
            elif value != reference:
                return False
    return True


def run(args: argparse.Namespace) -> dict:
    with open(args.deployment) as f:
        deployment = json.load(f)
    urls = [function["url"] for function in deployment["functions"]]
    if args.workers is not None:
        urls = urls[: args.workers]
    event = deployment["inputs"][0]

    workers = Workers(urls, event["bucket"], args.access_mode)
    partitions = args.partitions if args.partitions is not None else len(urls)
    run_id = f"parallel-{uuid.uuid4().hex[:8]}"

    begin = time.perf_counter()
    if args.query == "q12":
        rows = run_shuffle_join(workers, args.query, event["tables"], run_id, partitions)
    else:
        rows = run_scan_aggregate(workers, args.query, event["tables"], run_id)
    total_time = time.perf_counter() - begin

    baseline: Optional[dict] = None
    matches = None
    if args.baseline:
        begin = time.perf_counter()
        output = workers.invoke({**event, "queries": [args.query], "access_mode": args.access_mode})
        baseline = {
            "client_time_ms": (time.perf_counter() - begin) * 1e3,
            **output["measurement"]["queries"][args.query],
        }
        matches = rows_match(rows, output["result"]["queries"][args.query])

    return {
        "query": args.query,
        "scale_factor": event["scale_factor"],
        "workers": len(urls),
        "partitions": partitions,
        "total_time_ms": total_time * 1e3,
        "get_requests": sum(stage["get_requests"] for stage in workers.stages),
        "objects_written": sum(stage["objects_written"] for stage in workers.stages),
        "stages": workers.stages,
        "rows": rows,
        "baseline": baseline,
        "matches_baseline": matches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("deployment", help="Output file of `sebs local start`.")
    parser.add_argument("--query", choices=["q1", "q6", "q12"], default="q1")
    parser.add_argument("--workers", type=int, help="Use only this number of instances.")
    parser.add_argument("--partitions", type=int, help="Shuffle partitions, default: workers.")
    parser.add_argument("--access-mode", choices=["httpfs", "download"], default="httpfs")
    parser.add_argument(
        "--baseline", action="store_true", help="Also run the query on one instance and compare."
    )
    args = parser.parse_args()

    result = run(args)
    print(json.dumps(result, indent=2))
    return 1 if result["matches_baseline"] is False else 0


if __name__ == "__main__":
    sys.exit(main())
