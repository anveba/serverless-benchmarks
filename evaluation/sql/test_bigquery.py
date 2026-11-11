"""Offline tests for the BigQuery setup and adapter."""
import unittest
from unittest.mock import Mock, patch

from evaluation.sql import bigquery_client
from evaluation.sql.bigquery_client import BigQueryClient, convert
from evaluation.sql.bigquery_setup import table_definition, table_uri
from evaluation.sql.service import manifest_fingerprint

INPUT = {"bucket": {"bucket": "sebs-benchmarks-abc", "input": "in", "output": "out"},
         "tables": {"nation": ["in/small/nation/data_0.parquet"]}, "scale_factor": 1}
STATE = {"project": "p", "location": "europe-west1", "dataset": "ds",
         "input_fingerprint": manifest_fingerprint(INPUT)}


def job(state="DONE", error=None, cache_hit=False):
    status = {"state": state}
    if error:
        status["errorResult"] = {"message": error}
    return {"status": status, "statistics": {
        "creationTime": "1000", "startTime": "1050", "endTime": "1850",
        "query": {"totalBytesProcessed": "52428800", "totalBytesBilled": "53477376",
                  "totalSlotMs": "1200", "cacheHit": cache_hit}}}


def results(rows, token=None):
    page = {"schema": {"fields": [{"name": "l_shipmode", "type": "STRING"},
                                  {"name": "high", "type": "INTEGER"},
                                  {"name": "revenue", "type": "NUMERIC"}]},
            "rows": [{"f": [{"v": value} for value in row]} for row in rows]}
    if token:
        page["pageToken"] = token
    return page


class BigQuerySetupTests(unittest.TestCase):
    def test_table_files_must_share_a_folder(self):
        self.assertEqual(table_uri("b", ["in/small/orders/data_0.parquet", "in/small/orders/data_1.parquet"]),
                         "gs://b/in/small/orders/*.parquet")
        with self.assertRaisesRegex(ValueError, "single folder"):
            table_uri("b", ["in/small/orders/data_0.parquet", "in/test/orders/data_0.parquet"])

    def test_table_definition(self):
        body = table_definition("p", "ds", "orders", "gs://b/in/small/orders/*.parquet")
        self.assertEqual(body["externalDataConfiguration"]["sourceFormat"], "PARQUET")
        with self.assertRaisesRegex(ValueError, "identifier"):
            table_definition("p", "ds", "orders; DROP", "gs://b/x/*.parquet")


class BigQueryClientTests(unittest.TestCase):
    def client(self, responses):
        with patch("evaluation.sql.bigquery_client.google.auth.default", return_value=(Mock(), "p")):
            client = BigQueryClient(STATE, poll_interval=0)
        client.request = Mock(side_effect=responses)
        return client

    def test_values_match_function_results(self):
        self.assertEqual(convert("42", "INTEGER"), 42)
        self.assertEqual(convert("1193053.2253", "NUMERIC"), 1193053.2253)
        self.assertEqual(convert("1995-03-15", "DATE"), "1995-03-15")
        self.assertIsNone(convert(None, "INTEGER"))

    def test_successful_query_returns_validator_shape(self):
        client = self.client([{}, job("RUNNING"), job(), results([["MAIL", "64", "1.5"]], token="t"),
                              results([["SHIP", None, "2"]])])
        result = client.invoke("q12")
        insert = client.request.call_args_list[0]
        config = insert.kwargs["json"]["configuration"]["query"]
        self.assertEqual(config["query"], bigquery_client.queries.QUERIES["q12"])
        self.assertFalse(config["useQueryCache"])
        self.assertFalse(config["useLegacySql"])
        self.assertEqual(config["defaultDataset"]["datasetId"], "ds")
        self.assertFalse(result.stats.failure)
        self.assertEqual(result.output["result"]["queries"]["q12"], [["MAIL", 64, 1.5], ["SHIP", None, 2.0]])
        metrics = result.output["provider_metrics"]
        self.assertEqual((metrics["queue_ms"], metrics["execution_ms"], metrics["polls"]), (50, 800, 2))
        self.assertEqual(metrics["bytes_billed"], 53477376)

    def test_failed_query_is_a_failure_without_results(self):
        client = self.client([{}, job(error="Syntax error")])
        result = client.invoke("q6")
        self.assertTrue(result.stats.failure)
        self.assertEqual(result.output["reason"], "Syntax error")
        self.assertEqual(client.request.call_count, 2)

    def test_timeout_cancels_the_job(self):
        client = self.client([{}, job("RUNNING"), {}])
        client.timeout = 0
        self.assertTrue(client.invoke("q6").stats.failure)
        self.assertIn("/cancel", client.request.call_args_list[-1].args[1])

    def test_cached_result_is_rejected(self):
        client = self.client([{}, job(cache_hit=True), results([])])
        with self.assertRaisesRegex(ValueError, "cached"):
            client.invoke("q6")

    def test_tables_for_other_input_files_rejected(self):
        client = self.client([])
        with self.assertRaisesRegex(ValueError, "different input files"):
            client.verify({"input_fingerprint": "other", "location": "europe-west1"}, {"input": INPUT})


if __name__ == "__main__":
    unittest.main()
