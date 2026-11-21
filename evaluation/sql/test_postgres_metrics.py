"""Checks for historical meter scope and heterogeneous CloudWatch periods."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from evaluation.sql.postgres_metrics import collect_failure_logs, metric_queries, observation_window


class HistoricalMetricsTests(unittest.TestCase):
    def test_basic_cpu_and_ebs_have_distinct_periods(self):
        queries = metric_queries({"InstanceId": "i-test", "Monitoring": {"State": "disabled"}},
                                 [{"VolumeId": "vol-test"}])
        self.assertEqual(queries[0]["MetricStat"]["Period"], 300)
        self.assertTrue(all(q["MetricStat"]["Period"] == 60 for q in queries
                            if q["MetricStat"]["Metric"]["Namespace"] == "AWS/EBS"))
        self.assertEqual(queries[0]["MetricStat"]["Metric"]["Dimensions"][0]["Value"], "i-test")
        self.assertTrue(all(q["MetricStat"]["Metric"]["Dimensions"][0]["Value"] == "vol-test"
                            for q in queries if q["MetricStat"]["Metric"]["Namespace"] == "AWS/EBS"))
        detailed = metric_queries({"InstanceId": "i-test", "Monitoring": {"State": "enabled"}}, [])
        self.assertEqual(detailed[0]["MetricStat"]["Period"], 60)

    def test_scope_keeps_failed_queries_and_requires_all_saved_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                observation_window(root)
            for i, name in enumerate(("q6", "q1", "q12", "q3", "q21")):
                (root / name).mkdir()
                data = {"begin_time": i * 10, "end_time": i * 10 + 5,
                        "_invocations": {"function": {"id": {"stats": {"failure": i == 0}}}}}
                (root / name / "experiments.json").write_text(json.dumps(data))
            records = observation_window(root)
            self.assertEqual(len(records), 5)
            self.assertEqual(records[0]["failures"], 1)
            self.assertEqual(records[-1]["end"], 45)


    def test_single_query_window_does_not_require_unrequested_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "q21").mkdir()
            data = {"begin_time": 100, "end_time": 340,
                    "_invocations": {"fn": {"id": {"stats": {"failure": True}}}}}
            (root / "q21/experiments.json").write_text(json.dumps(data))
            records = observation_window(root, ["q21"])
            self.assertEqual(records, [{"query": "q21", "begin": 100, "end": 340, "failures": 1}])

    def test_collects_distinct_volume_limits_and_instance_credit_meters(self):
        queries = metric_queries({"InstanceId": "i-test"}, [{"VolumeId": "vol-test"}])
        by_id = {q["Id"]: q["MetricStat"] for q in queries}
        self.assertEqual(by_id["v0_iops_exceeded"]["Metric"]["MetricName"], "VolumeIOPSExceededCheck")
        self.assertEqual(by_id["inst_ebs_byte_balance"]["Metric"]["MetricName"], "EBSByteBalance%")
        self.assertEqual(by_id["inst_ebs_byte_balance"]["Stat"], "Minimum")
        self.assertEqual(by_id["inst_ebs_iops_exceeded"]["Stat"], "Maximum")

    def test_failure_log_collection_reads_saved_window_without_invoking_sql(self):
        with tempfile.TemporaryDirectory() as temp:
            root, session = Path(temp), Mock()
            (root / "q21").mkdir()
            data = {"_invocations": {"fn": {"id": {"stats": {"failure": True}}}}}
            (root / "q21/experiments.json").write_text(json.dumps(data))
            events = [{"message": "[ERROR] QueryCanceled", "timestamp": 100000}]
            paginator = session.client.return_value.get_paginator.return_value
            paginator.paginate.return_value = [{"events": events}]
            observations = [{"query": "q21", "begin": 100, "end": 340, "failures": 1}]
            self.assertEqual(collect_failure_logs(session, root, observations, "region"), {"q21": events})
            session.client.assert_called_once_with("logs", region_name="region")
            paginator.paginate.assert_called_once_with(logGroupName="/aws/lambda/fn", startTime=90000, endTime=350000)


if __name__ == "__main__":
    unittest.main()
