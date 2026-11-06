"""Offline tests for the Athena setup and adapter."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evaluation.sql import athena_client
from evaluation.sql.athena_client import AthenaClient, billed_bytes, convert
from evaluation.sql.athena_setup import athena_type, create_table_sql, table_location
from evaluation.sql.replay_service import run
from evaluation.sql.service import manifest_fingerprint

INPUT = {"bucket": {"bucket": "sebs-benchmarks-abc", "input": "in", "output": "out"},
         "tables": {"nation": ["in/test/nation/data_0.parquet"]}, "scale_factor": 0.01}
STATE = {"region": "eu-north-1", "workgroup": "wg", "database": "sebs_sql_db",
         "input_fingerprint": manifest_fingerprint(INPUT)}


def execution(state, scanned=1000, reused=False):
    return {"QueryExecution": {
        "Status": {"State": state, "StateChangeReason": "syntax error" if state == "FAILED" else ""},
        "Statistics": {"DataScannedInBytes": scanned, "EngineExecutionTimeInMillis": 800,
                       "QueryQueueTimeInMillis": 50, "QueryPlanningTimeInMillis": 100,
                       "ServiceProcessingTimeInMillis": 20, "TotalExecutionTimeInMillis": 900,
                       "ResultReuseInformation": {"ReusedPreviousResult": reused}},
        "EngineVersion": {"EffectiveEngineVersion": "Athena engine version 3"}}}


def page(rows, header=True):
    columns = [{"Name": "l_shipmode", "Type": "varchar"}, {"Name": "high", "Type": "bigint"},
               {"Name": "revenue", "Type": "decimal"}]
    cells = [[{"VarCharValue": value} if value is not None else {} for value in row] for row in rows]
    if header:
        cells.insert(0, [{"VarCharValue": column["Name"]} for column in columns])
    return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": columns},
                          "Rows": [{"Data": row} for row in cells]}}


class AthenaSetupTests(unittest.TestCase):
    def test_types_follow_parquet_schema(self):
        self.assertEqual(athena_type("DECIMAL(15,2)"), "decimal(15,2)")
        self.assertEqual(athena_type("BIGINT"), "bigint")
        self.assertEqual(athena_type("DATE"), "date")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            athena_type("BLOB")

    def test_table_files_must_share_a_folder(self):
        self.assertEqual(table_location("b", ["in/small/orders/data_0.parquet",
                                              "in/small/orders/data_1.parquet"]),
                         "s3://b/in/small/orders/")
        with self.assertRaisesRegex(ValueError, "single folder"):
            table_location("b", ["in/small/orders/data_0.parquet", "in/test/orders/data_0.parquet"])

    def test_table_definition_rejects_unexpected_names(self):
        sql = create_table_sql("db", "orders", [("o_orderkey", "bigint")], "s3://b/in/small/orders/")
        self.assertIn("STORED AS PARQUET", sql)
        self.assertIn("LOCATION 's3://b/in/small/orders/'", sql)
        with self.assertRaisesRegex(ValueError, "identifier"):
            create_table_sql("db", "orders; DROP", [("c", "int")], "s3://b/in/small/orders/")
        with self.assertRaisesRegex(ValueError, "location"):
            create_table_sql("db", "orders", [("c", "int")], "s3://b/x'/")


class AthenaClientTests(unittest.TestCase):
    def client(self):
        with patch("evaluation.sql.athena_client.boto3.client", return_value=Mock()):
            client = AthenaClient(STATE, poll_interval=0)
        client.client.start_query_execution.return_value = {"QueryExecutionId": "query-1"}
        return client

    def test_values_match_function_results(self):
        self.assertEqual(convert("42", "bigint"), 42)
        self.assertEqual(convert("1193053.2253", "decimal"), 1193053.2253)
        self.assertEqual(convert("1995-03-15", "date"), "1995-03-15")
        self.assertIsNone(convert(None, "bigint"))

    def test_minimum_and_rounded_billing(self):
        self.assertEqual(billed_bytes(0), 10 * 2**20)
        self.assertEqual(billed_bytes(50 * 2**20 + 1), 51 * 2**20)

    def test_successful_query_returns_validator_shape(self):
        client = self.client()
        client.client.get_query_execution.side_effect = [execution("RUNNING"),
                                                         execution("SUCCEEDED", 50 * 2**20)]
        client.client.get_paginator.return_value.paginate.return_value = [
            page([["MAIL", "64", "1.5"]]), page([["SHIP", None, "2"]], header=False)]
        result = client.invoke("q12")

        kwargs = client.client.start_query_execution.call_args.kwargs
        self.assertEqual(kwargs["QueryString"], athena_client.queries.QUERIES["q12"])
        self.assertEqual(kwargs["QueryExecutionContext"], {"Database": "sebs_sql_db"})
        self.assertFalse(kwargs["ResultReuseConfiguration"]["ResultReuseByAgeConfiguration"]["Enabled"])
        self.assertIn("ClientRequestToken", kwargs)
        self.assertFalse(result.stats.failure)
        self.assertEqual(result.request_id, "query-1")
        self.assertEqual(result.output["result"]["queries"]["q12"],
                         [["MAIL", 64, 1.5], ["SHIP", None, 2.0]])
        metrics = result.output["provider_metrics"]
        self.assertEqual(metrics["polls"], 2)
        self.assertEqual(metrics["billed_bytes"], 50 * 2**20)
        self.assertEqual(metrics["engine_ms"], 800)

    def test_failed_query_is_a_failure_without_results(self):
        client = self.client()
        client.client.get_query_execution.return_value = execution("FAILED")
        result = client.invoke("q6")
        self.assertTrue(result.stats.failure)
        self.assertEqual(result.output["reason"], "syntax error")
        client.client.get_paginator.assert_not_called()

    def test_timeout_stops_the_query(self):
        client = self.client()
        client.timeout = 0
        client.client.get_query_execution.return_value = execution("RUNNING")
        self.assertTrue(client.invoke("q6").stats.failure)
        client.client.stop_query_execution.assert_called_once_with(QueryExecutionId="query-1")

    def test_reused_result_is_rejected(self):
        client = self.client()
        client.client.get_query_execution.return_value = execution("SUCCEEDED", reused=True)
        client.client.get_paginator.return_value.paginate.return_value = [page([])]
        with self.assertRaisesRegex(ValueError, "reused"):
            client.invoke("q6")

    def test_tables_for_other_input_files_rejected(self):
        client = self.client()
        health = {"input_fingerprint": "other", "state": "ENABLED", "enforce_configuration": True}
        with self.assertRaisesRegex(ValueError, "different input files"):
            client.verify(health, {"input": INPUT})

    def test_replay_uses_athena_verification(self):
        client = self.client()
        client.health = Mock(return_value={"input_fingerprint": "other", "state": "ENABLED",
                                           "enforce_configuration": True})
        trace = {"input": INPUT, "requests": [{"at": 0, "query": "q6"}],
                 "settings": {"max-in-flight": 1}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "different input files"):
                run(trace, client, Path(directory) / "out")
            self.assertFalse((Path(directory) / "out").exists())
        client.client.start_query_execution.assert_not_called()


if __name__ == "__main__":
    unittest.main()
