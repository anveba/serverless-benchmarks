# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
"""Offline replay tests: scheduling, failures, query isolation, and validation."""
import json
import tempfile
import time
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sebs.experiments.config import Config
from sebs.experiments.sql_workload import SqlWorkload, replay, validate_trace
from sebs.faas.function import ExecutionResult

ROOT = Path(__file__).resolve().parents[1]


def result(request_id="request"):
    run = ExecutionResult()
    run.request_id = request_id
    run.output = {"result": {"result": {"queries": {"q6": [[42]]}}}}
    return run


class SqlWorkloadTests(unittest.TestCase):
    def test_trace_preserves_repetition_and_bursts(self):
        trace = [{"at": 0, "query": "q6"}, {"at": 0, "query": "q6"}]
        checked = validate_trace(trace)
        checked[0]["query"] = "q1"
        self.assertEqual(trace[0]["query"], "q6")

    def test_bad_traces_rejected_before_invocations(self):
        for trace in [[], [{"at": -1, "query": "q6"}],
                      [{"at": float("nan"), "query": "q6"}],
                      [{"at": True, "query": "q6"}],
                      [{"at": 0, "query": "q2"}],
                      [{"at": 0, "query": "q6", "extra": 1}],
                      [{"at": 2, "query": "q6"}, {"at": 1, "query": "q1"}]]:
            invoke = Mock()
            with self.subTest(trace=trace):
                with self.assertRaises(ValueError):
                    replay(trace, invoke, 1)
                invoke.assert_not_called()

    def test_queued_requests_include_driver_delay(self):
        def invoke(query):
            time.sleep(0.02)
            return result(query)
        runs = replay([{"at": 0, "query": q} for q in ["q6", "q1"]], invoke, 1)
        second = runs[1][0]
        self.assertGreater(second["queue_delay_s"], 0.01)
        self.assertGreater(second["total_latency_s"], second["service_latency_s"])
        self.assertEqual([r[0]["query"] for r in runs], ["q6", "q1"])

    def test_pool_respects_limit_and_checkpoints_each_response(self):
        state = {"active": 0, "peak": 0}
        lock = Lock()
        def invoke(query):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.01)
            with lock:
                state["active"] -= 1
            return result(query)
        save = Mock()
        runs = replay([{"at": 0, "query": q} for q in ["q6", "q1", "q3"]], invoke, 2, save)
        self.assertLessEqual(state["peak"], 2)
        self.assertEqual(len(runs), 3)
        self.assertEqual(save.call_count, 3)

    def test_request_does_not_start_before_its_arrival(self):
        runs = replay([{"at": 0.02, "query": "q6"}], lambda query: result(), 1)
        self.assertGreaterEqual(runs[0][0]["started_s"], 0.02)

    def test_transport_failure_is_retained_and_not_retried(self):
        invoke = Mock(side_effect=TimeoutError("client stopped waiting"))
        record, run = replay([{"at": 0, "query": "q6"}], invoke, 1)[0]
        self.assertIsNone(run)
        self.assertTrue(record["failure"])
        self.assertEqual(record["outcome"], "unknown")
        invoke.assert_called_once_with("q6")

    def test_payload_is_single_query_without_mutating_base(self):
        experiment = SqlWorkload(Config())
        experiment._input = {"queries": ["q1", "q6"], "tables": {"lineitem": ["file"]}}
        experiment._trigger = Mock()
        experiment._invoke("q6")
        payload = experiment._trigger.sync_invoke.call_args.args[0]
        self.assertEqual(payload["queries"], ["q6"])
        self.assertEqual(experiment._input["queries"], ["q1", "q6"])

    def test_registered_settings_survive_result_config_roundtrip(self):
        c = json.loads((ROOT / "configs/sql-aws-workload.example.json").read_text())["experiments"]
        cfg = Config.deserialize(c)
        restored = Config.deserialize(cfg.serialize())
        self.assertEqual(restored.experiment_settings("sql-workload"), c["sql-workload"])

    def test_validation_runs_after_replay_and_records_bad_answers(self):
        settings = json.loads((ROOT / "configs/sql-aws-workload.example.json").read_text())
        experiment = SqlWorkload(Config.deserialize(settings["experiments"]))
        events = []
        experiment._trace = [{"at": 0, "query": "q6"}, {"at": 0, "query": "q1"}]
        experiment._settings = {"requests": experiment._trace}
        experiment._limit = 1
        experiment._input = {"scale_factor": 1, "queries": ["q1", "q6"]}
        experiment._function = SimpleNamespace(name="test-function")
        experiment._deployment = SimpleNamespace(config=None)
        experiment._storage = None
        experiment._benchmark = Mock()
        def validate(payload, output, storage):
            events.append("validate")
            return "wrong answer" if payload["queries"] == ["q1"] else None
        experiment._benchmark.validate_output.side_effect = validate
        def invoke(query):
            events.append("invoke")
            return result(query)
        experiment._invoke = invoke
        with tempfile.TemporaryDirectory() as folder:
            experiment._out = Path(folder)
            experiment.run()
            records = json.loads((Path(folder) / "requests.json").read_text())
            self.assertTrue(records[0]["valid"])
            self.assertFalse(records[1]["valid"])
            self.assertEqual(records[1]["validation_error"], "wrong answer")
            self.assertTrue((Path(folder) / "invocations.json").exists())
        self.assertEqual(events, ["invoke", "invoke", "validate", "validate"])


if __name__ == "__main__":
    unittest.main()
