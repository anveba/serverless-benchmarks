"""Ensure VPC/environment configuration survives caches and applies to new/cached Lambdas."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sebs.aws.aws import AWS
from sebs.aws.config import AWSResources
from sebs.faas.system import System
from sebs.utils import LoggingBase, LoggingHandlers

NAME = '720.sql-postgresql'
OPTIONS = {'VpcConfig': {'SubnetIds': ['subnet-fixture'], 'SecurityGroupIds': ['sg-fixture']},
           'Environment': {'Variables': {'SEBS_POSTGRES_SECRET_ARN': 'arn:fixture'}}}


class FunctionOptionsTests(unittest.TestCase):
    def resources(self):
        resources = AWSResources()
        AWSResources.initialize(resources, {'function-options': {NAME: copy.deepcopy(OPTIONS)}})
        return resources

    def test_roundtrip_copy_and_unconfigured_benchmark(self):
        original = self.resources()
        restored = AWSResources()
        AWSResources.initialize(restored, original.serialize())
        self.assertEqual(restored.function_options(NAME), OPTIONS)
        altered = restored.function_options(NAME)
        altered['VpcConfig']['SubnetIds'].append('subnet-other')
        self.assertEqual(restored.function_options(NAME), OPTIONS)
        self.assertEqual(restored.function_options('710.sql-query'), {})

    def test_explicit_configuration_overrides_cached_endpoint_and_role(self):
        cache = Mock()
        cache.get_config.return_value = {'resources': self.resources().serialize()}
        entry = copy.deepcopy(OPTIONS)
        entry['Environment']['Variables']['SEBS_POSTGRES_SECRET_ARN'] = 'arn:new'
        result = AWSResources.deserialize({'resources': {'function-options': {NAME: entry},
            'lambda-role': 'arn:updated-role'}}, cache, LoggingHandlers())
        self.assertEqual(result.function_options(NAME), entry)
        self.assertEqual(result.serialize()['lambda-role'], 'arn:updated-role')

    def test_unsupported_options_rejected(self):
        for entry in [{'Role': 'admin'}, {'VpcConfig': {'SubnetIds': []}},
                      {'Environment': {'Variables': {'x': 123}}},
                      {'VpcConfig': {'SubnetIds': ['bad'], 'SecurityGroupIds': ['sg-valid']}}]:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                AWSResources.validate_function_options({NAME: entry})

    def deployment(self):
        deployment = AWS.__new__(AWS)
        LoggingBase.__init__(deployment)
        deployment._config = SimpleNamespace(resources=self.resources())
        deployment.client = Mock()
        deployment.wait_function_updated = Mock()
        deployment.logging_handlers = LoggingHandlers()
        return deployment

    def test_update_preserves_existing_environment_and_applies_private_network(self):
        deployment = self.deployment()
        deployment.client.get_function_configuration.return_value = {'Environment': {'Variables': {'existing': 'yes'}}}
        function = SimpleNamespace(name='fixture', config=SimpleNamespace(timeout=300, memory=512))
        benchmark = SimpleNamespace(has_input_processed=True, benchmark=NAME, uses_nosql=False)
        deployment.update_function_configuration(function, benchmark)
        sent = deployment.client.update_function_configuration.call_args.kwargs
        self.assertEqual(sent['VpcConfig'], OPTIONS['VpcConfig'])
        self.assertEqual(sent['Environment']['Variables'], {**OPTIONS['Environment']['Variables'], 'existing': 'yes'})

    def test_cached_function_detects_endpoint_and_subnet_changes(self):
        deployment = self.deployment()
        deployment.client.get_function_configuration.return_value = {}
        with patch.object(System, 'is_configuration_changed', return_value=False):
            self.assertTrue(deployment.is_configuration_changed(SimpleNamespace(name='fixture'), SimpleNamespace(benchmark=NAME)))
            deployment.client.get_function_configuration.return_value = copy.deepcopy(OPTIONS)
            self.assertFalse(deployment.is_configuration_changed(SimpleNamespace(name='fixture'), SimpleNamespace(benchmark=NAME)))
            self.assertFalse(deployment.is_configuration_changed(SimpleNamespace(name='fixture'), SimpleNamespace(benchmark='710.sql-query')))


if __name__ == '__main__':
    unittest.main()
