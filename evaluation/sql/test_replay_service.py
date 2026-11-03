"""Offline adapter tests; no network or AWS access."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evaluation.sql.replay_service import ServiceClient, run, verify_snapshot
from evaluation.sql.service import manifest_fingerprint
from sebs.faas.function import ExecutionResult


class ReplayServiceTests(unittest.TestCase):
    def trace(self):
        return {"requests": [{"at": 0, "query": "q6"}, {"at": 0, "query": "q1"}],
                "settings": {"max-in-flight": 1},
                "input": {"scale_factor": 1, "tables": {"lineitem": ["fixture"]}}}

    def health(self, trace):
        return {"duckdb": "2.0.0", "input_fingerprint": manifest_fingerprint(trace["input"]),
                "connection_policy": "fresh_per_request", "external_file_cache": False}

    def test_single_query_request_and_complete_response(self):
        body = {"request_id": "example", "query": "q6", "result": {"queries": {"q6": [[42]]}}}
        with patch("evaluation.sql.replay_service.urllib.request.urlopen",
                   return_value=io.BytesIO(json.dumps(body).encode())) as open_url:
            result = ServiceClient("http://localhost:8080").invoke("q6")
        request = open_url.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"query": "q6"})
        self.assertEqual(result.output, body)
        self.assertIsNone(result.stats.cold_start)
        self.assertFalse(result.stats.failure)

    def test_snapshot_changes_rejected(self):
        trace = self.trace()
        health = self.health(trace)
        trace["input"]["tables"]["lineitem"] = ["different-file"]
        with self.assertRaisesRegex(ValueError, "manifest differs"):
            verify_snapshot(health, trace)

    def test_query_selection_does_not_change_manifest_identity(self):
        first = {"scale_factor": 1, "tables": {"lineitem": ["file"]}, "queries": ["q6"]}
        second = {**first, "queries": ["q1"]}
        self.assertEqual(manifest_fingerprint(first), manifest_fingerprint(second))

    def test_wrong_query_response_is_rejected(self):
        body = {"request_id": "example", "query": "q1"}
        with patch("evaluation.sql.replay_service.urllib.request.urlopen",
                   return_value=io.BytesIO(json.dumps(body).encode())):
            with self.assertRaises(ValueError):
                ServiceClient("http://localhost:8080").invoke("q6")

    def test_run_preserves_schedule_and_records_invalid_answer(self):
        trace = self.trace()
        client = Mock(endpoint="http://localhost:8080", backend="persistent_service")
        client.health.return_value = self.health(trace)
        order = []
        def invoke(query):
            order.append("invoke")
            result = ExecutionResult()
            result.request_id = query
            result.output = {"result": {"queries": {query: [[42]]}}}
            result.stats.cold_start = None
            return result
        client.invoke.side_effect = invoke
        validator = Mock()
        def validate(data_dir, payload, output, language):
            order.append("validate")
            return "incorrect" if payload["queries"] == ["q1"] else None
        validator.validate_output.side_effect = validate
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "replay"
            with patch("evaluation.sql.replay_service.load_benchmark_input", return_value=validator):
                self.assertFalse(run(trace, client, output))
            records = json.loads((output / "requests.json").read_text())
            self.assertEqual([r["query"] for r in records], ["q6", "q1"])
            self.assertEqual([r["valid"] for r in records], [True, False])
            self.assertEqual(json.loads((output / "summary.json").read_text())["cost"], None)
        self.assertEqual(order, ["invoke", "invoke", "validate", "validate"])


if __name__ == "__main__":
    unittest.main()
