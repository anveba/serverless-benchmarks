"""Azure resource caches must survive interrupted first-time initialization."""
import unittest
from unittest.mock import Mock

from sebs.azure.config import AzureResources
from sebs.utils import LoggingHandlers


class AzureResourceCacheTests(unittest.TestCase):
    def test_unallocated_resources_round_trip(self):
        original = AzureResources()
        cached = original.serialize()
        self.assertNotIn('resource_group', cached)
        restored = AzureResources()
        AzureResources.initialize(restored, cached)
        self.assertEqual(restored.serialize(), cached)

    def test_partial_cache_can_continue_allocation(self):
        cache = Mock()
        cache.get_config.return_value = {'resources': {'storage_accounts': []}}
        resources = AzureResources.deserialize({}, cache, LoggingHandlers())
        resources.resources_id = 'fixture'
        resources.set_region('westeurope')
        cli = Mock()
        cli.execute.return_value = b'[]'
        self.assertEqual(resources.resource_group(cli), 'sebs_resource_group_fixture')
        self.assertTrue(any('az group create' in call.args[0] for call in cli.execute.call_args_list))

    def test_partial_cache_preserves_existing_storage(self):
        cached = {'resources_id': 'existing-id', 'storage_accounts': [
            {'account_name': 'fixture', 'connection_string': 'fake-connection-string'}]}
        resources = AzureResources()
        AzureResources.initialize(resources, cached)
        self.assertEqual(resources.resources_id, 'existing-id')
        self.assertEqual(resources.storage_accounts[0].account_name, 'fixture')
        self.assertNotIn('resource_group', resources.serialize())

    def test_allocated_resource_group_is_reused(self):
        cache = Mock()
        cache.get_config.return_value = {'resources': {'resource_group': 'existing-group', 'storage_accounts': []}}
        resources = AzureResources.deserialize({}, cache, LoggingHandlers())
        cli = Mock()
        self.assertEqual(resources.resource_group(cli), 'existing-group')
        cli.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
