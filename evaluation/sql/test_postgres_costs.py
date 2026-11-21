"""Offline checks for cost evidence ownership, redaction and pricing gaps."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evaluation.sql.postgres_costs import collect, collect_prices, instance_inventory, price_queries, stack_inventory


class CostEvidenceTests(unittest.TestCase):
    def test_instance_requires_stack_membership_before_describe(self):
        ec2 = Mock()
        with self.assertRaisesRegex(ValueError, 'member'):
            instance_inventory(ec2, 'i-unowned', {'resources': []})
        ec2.describe_instances.assert_not_called()

    def test_stack_history_excludes_parameters_properties_and_reasons(self):
        cf = Mock()
        cf.describe_stacks.return_value = {'Stacks': [{'StackId': 's', 'Parameters': ['sensitive']}]}
        cf.get_paginator.return_value.paginate.side_effect = [
            [{'StackResourceSummaries': [{'LogicalResourceId': 'r', 'ResourceStatusReason': 'sensitive'}]}],
            [{'StackEvents': [{'EventId': 'e', 'ResourceProperties': 'sensitive'}]}]]
        report = stack_inventory(cf, 's')
        self.assertNotIn('sensitive', json.dumps(report))
        self.assertEqual(report['events'], [{'EventId': 'e'}])

    def test_prices_preserve_products_cache_queries_and_flag_empty_results(self):
        client = Mock()
        client.get_paginator.return_value.paginate.side_effect = [
            [{'PriceList': [json.dumps({'product': {'sku': 'sku'}, 'terms': {'OnDemand': {}}})]}],
            [{'PriceList': []}]]
        queries = {'good': ('service', {'regionCode': 'us-east-1'}), 'missing': ('other', {})}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            status = collect_prices(client, queries, folder)
            self.assertFalse(status['complete'])
            self.assertEqual(status['product_counts'], {'good': 1, 'missing': 0})
            self.assertIn('missing', status['errors'])
            self.assertEqual(json.loads((folder/'good.json').read_text())['products'][0]['product']['sku'], 'sku')
            collect_prices(client, queries, folder)
            self.assertEqual(client.get_paginator.return_value.paginate.call_count, 2)

    def test_study_region_is_explicit_and_compute_selection_is_linux_shared(self):
        with self.assertRaisesRegex(ValueError, 'us-east-1'):
            price_queries('eu-west-1', {'m7i-flex.large'})
        queries = price_queries('us-east-1', {'m7i-flex.large'})
        self.assertEqual(queries['compute-m7i-flex.large'][1]['tenancy'], 'Shared')
        self.assertEqual(queries['compute-m7i-flex.large'][1]['operatingSystem'], 'Linux')

    def test_collector_uses_describe_secret_not_secret_value_and_omits_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder/'instance.json').write_text(json.dumps({'region':'us-east-1', 'stack_id':'arn:a:b:c:account:driver', 'instance_id':'driver'}))
            (folder/'preflight.json').write_text(json.dumps({'region':'us-east-1', 'lambda_function':'function'}))
            session = Mock()
            ec2, secret, function, pricing = Mock(), Mock(), Mock(), Mock()
            session.client.side_effect = lambda name, **kwargs: {'ec2':ec2, 'cloudformation':Mock(),
                'secretsmanager':secret, 'lambda':function, 'pricing':pricing}[name]
            secret.describe_secret.return_value = {'Name':'secret', 'Description':'sensitive'}
            function.get_function_configuration.return_value = {'FunctionName':'function', 'Environment':{'Variables':{'PASSWORD':'sensitive'}}}
            ec2.describe_vpc_endpoints.return_value = {'VpcEndpoints':[{'VpcEndpointId':'vpce'}]}
            outputs = {'InstanceId':'server', 'EndpointId':'vpce', 'SecretArn':'secret'}
            owned_instance = {'instance':{'InstanceType':'m7i-flex.large', 'State':{'Name':'running'}}, 'volumes':[{'Size':64}]}
            with patch('evaluation.sql.postgres_costs.sql_config', return_value=SimpleNamespace(provider='aws', region='us-east-1')), \
                 patch('evaluation.sql.postgres_costs.AWSPostgreSQL') as backend, \
                 patch('evaluation.sql.postgres_costs.stack_inventory', return_value={'resources':[]}), \
                 patch('evaluation.sql.postgres_costs.instance_inventory', return_value=owned_instance), \
                 patch('evaluation.sql.postgres_costs.collect_prices', return_value={'complete':True, 'product_counts':{}, 'errors':{}}), \
                 redirect_stdout(io.StringIO()):
                backend.return_value.info.return_value = ({}, {'StackId':'arn:a:b:c:account:database'}, outputs)
                attachment = folder/'attachment.json'
                attachment.write_text('{"sql":{}}')
                report, _ = collect(attachment, folder, folder/'out', session)
            secret.describe_secret.assert_called_once_with(SecretId='secret')
            secret.get_secret_value.assert_not_called()
            self.assertNotIn('sensitive', json.dumps(report))
            self.assertTrue((folder/'out/latest.json').exists())


if __name__ == '__main__':
    unittest.main()
