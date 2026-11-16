"""Native SDK reports survive processing; CloudWatch fills only missing metrics."""
import copy
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from sebs.aws.aws import AWS
from sebs.faas.function import ExecutionResult
from sebs.utils import LoggingBase


def report(request_id):
    return (f"REPORT RequestId: {request_id}\tDuration: 5.00 ms\tBilled Duration: 6 ms\t"
            "Memory Size: 512 MB\tMax Memory Used: 100 MB\tInit Duration: 1.00 ms")


def recorded(request_id="native"):
    result = ExecutionResult()
    AWS.parse_aws_report(report(request_id), result)
    return result


def range_error():
    return ClientError({"Error": {"Code": "MalformedQueryException", "Message":
        "Query's end date and time is either before the log groups creation time or exceeds retention"}}, "StartQuery")


class MetricsTests(unittest.TestCase):
    def deployment(self):
        deployment = AWS.__new__(AWS)
        LoggingBase.__init__(deployment)
        deployment.logs_client = Mock()
        deployment.logs_client.start_query.return_value = {"queryId": "query"}
        deployment.logs_client.get_query_results.return_value = {
            "status": "Complete", "results": [[{"field": "@message", "value": report("missing")}]]}
        return deployment

    def test_complete_sdk_metrics_skip_cloudwatch_and_remain_unchanged(self):
        deployment = self.deployment()
        result = recorded()
        before = copy.deepcopy((result.billing.__dict__, result.provider_times.__dict__, result.stats.__dict__))
        deployment.download_metrics("function", 100, 101, {"native": result}, {})
        deployment.logs_client.start_query.assert_not_called()
        deployment.logs_client.describe_log_groups.assert_not_called()
        self.assertEqual(before, (result.billing.__dict__, result.provider_times.__dict__, result.stats.__dict__))

    def test_no_invocations_do_not_query_cloudwatch(self):
        deployment = self.deployment()
        deployment.download_metrics("function", 100, 101, {}, {})
        deployment.logs_client.start_query.assert_not_called()

    def test_partial_results_fill_missing_report_without_overwriting_native(self):
        deployment = self.deployment()
        native, missing = recorded(), ExecutionResult()
        native.provider_times.execution = 99999
        deployment.logs_client.get_query_results.return_value["results"].append([
            {"field": "@message", "value": report("native")}])
        with patch("sebs.aws.aws.time.sleep"):
            deployment.download_metrics("function", 100, 101, {"native": native, "missing": missing}, {})
        self.assertEqual(native.provider_times.execution, 99999)
        self.assertEqual(missing.provider_times.execution, 5000)
        self.assertEqual(missing.billing.gb_seconds, .003)
        params = deployment.logs_client.start_query.call_args.kwargs
        self.assertEqual(params["startTime"], 40)
        self.assertEqual(params["endTime"], 161)

    def test_new_log_group_creation_expands_end_boundary_once(self):
        deployment = self.deployment()
        deployment.logs_client.start_query.side_effect = [range_error(), {"queryId": "retry"}]
        deployment.logs_client.describe_log_groups.return_value = {"logGroups": [
            {"logGroupName": "/aws/lambda/function-extra", "creationTime": 999999999},
            {"logGroupName": "/aws/lambda/function", "creationTime": 2000000},
        ]}
        result = ExecutionResult()
        with patch("sebs.aws.aws.time.sleep"):
            deployment.download_metrics("function", 100, 101, {"missing": result}, {})
        self.assertEqual(deployment.logs_client.start_query.call_count, 2)
        self.assertEqual(deployment.logs_client.start_query.call_args.kwargs["endTime"], 2060)
        self.assertEqual(result.billing.billed_time, 6)

    def test_retention_error_is_not_hidden_by_adjusting_to_now(self):
        deployment = self.deployment()
        deployment.logs_client.start_query.side_effect = range_error()
        deployment.logs_client.describe_log_groups.return_value = {"logGroups": [
            {"logGroupName": "/aws/lambda/function", "creationTime": 0}]}
        with self.assertRaises(ClientError):
            deployment.download_metrics("function", 100, 101, {"missing": ExecutionResult()}, {})
        self.assertEqual(deployment.logs_client.start_query.call_count, 1)

    def test_permission_errors_are_not_retried_or_masked(self):
        deployment = self.deployment()
        deployment.logs_client.start_query.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "StartQuery")
        with self.assertRaises(ClientError):
            deployment.download_metrics("function", 100, 101, {"missing": ExecutionResult()}, {})
        deployment.logs_client.describe_log_groups.assert_not_called()

    def test_scheduled_query_is_polled_until_complete(self):
        deployment = self.deployment()
        complete = deployment.logs_client.get_query_results.return_value
        deployment.logs_client.get_query_results.side_effect = [
            {"status": "Scheduled", "results": []}, {"status": "Running", "results": []}, complete]
        with patch("sebs.aws.aws.time.sleep"):
            deployment.download_metrics("function", 100, 101, {"missing": ExecutionResult()}, {})
        self.assertEqual(deployment.logs_client.get_query_results.call_count, 3)

    def test_duplicate_log_entries_are_harmless(self):
        deployment = self.deployment()
        data = deployment.logs_client.get_query_results.return_value
        data["results"].append(copy.deepcopy(data["results"][0]))
        with patch("sebs.aws.aws.time.sleep"):
            deployment.download_metrics("function", 100, 101, {"missing": ExecutionResult()}, {})

    def test_failed_cloudwatch_query_is_not_reported_as_success(self):
        deployment = self.deployment()
        deployment.logs_client.get_query_results.return_value = {"status": "Failed", "results": []}
        with patch("sebs.aws.aws.time.sleep"), self.assertRaises(RuntimeError):
            deployment.download_metrics("function", 100, 101, {"missing": ExecutionResult()}, {})


if __name__ == "__main__":
    unittest.main()
