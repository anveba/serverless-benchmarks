# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
"""Replay explicit individual-query arrival traces through SeBS function triggers."""
import copy
import json
import math
import time
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sebs.experiments.experiment import Experiment
from sebs.experiments.result import Result
from sebs.faas.function import Trigger
from sebs.utils import serialize


QUERIES = {"q1", "q3", "q6", "q12", "q21"}


def validate_trace(requests):
    """Return a trace copy; equal offsets represent a burst."""
    if not isinstance(requests, list) or not requests:
        raise ValueError("sql-workload.requests must be a non-empty list")
    previous = -1.0
    for request in requests:
        if not isinstance(request, dict) or set(request) != {"at", "query"}:
            raise ValueError("Each request must contain exactly at (seconds) and query")
        offset = request["at"]
        if isinstance(offset, bool) or not isinstance(offset, (int, float)):
            raise ValueError("Request offsets must be numbers")
        if not math.isfinite(offset) or offset < 0 or offset < previous:
            raise ValueError("Request offsets must be finite, nonnegative, and sorted")
        if not isinstance(request["query"], str) or request["query"] not in QUERIES:
            raise ValueError(f"Supported query IDs: {', '.join(sorted(QUERIES))}")
        previous = offset
    return copy.deepcopy(requests)


def replay(requests, invoke, max_in_flight, on_complete=None):
    """Replay arrivals independently of completions, reporting driver queue delay.

    invoke(query) returns a SeBS ExecutionResult. No retries or warm-up are added.
    The bounded worker pool can queue arrivals; schedule-relative latency includes
    that queue time. Transport exceptions have unknown server-side outcomes.
    """
    requests = validate_trace(requests)
    if type(max_in_flight) is not int or max_in_flight < 1:
        raise ValueError("max-in-flight must be a positive integer")
    origin = time.perf_counter()
    save_lock = Lock()

    def execute(index, request):
        start = time.perf_counter() - origin
        record = {"index": index, "query": request["query"],
                  "scheduled_s": request["at"], "started_s": start,
                  "queue_delay_s": max(0.0, start - request["at"])}
        result = None
        try:
            result = invoke(request["query"])
            record.update(request_id=result.request_id, failure=result.stats.failure,
                          cold_start=result.stats.cold_start, outcome="response")
        except Exception as error:
            record.update(request_id=None, failure=True, cold_start=None,
                          outcome="unknown", error=f"{type(error).__name__}: {error}")
        finish = time.perf_counter() - origin
        record.update(completed_s=finish, service_latency_s=finish-start,
                      total_latency_s=finish-request["at"])
        if on_complete is not None:
            with save_lock:
                on_complete(record, result)
        return record, result

    completed = []
    with ThreadPoolExecutor(max_workers=max_in_flight) as pool:
        futures = []
        for index, request in enumerate(requests):
            delay = request["at"] - (time.perf_counter() - origin)
            if delay > 0:
                time.sleep(delay)
            futures.append(pool.submit(execute, index, request))
        for future in as_completed(futures):
            item = future.result()
            completed.append(item)
    return sorted(completed, key=lambda item: item[0]["index"])


