import importlib.util
import io
from pathlib import Path
from contextlib import redirect_stdout
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

try:
    from evaluation.sql import postgres_azure_costs as study
except ImportError:
    spec = importlib.util.spec_from_file_location('study', Path(__file__).with_name('postgres_azure_costs.py'))
    study = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(study)


class CostsTest(unittest.TestCase):
    def price(self, name, unit, value, **changes):
        return {'armRegionName': 'swedencentral', 'currencyCode': 'USD', 'type': 'Consumption',
                'tierMinimumUnits': 0, 'effectiveStartDate': '2026-01-01T00:00:00Z',
                'productName': name, 'skuName': name, 'meterName': name,
                'unitOfMeasure': unit, 'retailPrice': value, **changes}

    def fixture(self, root):
        phases = [{'run_id': 'burst-100-2', 'repetition': 'repeat-1', 'backend': backend,
            'metric_start': '2026-11-24T12:00:00Z', 'metric_end': '2026-11-24T12:02:00Z',
            'summary': {'submitted': 100, 'observation_seconds': 100}}
            for backend in ('postgresql', 'functions')]
        study.save(root / 'study.json', {'plan': {'region': 'swedencentral', 'function_memory_mb': 512}, 'phases': phases, 'metric_windows': study.function_windows(phases)})
        for role in ('server', 'driver'):
            study.save(root / (role + '-vm.json'), {'hardwareProfile': {'vmSize': 'Standard_D2s_v5'}})
        study.save(root / 'prices/Standard_D2s_v5.json', {'pages': [{'Items': [
            self.price('Standard_D2s_v5', '1 Hour', .36)]}]})
        study.save(root / 'prices/functions.json', {'pages': [{'Items': [
            self.price('Flex Consumption On Demand', '1 GB Second', .00002),
            self.price('Flex Consumption On Demand', '10', .000004),
            self.price('Functions Standard', '1 GB Second', .000016)]}]})
        study.save(root / 'function-app.json', {'functionAppConfig': {
            'scaleAndConcurrency': {'instanceMemoryMB': 512, 'alwaysReady': []}}})
        for metric, value in zip(study.METRICS, [100, 102400000, None, None, None]):
            study.save(root / 'burst-100-2/window-1' / (metric + '.json'), {'total': value})

    def test_native_flex_conversion_shared_database_and_driver_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.fixture(root)
            result = study.calculate(root)
            direct, functions = [result['workloads']['burst-100-2'][b] for b in ('postgresql', 'functions')]
            self.assertAlmostEqual(direct['compute_usd_per_100'], .01)
            self.assertAlmostEqual(functions['function_gb_seconds'], 100)
            self.assertAlmostEqual(functions['compute_usd_per_100'], .01204)
            self.assertAlmostEqual(functions['driver_compute_usd_separate'], .01)
            self.assertEqual(result['price_errors'], {})
            self.assertIn('OS/data disks and transactions', result['excluded'])

    def test_mismatch_missing_and_always_ready_never_fabricate_cost(self):
        for metric, value in [('OnDemandFunctionExecutionCount', 99),
                              ('OnDemandFunctionExecutionUnits', None),
                              ('AlwaysReadyUnits', 100)]:
            with self.subTest(metric=metric), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); self.fixture(root)
                study.save(root / 'burst-100-2/window-1' / (metric + '.json'), {'total': value})
                report = study.calculate(root)
                fn = report['workloads']['burst-100-2']['functions']
                self.assertIsNone(fn['compute_usd_per_100'])
                self.assertTrue(report['missing_meters'])
                self.assertAlmostEqual(report['workloads']['burst-100-2']['postgresql']['compute_usd_per_100'], .01)

    def test_current_always_ready_or_unknown_configuration_blocks_flex_price(self):
        for config in [{}, {'functionAppConfig': {'scaleAndConcurrency': {
            'instanceMemoryMB': 512, 'alwaysReady': [{'instanceCount': 1}]}}}]:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp); self.fixture(root); study.save(root / 'function-app.json', config)
                self.assertIsNone(study.calculate(root)['workloads']['burst-100-2']['functions']['compute_usd_per_100'])

    def test_only_native_named_metric_valid_totals(self):
        response = {'value': [{'name': {'value': 'OnDemandFunctionExecutionCount'}, 'timeseries': [
            {'data': [{'total': None}, {'total': 20}]}, {'data': [{'total': 80}]}]}]}
        self.assertEqual(study.metric_total(response, 'OnDemandFunctionExecutionCount'), 100)
        self.assertIsNone(study.metric_total(response, 'FunctionExecutionCount'))
        response['value'][0]['timeseries'][0]['data'][1]['total'] = -1
        self.assertIsNone(study.metric_total(response, 'OnDemandFunctionExecutionCount'))
        self.assertIsNone(study.metric_total({'value': []}, 'OnDemandFunctionExecutionCount'))

    def test_price_selection_region_date_currency_unit_ambiguity(self):
        good = self.price('VM', '1 Hour', .1)
        rows = [good, {**good, 'armRegionName': 'eastus', 'retailPrice': .01},
                {**good, 'effectiveStartDate': '2027-01-01T00:00:00Z', 'retailPrice': 3},
                {**good, 'currencyCode': 'EUR', 'retailPrice': 4}]
        select = lambda candidates: study.select_rate(candidates, 'swedencentral',
            '2026-11-01T00:00:00Z', lambda r: True, {'1 Hour': 1})
        self.assertEqual(select(rows)['usd_per_unit'], .1)
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            select([good, {**good, 'retailPrice': .2}])
        with self.assertRaises(ValueError):
            select([{**good, 'unitOfMeasure': '100 Hours'}])

    def test_legacy_rate_is_not_a_flex_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.fixture(root)
            study.save(root / 'prices/functions.json', {'pages': [{'Items': [
                self.price('Functions Standard', '1 GB Second', .000016)]}]})
            result = study.calculate(root)
            self.assertIn('function_time', result['price_errors'])
            self.assertIsNone(result['workloads']['burst-100-2']['functions']['compute_usd_per_100'])

    def test_paid_flex_tiers_after_free_grant_are_selected_not_free_or_volume_rates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.fixture(root)
            items = []
            for unit, grant, rate in [('1 GB Second', 100000, .000026), ('10', 25000, .000004)]:
                items += [self.price('Flex Consumption On Demand', unit, 0),
                          self.price('Flex Consumption On Demand', unit, rate, tierMinimumUnits=grant),
                          self.price('Flex Consumption On Demand', unit, rate / 2, tierMinimumUnits=10000000)]
            study.save(root / 'prices/functions.json', {'pages': [{'Items': items}]})
            result = study.calculate(root)
            self.assertEqual(result['price_errors'], {})
            self.assertEqual(result['rates']['function_time']['usd_per_unit'], .000026)
            self.assertAlmostEqual(result['rates']['function_requests']['usd_per_unit'], .0000004)
            self.assertAlmostEqual(result['workloads']['burst-100-2']['functions']['compute_usd_per_100'], .01264)

    def test_padding_merges_repetitions_once_and_rejects_other_workload_overlap(self):
        def phase(run_id, rep, begin, end):
            return {'run_id': run_id, 'repetition': rep, 'backend': 'functions', 'summary': {'submitted': 100},
                    'metric_start': '2026-11-24T' + begin + ':00Z', 'metric_end': '2026-11-24T' + end + ':00Z'}
        phases = [phase('burst-100-2', 'repeat-1', '16:34', '16:37'),
                  phase('burst-100-2', 'repeat-2', '16:37', '16:40'),
                  phase('burst-100-2', 'repeat-3', '16:45', '16:49'),
                  phase('burst-100-8', 'repeat-1', '16:59', '17:02')]
        windows = study.function_windows(phases, 5)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]['expected_requests'], 300)
        self.assertEqual(windows[0]['metric_start'], '2026-11-24T16:29:00Z')
        self.assertEqual(windows[0]['metric_end'], windows[1]['metric_start'])
        with self.assertRaisesRegex(ValueError, 'overlap'):
            study.function_windows(phases, 6)

    def test_saved_phases_require_complete_matching_resources_and_disjoint_buckets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = {'function_app_id': 'app', 'server_vm_id': 'server', 'driver_vm_id': 'driver',
                    'input_fingerprint': 'fp'}
            run = root / 'runs/burst-100-2'
            study.save(run / 'settings.json', {'plan': plan, 'repetitions': 2,
                                               'trace': {'requests': [1] * 100}})
            study.save(run / 'collected/status.json', {'state': 'complete', 'completed_runs': 4})
            for n, backend in [(n, b) for n in (1, 2) for b in ('postgresql', 'functions')]:
                start = 180 + n * 180 + (0 if backend == 'postgresql' else 60)
                summary = {'submitted': 100, 'valid': 100, 'completed_records': 100,
                           'execution_failures': 0, 'observation_begin_unix': start,
                           'observation_end_unix': start + 50, 'observation_seconds': 50}
                study.save(run / f'collected/repeat-{n}/{backend}/summary.json', summary)
            self.assertEqual(len(study.phases(root, ['burst-100-2'])[0]), 4)
            path = run / 'collected/repeat-2/functions/summary.json'
            broken = study.read(path); broken.update(observation_begin_unix=420, observation_end_unix=470)
            study.save(path, broken)
            merged, _ = study.phases(root, ['burst-100-2'])
            self.assertEqual(len(study.function_windows(merged)), 1)
            self.assertEqual(study.function_windows(merged)[0]['expected_requests'], 200)
            broken['valid'] = 99; study.save(path, broken)
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                study.phases(root, ['burst-100-2'])

    def test_collector_only_reads_cloud_and_preserves_raw_evidence(self):
        calls = []
        class CLI:
            def __init__(self, subscription): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def run(self, args):
                calls.append(args)
                if args[:3] == ['monitor', 'metrics', 'list']:
                    metric = args[args.index('--metric') + 1]
                    return {'value': [{'name': {'value': metric}, 'timeseries': [
                        {'data': [{'total': 100 if metric.endswith('Count') else 1024000}]}]}]}
                if args[:2] == ['vm', 'show']:
                    return {'hardwareProfile': {'vmSize': 'Standard_D2s_v5'},
                            'storageProfile': {'osDisk': {'managedDisk': {'id': 'disk'}}}}
                if args[:2] == ['disk', 'show']: return {'diskSizeGb': 64}
                if args[:3] == ['rest', '--method', 'get']:
                    return {'properties': {'functionAppConfig': {'scaleAndConcurrency': {
                        'instanceMemoryMB': 512, 'alwaysReady': []}, 'deployment': {'secret': 'not-persisted'}}}}
                if args[:2] == ['resource', 'list']: return []
                raise AssertionError('Unexpected cloud operation: ' + repr(args))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            phase = {'run_id': 'run', 'repetition': 'repeat-1', 'backend': 'functions',
                     'metric_start': '2026-11-24T00:00:00Z', 'metric_end': '2026-11-24T00:03:00Z', 'summary': {'submitted': 100}}
            plan = dict(subscription='sub', region='swedencentral', function_app_id='app',
                        server_vm_id='server', driver_vm_id='driver', function_memory_mb=512,
                        input_fingerprint='fp', sql_resource_group='group')
            def prices(directory, key, query):
                study.save(directory / (key + '.json'), {'pages': [{'Items': []}]})
            with patch.dict('sys.modules', {'sebs.azure.control': SimpleNamespace(AzureControl=CLI)}), \
                 patch.object(study, 'phases', return_value=([phase], plan)), \
                 patch.object(study, 'fetch_prices', side_effect=prices), redirect_stdout(io.StringIO()):
                out = study.collect(folder, ['run'])
            self.assertTrue((out / 'collection-complete.json').exists())
            self.assertEqual(study.read(out / 'run/window-1/OnDemandFunctionExecutionCount.json')['total'], 100)
            self.assertNotIn('not-persisted', (out / 'function-app.json').read_text())
            self.assertEqual(len(calls), 11)


if __name__ == '__main__':
    unittest.main()
