"""Native meter parsing preserves unknown values and workspace counter boundaries."""
import unittest
from evaluation.sql.azure_metrics import metric_total, metric_window, synapse_delta


class AzureMetricsTests(unittest.TestCase):
    def test_missing_points_are_unknown_but_explicit_zero_is_zero(self):
        name = 'FunctionExecutionUnits'
        self.assertIsNone(metric_total({}, name))
        data = {'value': [{'name': {'value': name}, 'timeseries': [{'data': [{'total': None}]}]}]}
        self.assertIsNone(metric_total(data, name))
        data['value'][0]['timeseries'][0]['data'] = [{'total': 0}]
        self.assertEqual(metric_total(data, name), 0)
        data['value'][0]['timeseries'].append({'data': [{'total': 1024000}]})
        self.assertEqual(metric_total(data, name), 1024000)

    def test_metric_window_covers_entire_observation_in_minute_buckets(self):
        start, end = metric_window({'observation_begin_unix': 61.2, 'observation_end_unix': 120.3}, padding_minutes=0)
        self.assertEqual(start, '1970-01-01T00:01:00Z')
        self.assertEqual(end, '1970-01-01T00:03:00Z')

    def test_reporting_margin_covers_delayed_native_meter_points(self):
        start, end = metric_window({'observation_begin_unix': 61.2, 'observation_end_unix': 120.3})
        self.assertEqual(start, '1969-12-31T23:56:00Z')
        self.assertEqual(end, '1970-01-01T00:08:00Z')
        with self.assertRaises(ValueError):
            metric_window({}, padding_minutes=-1)

    def test_synapse_period_reset_is_not_negative_usage(self):
        data = {'before': {'rows': [{'type': 'daily', 'data_processed_mb': '20'},
                                    {'type': 'monthly', 'data_processed_mb': '40'}]},
                'after': {'rows': [{'type': 'daily', 'data_processed_mb': '10'},
                                   {'type': 'monthly', 'data_processed_mb': '50'}]}}
        self.assertEqual(synapse_delta(data), {'monthly': 10})
        self.assertEqual(synapse_delta({'before': {'rows': None}, 'after': {'rows': None}}), {})


if __name__ == '__main__':
    unittest.main()
