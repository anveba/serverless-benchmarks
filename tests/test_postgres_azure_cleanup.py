import importlib.util
from pathlib import Path
import unittest

try:
    from evaluation.sql import postgres_azure_cleanup as study
except ImportError:
    spec = importlib.util.spec_from_file_location('cleanup', Path(__file__).with_name('postgres_azure_cleanup.py'))
    study = importlib.util.module_from_spec(spec); spec.loader.exec_module(study)


class CleanupTest(unittest.TestCase):
    def test_parent_absence_and_exact_support_lookup_preserve_other_resources(self):
        expected = [{'id': '/subscriptions/sub/resourceGroups/sql/providers/Microsoft.Compute/virtualMachines/db', 'group': 'sql'},
                    {'id': '/subscriptions/sub/resourceGroups/functions/providers/Microsoft.Web/sites/app', 'group': 'functions'}]
        calls = []
        class CLI:
            def run(self, args, **kwargs):
                calls.append(args)
                if args[:2] == ['group', 'show']:
                    return None if args[-1] == 'sql' else {'id': 'functions'}
                if args[:2] == ['resource', 'list']:
                    return [{'id': '/subscriptions/sub/resourceGroups/functions/providers/Microsoft.Storage/storageAccounts/retained',
                             'name': 'retained', 'type': 'Microsoft.Storage/storageAccounts'}]
                if args[:2] == ['resource', 'show']: return None
                raise AssertionError(args)
        report = study.verify(CLI(), expected)
        self.assertTrue(report['all_checked_experiment_resources_removed'])
        self.assertEqual(len(report['other_retained_group_resources']), 1)
        self.assertEqual(len(calls), 4)
        self.assertFalse(any('delete' in c or 'create' in c for c in calls))

    def test_permission_failure_and_live_resource_never_mark_complete(self):
        expected = [{'id': 'id', 'group': 'group'}]
        class CLI:
            def run(self, args, **kwargs): raise RuntimeError('AuthorizationFailed')
        result = study.verify(CLI(), expected)
        self.assertFalse(result['all_checked_experiment_resources_removed'])
        self.assertIsNone(result['resources'][0]['absent'])
        class Present:
            def run(self, args, **kwargs): return [] if args[:2] == ['resource', 'list'] else {'id': 'id'}
        self.assertFalse(study.verify(Present(), expected)['all_checked_experiment_resources_removed'])

    def test_target_inventory_deduplicates_and_rejects_other_subscription(self):
        app = '/subscriptions/sub/resourceGroups/functions/providers/Microsoft.Web/sites/app'
        vm = '/subscriptions/sub/resourceGroups/sql/providers/Microsoft.Compute/virtualMachines/db'
        plan = {'subscription': 'sub', 'function_app_id': app, 'server_vm_id': vm, 'driver_vm_id': vm + '-driver'}
        result = study.targets(plan, {'app': {'app_id': app.upper(), 'resource_ids': []}}, [{'id': vm}])
        self.assertEqual(len(result), 3)
        with self.assertRaises(ValueError):
            study.targets(plan, {}, [{'id': vm.replace('/sub/', '/other/')}])


if __name__ == '__main__': unittest.main()
