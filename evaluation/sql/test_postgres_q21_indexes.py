"""Offline checks for the bounded Q21 covering-index treatment."""
import ast
import copy
import subprocess
import unittest

from evaluation.sql.postgres_q21_indexes import INDEXES, remote_script


class CoveringIndexTests(unittest.TestCase):
    def payload(self):
        return dict(namespace="sebs_720_abc", fingerprint="a" * 64, action="prepare",
                    indexes=copy.deepcopy(INDEXES), query="SELECT 1", region="us-east-1",
                    bucket="test", key="reports/index.json")

    def test_remote_script_compiles_and_does_not_execute_query(self):
        script = remote_script(self.payload())
        self.assertEqual(subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True).returncode, 0)
        program = script.split("\n", 2)[2].rsplit("\nSEBS_Q21_INDEXES", 1)[0]
        ast.parse(program)
        self.assertIn('"EXPLAIN (FORMAT JSON) " + request["query"]', program)
        self.assertNotIn('EXPLAIN (ANALYZE', program)
        self.assertIn('VACUUM (ANALYZE)', program)
        self.assertNotIn('VACUUM FULL', program)

    def test_profile_covers_both_correlated_lookup_paths(self):
        lineitem, orders = INDEXES
        self.assertEqual(lineitem["keys"], ["l_orderkey"])
        self.assertEqual(set(lineitem["include"]), {"l_suppkey", "l_receiptdate", "l_commitdate"})
        self.assertEqual(orders["keys"], ["o_orderkey"])
        self.assertEqual(orders["predicate"], "o_orderstatus = 'F'")

    def test_untrusted_namespace_or_profile_rejected(self):
        for key, value in (("namespace", "x; DROP TABLE y"), ("fingerprint", "bad"),
                           ("indexes", []), ("action", "rebuild")):
            payload = self.payload()
            payload[key] = value
            with self.assertRaises(ValueError):
                remote_script(payload)

    def test_ownership_and_dataset_guards_are_present(self):
        script = remote_script(self.payload())
        self.assertIn('recorded["definition"] != existing', script)
        self.assertIn('Prepared dataset fingerprint differs', script)
        self.assertIn('sql.Identifier(namespace, name)', script)
        self.assertNotIn('print(settings', script)


if __name__ == "__main__":
    unittest.main()
