"""Offline checks for replay orchestration and complete-result reporting."""
import ast
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from evaluation.sql.workloads import remote_script, summarize
from evaluation.sql.provision_server import server_bootstrap


class WorkloadTests(unittest.TestCase):
    def test_sparse_remote_has_correct_timing_and_no_shell_errors(self):
        script = remote_script("sparse", 2, 100, "prefix/", "workloads-100")
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = script.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n")[0]
        ast.parse(source)
        self.assertIn("'interval': 30", source)
        self.assertIn("export HOME=/root", script)
        self.assertIn('if result.returncode:', source)
        self.assertIn('if (root /', source)
        self.assertEqual(subprocess.run(["bash", "-n"], input=server_bootstrap(), text=True).returncode, 0)

    def test_report_checks_completeness_and_counts_billed_time(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for backend in ["ec2", "lambda"]:
                path = root / "moderate/repeat-1" / backend
                path.mkdir(parents=True)
                values = {
                    "summary.json": dict(submitted=1, valid=1, execution_failures=0, observation_seconds=3600),
                    "trace.json": {"trace": {"requests": [{"at": 0, "query": "q6"}]}},
                    "requests.json": [dict(index=0, valid=True, failure=False, total_latency_s=2,
                        queue_delay_s=.5, service_latency_s=1.5, cold_start=True)],
                    "responses.json": {"0": {"provider_metrics": {"gb_seconds": 4}}},
                }
                for name, value in values.items():
                    (path / name).write_text(json.dumps(value))
            report = summarize(root, .1, .01, .2)["workloads"]["moderate"]
            self.assertAlmostEqual(report["ec2"]["estimated_window_cost_usd"], .1)
            self.assertAlmostEqual(report["lambda"]["estimated_window_cost_usd"], .0400002)
            (root / "moderate/repeat-1/lambda/responses.json").write_text('{}')
            with self.assertRaisesRegex(ValueError, "Incomplete/invalid"):
                summarize(root, .1, .01, .2)


if __name__ == "__main__":
    unittest.main()
