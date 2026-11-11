"""Azure comparison shares scheduling/input without extra validation invocations."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from evaluation.sql.azure_prepare import make_trace, check_providers, eligible_vm_candidates
from evaluation.sql.azure_client import AzureFunctionsClient
from evaluation.sql.service import QueryService, manifest_fingerprint
from sebs.faas.function import ExecutionResult


INPUT = {'scale_factor': 1, 'access_mode': 'azure',
         'bucket': {'bucket': 'sebs-benchmarks-fixture'},
         'tables': {'lineitem': ['in/small/lineitem/data_0.parquet']}}


class AzureComparisonTests(unittest.TestCase):
    def test_provider_registration_only_when_requested_and_needed(self):
        az = Mock()
        az.side_effect = [
            {'registrationState': 'NotRegistered'}, None, {'registrationState': 'Registering'},
            {'registrationState': 'Registered'}, {'registrationState': 'Registering'},
            {'registrationState': 'Registered'}]
        states = check_providers(az, register=True)
        registrations = [call for call in az.call_args_list if call.args[0][:2] == ['provider', 'register']]
        self.assertEqual(len(registrations), 1)
        self.assertEqual(states['Microsoft.Compute'], 'Registering')
        az = Mock(return_value={'registrationState': 'NotRegistered'})
        check_providers(az)
        self.assertFalse(any(call.args[0][:2] == ['provider', 'register'] for call in az.call_args_list))

    def test_vm_filter_distinguishes_zone_from_region_restrictions(self):
        base = {'resourceType': 'virtualMachines', 'name': 'Standard_D2s_v5',
                'family': 'family', 'capabilities': [
                    {'name': 'vCPUs', 'value': '2'}, {'name': 'MemoryGB', 'value': '8'},
                    {'name': 'CpuArchitectureType', 'value': 'x64'}]}
        zone = {**base, 'restrictions': [{'type': 'Zone', 'restrictionInfo': {'locations': ['northeurope']}}]}
        region = {**base, 'restrictions': [{'type': 'Location', 'values': ['northeurope']}]}
        self.assertEqual(len(eligible_vm_candidates([zone], 'northeurope')), 1)
        self.assertEqual(eligible_vm_candidates([region], 'northeurope'), [])
        arm = copy.deepcopy(base)
        arm['capabilities'][-1]['value'] = 'Arm64'
        self.assertEqual(eligible_vm_candidates([arm], 'northeurope'), [])

    def test_trace_has_all_query_types_and_same_input_snapshot(self):
        source = copy.deepcopy(INPUT)
        trace = make_trace(source)
        self.assertEqual(len(trace['requests']), 30)
        self.assertEqual(trace['requests'][-1]['at'], 58)
        self.assertEqual(trace['settings']['max-in-flight'], 2)
        for query in ['q1', 'q3', 'q6', 'q12', 'q21']:
            self.assertEqual(sum(r['query'] == query for r in trace['requests']), 6)
        trace['input']['tables']['lineitem'].append('different')
        self.assertEqual(source, INPUT)
        with self.assertRaises(ValueError):
            make_trace(INPUT, 7)
        with self.assertRaises(ValueError):
            make_trace({**INPUT, 'scale_factor': .01})

    def test_prepare_uses_sebs_input_upload_without_invoking_function(self):
        from evaluation.sql.azure_prepare import prepare
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / 'config.json'
            config.write_text(json.dumps({'deployment': {'name': 'azure'}, 'experiments': {}}))
            with patch('evaluation.sql.azure_prepare.SeBS') as factory:
                client = factory.return_value.__enter__.return_value
                deployment = client.get_deployment.return_value
                deployment.config.region = 'northeurope'
                deployment.config.credentials.subscription_id = 'subscription'
                deployment.config.resources.resource_group.return_value = 'group'
                deployment.config.resources.data_storage_account.return_value.account_name = 'account'
                client.cache_client.get_all_functions.return_value = {'function': {
                    'benchmark': '710.sql-query', 'hash': 'hash',
                    'triggers': [{'type': 'HTTP', 'url': 'https://example.azurewebsites.net/api/handler'}]}}
                benchmark = client.get_benchmark.return_value
                benchmark.prepare_input.return_value = copy.deepcopy(INPUT)
                plan = prepare(config, path / 'cache', path / 'output', 30)
                self.assertEqual(plan['sql_invocations_submitted'], 0)
                benchmark.prepare_input.assert_called_once_with(deployment.system_resources, 'small', replace_existing=True)
                deployment.get_function.assert_not_called()
                deployment.create_function.assert_not_called()
                deployment.shutdown.assert_called_once()
                self.assertTrue((path / 'output/trace.json').exists())

    def test_deploy_in_fresh_region_uses_sebs_without_sql_invocations(self):
        from evaluation.sql.azure_prepare import prepare
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / 'config.json'
            config.write_text(json.dumps({'deployment': {'name': 'azure',
                'azure': {'region': 'swedencentral'}}, 'experiments': {}}))
            with patch('evaluation.sql.azure_prepare.SeBS') as factory:
                client = factory.return_value.__enter__.return_value
                deployment = client.get_deployment.return_value
                deployment.config.region = 'swedencentral'
                deployment.config.credentials.subscription_id = 'subscription'
                deployment.config.resources.resource_group.return_value = 'group'
                deployment.config.resources.data_storage_account.return_value.account_name = 'account'
                client.cache_client.get_all_functions.return_value = {}
                benchmark = client.get_benchmark.return_value
                benchmark.prepare_input.return_value = copy.deepcopy(INPUT)
                function = deployment.get_function.return_value
                function.name = 'new-function'
                function.serialize.return_value = {'hash': 'hash', 'triggers': [
                    {'type': 'HTTP', 'url': 'https://example.azurewebsites.net/api/handler'}]}
                plan = prepare(config, path / 'cache', path / 'output', 30, deploy_function=True)
                deployment.get_function.assert_called_once_with(benchmark)
                self.assertEqual(plan['region'], 'swedencentral')
                self.assertEqual(plan['sql_invocations_submitted'], 0)
                function.invoke.assert_not_called()
                deployment.shutdown.assert_called_once()

    def test_region_move_cannot_reuse_previous_region_state(self):
        from evaluation.sql.azure_prepare import prepare
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / 'config.json'
            config.write_text(json.dumps({'deployment': {'name': 'azure',
                'azure': {'region': 'swedencentral'}}}))
            output = path / 'output'
            output.mkdir()
            (output / 'trace.json').write_text(json.dumps(make_trace(INPUT)))
            (output / 'function.json').write_text(json.dumps({'region': 'northeurope'}))
            with patch('evaluation.sql.azure_prepare.SeBS') as factory:
                with self.assertRaisesRegex(ValueError, 'another region'):
                    prepare(config, path / 'cache', output, 30, deploy_function=True)
                factory.assert_not_called()

    def test_function_adapter_reuses_sebs_trigger_and_retains_cold_flag(self):
        state = dict(function_endpoint='https://example.azurewebsites.net/api/handler',
            function_name='fixture', region='northeurope', function_code_hash='hash',
            input_fingerprint=manifest_fingerprint(INPUT))
        client = AzureFunctionsClient(state, INPUT)
        client.trigger = Mock()
        result = ExecutionResult()
        result.request_id = 'invocation'
        result.stats.cold_start = True
        result.times.benchmark = 123
        result.output = {'result': {'result': {'queries': {'q6': [[42]]}}, 'measurement': {}},
                         'container_id': 'container', 'is_cold_worker': True}
        client.trigger.sync_invoke.return_value = result
        client.verify(client.health(), make_trace(INPUT))
        received = client.invoke('q6')
        self.assertEqual(client.trigger.sync_invoke.call_args.args[0]['queries'], ['q6'])
        self.assertNotIn('queries', INPUT)
        self.assertTrue(received.stats.cold_start)
        self.assertEqual(received.output['result']['queries']['q6'], [[42]])
        self.assertIsNone(received.output['provider_metrics']['billed_gb_seconds'])

    def test_persistent_service_uses_same_native_azure_transport(self):
        service = QueryService(INPUT)
        connection = Mock()
        with patch.dict('os.environ', {'STORAGE_CONNECTION_STRING': "AccountKey=abc'def"}):
            service._storage(connection)
        calls = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(calls[0], "SET azure_transport_option_type='curl'")
        self.assertIn("CONNECTION_STRING 'AccountKey=abc''def'", calls[1])
        self.assertEqual(service.health()['access_mode'], 'azure')
        self.assertEqual(service.health()['input_fingerprint'], manifest_fingerprint(INPUT))


if __name__ == '__main__':
    unittest.main()
