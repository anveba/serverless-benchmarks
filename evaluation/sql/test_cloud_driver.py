"""Offline tests for cloud replay response compatibility and bootstrap scripts."""
import ast
import base64
import datetime
import io
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from evaluation.sql.lambda_client import LambdaClient
from evaluation.sql.launch_driver import bootstrap


class CloudDriverTests(unittest.TestCase):
    def client(self):
        with patch("evaluation.sql.lambda_client.boto3.client", return_value=Mock()):
            return LambdaClient("function", {"queries": ["q1", "q6"], "tables": {}})

    def test_lambda_normalizes_complete_response_and_billing(self):
        client = self.client()
        body = {"begin": "1", "end": "2", "is_cold": False,
                "result": {"result": {"queries": {"q6": [[42]]}}, "measurement": {}}}
        payload = io.BytesIO(json.dumps({"body": json.dumps(body)}).encode())
        report = ("REPORT RequestId: request-1\tDuration: 10.00 ms\tBilled Duration: 11 ms"
                  "\tMemory Size: 2048 MB\tMax Memory Used: 100 MB")
        client.client.invoke.return_value = {"StatusCode": 200, "Payload": payload,
            "ResponseMetadata": {"RequestId": "request-1"},
            "LogResult": base64.b64encode(report.encode()).decode()}
        result = client.invoke("q6")
        self.assertTrue(payload.closed)
        submitted = json.loads(client.client.invoke.call_args.kwargs["Payload"])
        self.assertEqual(submitted["queries"], ["q6"])
        self.assertEqual(client.input["queries"], ["q1", "q6"])
        self.assertEqual(result.output["result"]["queries"], {"q6": [[42]]})
        self.assertEqual(result.output["provider_metrics"]["billed_ms"], 11)
        self.assertAlmostEqual(result.output["provider_metrics"]["gb_seconds"], 0.022)

    def test_function_error_remains_failure(self):
        client = self.client()
        payload = io.BytesIO(b'{"error":"failed"}')
        client.client.invoke.return_value = {"StatusCode": 200, "FunctionError": "Unhandled",
            "Payload": payload, "ResponseMetadata": {"RequestId": "request-1"}}
        self.assertTrue(client.invoke("q6").stats.failure)
        self.assertTrue(payload.closed)
        client.client.invoke.assert_called_once()

    def test_actual_cold_and_warm_report_trailing_tab_keeps_response_and_billing(self):
        # AWS ends both cold and warm REPORT records with a tab plus newline.
        # splitlines() removes the newline but retains the tab: that used to
        # leave an empty token in the shared report parser and lose the result.
        for cold in [True, False]:
            with self.subTest(cold=cold):
                client = self.client()
                report = ('REPORT RequestId: request-1\tDuration: 2810.74 ms\tBilled Duration: 2884 ms\t'
                          'Memory Size: 512 MB\tMax Memory Used: 108 MB'
                          + ('\tInit Duration: 72.82 ms' if cold else '') + '\t\n')
                body = {'begin':'1','end':'2','is_cold':cold,
                        'result':{'result':{'queries':{'q6':[[42]]}},'measurement':{}}}
                payload = io.BytesIO(json.dumps({'body':body}).encode())
                client.client.invoke.return_value = {'StatusCode':200,'Payload':payload,
                    'ResponseMetadata':{'RequestId':'request-1'},
                    'LogResult':base64.b64encode(('START RequestId: request-1\n'+report).encode()).decode()}
                result = client.invoke('q6')
                self.assertFalse(result.stats.failure)
                self.assertEqual(result.stats.cold_start,cold)
                self.assertEqual(result.output['result']['queries']['q6'],[[42]])
                self.assertEqual(result.output['provider_metrics']['billed_ms'],2884)
                self.assertAlmostEqual(result.output['provider_metrics']['gb_seconds'],1.442)
                self.assertEqual(result.provider_times.initialization,72820 if cold else 0)
                self.assertTrue(payload.closed)
                client.client.invoke.assert_called_once()

    def test_meter_parser_failure_preserves_received_rows_and_exposes_unknown_usage(self):
        client = self.client()
        body = {'begin':'1','end':'2','is_cold':False,
                'result':{'result':{'queries':{'q6':[[42]]}},'measurement':{}}}
        payload = io.BytesIO(json.dumps({'body':body}).encode())
        client.client.invoke.return_value = {'StatusCode':200,'Payload':payload,
            'ResponseMetadata':{'RequestId':'request-1'},
            'LogResult':base64.b64encode(b'REPORT RequestId: request-1\tunrecognized\t\n').decode()}
        with patch('evaluation.sql.lambda_client.AWS.parse_aws_report',side_effect=IndexError('malformed')):
            result = client.invoke('q6')
        self.assertFalse(result.stats.failure)
        self.assertEqual(result.output['result']['queries']['q6'],[[42]])
        self.assertEqual(result.output['provider_metrics']['native_report_parse_error'],'IndexError')
        self.assertFalse(result.output['provider_metrics']['native_report_available'])
        self.assertIsNone(result.output['provider_metrics']['gb_seconds'])
        self.assertEqual(result.output['native_report'],'REPORT RequestId: request-1\tunrecognized')
        client.client.invoke.assert_called_once()

    def test_wrong_lambda_memory_rejected_before_replay(self):
        client = self.client()
        health = {"Runtime": "python3.12", "Architectures": ["x86_64"],
                  "MemorySize": 1024, "State": "Active", "LastUpdateStatus": "Successful"}
        with self.assertRaisesRegex(ValueError, "memory differs"):
            client.verify(health, {"settings": {"memory": 2048}})
        client.client.invoke.assert_not_called()

    def test_failed_lambda_keeps_report_and_billing_without_retry(self):
        client = self.client()
        report = ('REPORT RequestId: request-1\tDuration: 120000.00 ms\tBilled Duration: 120100 ms'
                  '\tMemory Size: 512 MB\tMax Memory Used: 108 MB\tInit Duration: 100.00 ms')
        payload = io.BytesIO(b'{"errorType":"RuntimeError"}')
        client.client.invoke.return_value = {'StatusCode': 200, 'FunctionError': 'Unhandled', 'Payload': payload,
            'ResponseMetadata': {'RequestId': 'request-1'},
            'LogResult': base64.b64encode(('[ERROR] failing: example\n' + report + '\n').encode()).decode()}
        result = client.invoke('q6')
        self.assertTrue(result.stats.failure)
        self.assertTrue(result.stats.cold_start)
        self.assertTrue(payload.closed)
        self.assertEqual(result.output['provider_metrics']['billed_ms'], 120100)
        self.assertAlmostEqual(result.output['provider_metrics']['gb_seconds'], 60.05)
        client.client.invoke.assert_called_once()

    def test_missing_native_report_is_unknown_usage_not_zero(self):
        client = self.client()
        client.client.invoke.return_value = {'StatusCode': 200, 'FunctionError': 'Unhandled',
            'Payload': io.BytesIO(b'{}'), 'ResponseMetadata': {'RequestId': 'request-1'}}
        result = client.invoke('q6')
        self.assertFalse(result.output['provider_metrics']['native_report_available'])
        self.assertIsNone(result.output['provider_metrics']['gb_seconds'])

    def test_client_elapsed_time_uses_monotonic_clock_including_payload_read(self):
        client = self.client()
        client.client.invoke.return_value = {'StatusCode': 200, 'FunctionError': 'Unhandled',
            'Payload': io.BytesIO(b'{}'), 'ResponseMetadata': {'RequestId': 'request-1'}}
        begin = datetime.datetime(2026, 11, 20, 16, 0, 0)
        with patch('evaluation.sql.lambda_client.datetime.datetime') as clock:
            clock.now.side_effect = [begin, begin-datetime.timedelta(seconds=5)]
            with patch('evaluation.sql.lambda_client.time.perf_counter_ns', side_effect=[1_000_000_000, 1_125_000_000]):
                result = client.invoke('q6')
        self.assertEqual(result.times.client, 125000)

    def test_bootstrap_syntax_and_checksum_check(self):
        command = bootstrap("example-bucket", "path/code.tar.gz", "a" * 64)
        result = subprocess.run(["bash", "-n"], input=command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = command.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
        ast.parse(source)
        self.assertIn("checksum mismatch", source)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", command)


if __name__ == "__main__":
    unittest.main()
