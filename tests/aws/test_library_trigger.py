# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
"""Offline checks for synchronous Lambda invocation and timeout behavior."""
import base64
import datetime
import io
import json
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ReadTimeoutError

from sebs.aws.triggers import LibraryTrigger
from sebs.faas.function import ExecutionResult


class LibraryTriggerTests(unittest.TestCase):
    def setUp(self):
        self.deployment = Mock()
        self.deployment.config.region = "us-east-1"
        self.client = self.deployment.session.client.return_value
        self.trigger = LibraryTrigger("test-function", self.deployment)
        self.trigger._logging = Mock()

    def test_long_invocation_returns_and_parses_output(self):
        body = {"result": {"queries": {"q6": [[42]]}}}
        self.client.invoke.return_value = {
            "StatusCode": 200,
            "ResponseMetadata": {"RequestId": "test-request"},
            "LogResult": base64.b64encode(b"REPORT").decode(),
            "Payload": io.BytesIO(json.dumps({"body": body}).encode()),
        }
        result = Mock()
        with patch("sebs.aws.triggers.ExecutionResult.from_times", return_value=result):
            with patch("sebs.aws.triggers.AWS.parse_aws_report", return_value="test-request"):
                self.assertIs(self.trigger.sync_invoke({"queries": ["q6"]}), result)
        kwargs = self.deployment.session.client.call_args.kwargs
        self.assertEqual(kwargs["region_name"], "us-east-1")
        config = kwargs["config"]
        self.assertGreater(config.read_timeout, 900)
        self.assertEqual(config.connect_timeout, 10)
        self.assertEqual(config.retries["total_max_attempts"], 1)
        self.client.invoke.assert_called_once_with(
            FunctionName="test-function",
            Payload=b'{"queries": ["q6"]}',
            LogType="Tail",
        )
        result.parse_benchmark_output.assert_called_once_with(body)

    def test_client_latency_survives_wall_clock_adjustments(self):
        self.trigger.wrapper = Mock()
        for wall_seconds in (-2, 2):
            with self.subTest(wall_seconds=wall_seconds):
                self.client.invoke.return_value = {
                    "StatusCode": 200,
                    "ResponseMetadata": {"RequestId": "test-request"},
                    "LogResult": base64.b64encode(b"REPORT").decode(),
                    "Payload": io.BytesIO(json.dumps({"body": {}}).encode()),
                }
                begin = datetime.datetime(2026, 11, 16, 23, 43, 0)
                end = begin + datetime.timedelta(seconds=wall_seconds)
                with patch("sebs.aws.triggers.datetime.datetime") as clock:
                    clock.now.side_effect = [begin, end]
                    with patch("sebs.aws.triggers.time.perf_counter_ns", side_effect=[1_000_000_000, 1_408_000_000]):
                        with patch("sebs.aws.triggers.AWS.parse_aws_report", return_value="test-request"):
                            with patch.object(ExecutionResult, "parse_benchmark_output"):
                                result = self.trigger.sync_invoke({})
                self.assertEqual(result.times.client, 408000)
                self.assertEqual(result.times.client_begin, begin)
                self.assertEqual(result.times.client_end, end)

    def test_timeout_propagates_without_a_second_invocation(self):
        self.client.invoke.side_effect = ReadTimeoutError(endpoint_url="https://example.invalid")
        with self.assertRaises(ReadTimeoutError):
            self.trigger.sync_invoke({})
        self.client.invoke.assert_called_once()

    def test_lambda_function_error_is_recorded_as_failure(self):
        self.client.invoke.return_value = {
            "StatusCode": 200,
            "ResponseMetadata": {"RequestId": "test-request"},
            "FunctionError": "Unhandled",
        }
        result = Mock()
        result.stats.failure = False
        with patch("sebs.aws.triggers.ExecutionResult.from_times", return_value=result):
            self.assertIs(self.trigger.sync_invoke({}), result)
        self.assertTrue(result.stats.failure)
        self.client.invoke.assert_called_once()


if __name__ == "__main__":
    unittest.main()
