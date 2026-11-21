"""Offline checks for non-executing, read-only PostgreSQL diagnostics."""
import ast
import unittest

from evaluation.sql.postgres_diagnose import probe_script


class DiagnosticsTests(unittest.TestCase):
    def payload(self):
        return dict(namespace="sebs_720_abc123", fingerprint="a" * 64,
                    queries={"q6": "SELECT 1"}, bucket="test", key="reports/x.json",
                    region="us-east-1")

    def test_generated_remote_program_compiles(self):
        script = probe_script(self.payload())
        program = script.split("\n", 2)[2].rsplit("\nSEBS_DIAGNOSTICS", 1)[0]
        ast.parse(program)
        self.assertIn('SET TRANSACTION READ ONLY', program)
        self.assertIn('"EXPLAIN (FORMAT JSON) " + statement', program)
        self.assertNotIn('EXPLAIN (ANALYZE', program)
        self.assertIn('SET LOCAL statement_timeout', program)
        self.assertIn('SET LOCAL ROLE sebs_reader', program)

    def test_rejects_untrusted_namespace_and_fingerprint(self):
        for field, value in (("namespace", "x; DROP TABLE y"), ("fingerprint", "bad")):
            request = self.payload()
            request[field] = value
            with self.assertRaises(ValueError):
                probe_script(request)

    def test_does_not_include_or_print_credentials(self):
        script = probe_script(self.payload())
        self.assertNotIn('print(settings', script)
        self.assertNotIn('"credentials": settings', script)
        self.assertNotIn('docker", "inspect", "--format', script)
        self.assertIn('"HostConfig"]["Memory"]', script)


if __name__ == "__main__":
    unittest.main()
