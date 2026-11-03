"""Offline baseline checks; no AWS credentials, sockets, or extensions required."""
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import duckdb

from evaluation.sql.service import Handler, QueryService, literal


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "lineitem.parquet"
        with duckdb.connect() as con:
            con.execute("""CREATE TABLE lineitem AS SELECT * FROM (VALUES
                (100.0, 0.06, DATE '1994-06-01', 10),
                (200.0, 0.05, DATE '1994-07-01', 20),
                (999.0, 0.06, DATE '1995-01-01', 10)
            ) AS t(l_extendedprice, l_discount, l_shipdate, l_quantity)""")
            con.execute(f"COPY lineitem TO {literal(path)} (FORMAT PARQUET)")
        self.service = QueryService({"access_mode": "local", "scale_factor": None,
                                     "tables": {"lineitem": [str(path)]}}, threads=1)

    def tearDown(self):
        self.directory.cleanup()

    def handler(self, body):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(token="", service=self.service)
        handler.path = "/query"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.respond = Mock()
        return handler

    def test_existing_q6_sql_computes_correct_answer_repeatedly(self):
        for _ in range(2):
            output = self.service.execute("q6")
            self.assertEqual(output["result"]["queries"], {"q6": [[16.0]]})
            self.assertGreaterEqual(output["measurement"]["query_s"], 0)
        self.assertEqual(self.service.health()["connection_policy"], "fresh_per_request")
        self.assertFalse(self.service.health()["external_file_cache"])

    def test_unknown_query_rejected(self):
        with self.assertRaises(ValueError):
            self.service.execute("q2")

    def test_http_request_runs_exactly_selected_query(self):
        handler = self.handler(b'{"query":"q6"}')
        handler.do_POST()
        status, body = handler.respond.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["queries"], {"q6": [[16.0]]})

    def test_arbitrary_sql_and_extra_fields_rejected(self):
        for body in [b'{"query":"DROP TABLE lineitem"}', b'{"query":"q6","sql":"SELECT 1"}',
                     b'{"query":[]}', b'not json']:
            with self.subTest(body=body):
                handler = self.handler(body)
                handler.do_POST()
                self.assertEqual(handler.respond.call_args.args[0], 400)

    def test_credentials_are_not_exposed_in_error_response(self):
        handler = self.handler(b'{"query":"q6"}')
        handler.server.service = Mock()
        handler.server.service.execute.side_effect = RuntimeError("SECRET sensitive-value")
        handler.do_POST()
        status, body = handler.respond.call_args.args
        self.assertEqual(status, 500)
        self.assertNotIn("sensitive-value", str(body))

    def test_nonmatching_token_rejected(self):
        handler = self.handler(b'{"query":"q6"}')
        handler.server.token = "expected-token"
        handler.do_POST()
        self.assertEqual(handler.respond.call_args.args[0], 401)


if __name__ == "__main__":
    unittest.main()