class SqlWorkload(Experiment):
    @staticmethod
    def name():
        return "sql-workload"

    @staticmethod
    def typename():
        return "Experiment.SqlWorkload"

    def prepare(self, sebs_client, deployment_client):
        settings = self.config.experiment_settings(self.name())
        if settings.get("benchmark", "710.sql-query") != "710.sql-query":
            raise ValueError("sql-workload currently supports only 710.sql-query")
        self._trace = validate_trace(settings["requests"])
        self._limit = settings.get("max-in-flight", 1)
        if type(self._limit) is not int or self._limit < 1:
            raise ValueError("max-in-flight must be a positive integer")
        memory = settings.get("memory", 2048)
        if type(memory) is not int or memory < 1:
            raise ValueError("memory must be a positive integer")
        trigger_type = Trigger.TriggerType.get(settings.get("trigger", "library"))
        self._out = Path(sebs_client.output_dir) / self.name()
        self._out.mkdir(parents=True, exist_ok=True)
        if (self._out / "requests.json").exists():
            raise ValueError("Use a fresh output directory; requests.json already exists")
        self._sebs = sebs_client
        self._deployment = deployment_client
        self._benchmark = sebs_client.get_benchmark("710.sql-query", deployment_client, self.config)
        self._input = self._benchmark.prepare_input(
            deployment_client.system_resources, size=settings["input-size"],
            replace_existing=self.config.update_storage,
        )
        self._benchmark.benchmark_config.memory = memory
        self._function = deployment_client.get_function(self._benchmark)
        self._function.config.memory = memory
        deployment_client.update_function(
            self._function, self._benchmark, self._benchmark.system_variant,
            self._benchmark.container_uri if self._benchmark.system_variant.is_container else "",
        )
        sebs_client.cache_client.update_function(self._function)
        triggers = self._function.triggers(trigger_type)
        self._trigger = triggers[0] if triggers else deployment_client.create_trigger(self._function, trigger_type)
        self._storage = deployment_client.system_resources.get_storage()
        self._settings = settings

    def _invoke(self, query):
        payload = copy.deepcopy(self._input)
        payload["queries"] = [query]
        return self._trigger.sync_invoke(payload)

    def run(self):
        results = Result(self.config, self._deployment.config)
        results.begin()
        trace = {"schema_version": 1, "settings": self._settings,
                 "requests": self._trace, "scale_factor": self._input["scale_factor"],
                 "input": self._input, "validation_in_timing": False,
                 "warmup": False, "automatic_retries": False}
        (self._out / "trace.json").write_text(json.dumps(trace, indent=2) + "\n")
        records = []

        def save(record, result):
            records.append(record)
            if result is not None:
                results.add_invocation(self._function, result)
            results.end()
            (self._out / "invocations.json").write_text(serialize(results))
            (self._out / "requests.json").write_text(json.dumps(records, indent=2) + "\n")

        outcomes = replay(self._trace, self._invoke, self._limit, save)
        results.end()
        # Validate outside the timed replay so host-side reference queries do not
        # alter arrival times or compete with active requests on the driver.
        for record, result in outcomes:
            record["validation_error"] = None
            record["valid"] = False
            if result is not None and not result.stats.failure:
                payload = copy.deepcopy(self._input)
                payload["queries"] = [record["query"]]
                try:
                    error = self._benchmark.validate_output(
                        payload, result.output.get("result", {}), self._storage,
                    )
                    record["validation_error"] = error
                    record["valid"] = error is None
                except Exception as error:
                    record["validation_error"] = f"{type(error).__name__}: {error}"
            self.logging.info(
                f"Request {record['index']} {record['query']}: "
                f"failure={record['failure']}, valid={record['valid']}, "
                f"latency={record['total_latency_s']:.3f}s"
            )
        (self._out / "invocations.json").write_text(serialize(results))
        (self._out / "requests.json").write_text(json.dumps([r for r, _ in outcomes], indent=2) + "\n")

    def process(self, sebs_client, deployment_client, directory, logging_filename, extend_time_interval):
        out = Path(directory) / self.name()
        results = Result.deserialize(
            json.loads((out / "invocations.json").read_text()),
            sebs_client.cache_client, sebs_client.generate_logging_handlers(logging_filename),
        )
        for function in results.functions():
            begin, end = results.times()
            margin = max(0, extend_time_interval) * 60
            deployment_client.download_metrics(
                function, int(begin-margin), int(end+margin),
                results.invocations(function), results.metrics(function),
            )
        (out / "invocations-processed.json").write_text(serialize(results))
        self.logging.info(f"Saved AWS/provider metrics to {out / 'invocations-processed.json'}")
