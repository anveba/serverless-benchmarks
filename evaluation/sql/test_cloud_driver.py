"""Offline tests for cloud replay response compatibility and bootstrap scripts."""
import ast
import base64
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

    def test_wrong_lambda_memory_rejected_before_replay(self):
        client = self.client()
        health = {"Runtime": "python3.12", "Architectures": ["x86_64"],
                  "MemorySize": 1024, "State": "Active", "LastUpdateStatus": "Successful"}
        with self.assertRaisesRegex(ValueError, "memory differs"):
            client.verify(health, {"settings": {"memory": 2048}})
        client.client.invoke.assert_not_called()

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
