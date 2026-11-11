"""Cost formulas use paid units, observation windows and native meter uncertainty."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.sql.azure_costs import calculate, select_rates


def price_records():
    common = dict(currencyCode='USD', armRegionName='swedencentral', type='Consumption',
                  effectiveStartDate='2023-01-01T00:00:00Z')
    vm = {**common, 'armSkuName': 'Standard_D2s_v5', 'skuName': 'Standard_D2s_v5',
          'productName': 'Virtual Machines Dsv5 Series', 'retailPrice': .102, 'unitOfMeasure': '1 Hour'}
    time = {**common, 'productName': 'Functions', 'meterName': 'Standard Execution Time',
            'retailPrice': .000016, 'unitOfMeasure': '1 GB Second'}
    request = {**common, 'productName': 'Functions', 'meterName': 'Standard Total Executions',
               'retailPrice': .000002, 'unitOfMeasure': '10'}
    synapse = {**common, 'productName': 'Azure Synapse Analytics Serverless SQL Pool',
               'meterName': 'Standard Data Processed', 'retailPrice': 5, 'unitOfMeasure': '1 TB'}
    return {'vm': {'response': {'Items': [vm, {**vm, 'productName': vm['productName']+' Windows', 'retailPrice': .194},
        {**vm, 'skuName': 'Standard_D2s_v5 Spot', 'retailPrice': .01}]}},
        'functions': {'response': {'Items': [time, {**time, 'retailPrice': 0}, request, {**request, 'retailPrice': 0}]}},
        'synapse': {'response': {'Items': [synapse]}}}


class AzureCostsTests(unittest.TestCase):
    def test_selects_linux_paid_consumption_rates_and_request_unit(self):
        rates = select_rates(price_records(), 'swedencentral', 'Standard_D2s_v5')
        self.assertEqual(rates['vm_usd_per_hour'], .102)
        self.assertEqual(rates['functions_usd_per_gb_second'], .000016)
        self.assertAlmostEqual(rates['functions_usd_per_request'], .0000002)
        self.assertEqual(rates['synapse_usd_per_tb'], 5)

    def test_unexpected_price_unit_is_rejected(self):
        records = price_records()
        records['functions']['response']['Items'][2]['unitOfMeasure'] = '1M'
        with self.assertRaisesRegex(ValueError, 'unit'):
            select_rates(records, 'swedencentral', 'Standard_D2s_v5')

    def fixture(self, root, gb_seconds=100):
        (root/'command.json').write_text(json.dumps({'settings': {'backends': ['vm', 'functions', 'synapse']}}))
        for name in ['vm', 'functions', 'synapse']:
            directory = root/name
            directory.mkdir()
            (directory/'summary.json').write_text(json.dumps({'submitted': 100, 'valid': 100,
                'execution_failures': 0, 'observation_seconds': 100}))
        (root/'synapse/data-processed.json').write_text(json.dumps({
            'before': {'rows': [{'type': p, 'data_processed_mb': '10'} for p in ['daily', 'weekly', 'monthly']]},
            'after': {'rows': [{'type': p, 'data_processed_mb': '20'} for p in ['daily', 'weekly', 'monthly']]}}))
        (root/'azure-meters').mkdir()
        (root/'azure-meters/usage-summary.json').write_text(json.dumps({'function_gb_seconds': gb_seconds,
                                                                      'function_execution_count': 99}))

    def test_cost_uses_idle_window_and_one_synapse_period_and_marks_partial_function_meter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            report = calculate(root, select_rates(price_records(), 'swedencentral', 'Standard_D2s_v5'))
        self.assertAlmostEqual(report['totals']['vm']['cost_usd'], 100*.102/3600)
        self.assertEqual(report['totals']['synapse']['data_processed_mb'], 10)
        self.assertAlmostEqual(report['totals']['synapse']['cost_usd'], 10/1000000*5)
        fn = report['totals']['functions']
        self.assertFalse(fn['meter_count_matches_replay'])
        self.assertIn('provisional', fn['status'])
        self.assertAlmostEqual(fn['cost_usd'], 100*.000016+100*.0000002)
        self.assertIsNone(report['workloads']['initial']['functions']['cost_usd'])
        self.assertAlmostEqual(report['overhead']['shared_driver_matched_window_compute_usd'], 300*.102/3600)

    def test_missing_native_usage_stays_unknown_not_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, gb_seconds=None)
            report = calculate(root, select_rates(price_records(), 'swedencentral', 'Standard_D2s_v5'))
        self.assertIsNone(report['totals']['functions']['cost_usd'])
        self.assertIsNone(report['totals']['functions']['cost_per_100_queries_usd'])

    def test_unequal_backend_query_counts_are_not_compared_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            path = root/'vm/summary.json'
            summary = json.loads(path.read_text())
            summary.update(submitted=90, valid=90)
            path.write_text(json.dumps(summary))
            with self.assertRaisesRegex(ValueError, 'totals differ'):
                calculate(root, select_rates(price_records(), 'swedencentral', 'Standard_D2s_v5'))


if __name__ == '__main__':
    unittest.main()
