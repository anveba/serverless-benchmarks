"""A resource-group dry run must never call the deletion API."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from click.testing import CliRunner

from sebs.cli import cli


class ResourceGroupRemovalTests(unittest.TestCase):
    def invoke(self, dry_run):
        deployment, client = Mock(), Mock()
        deployment.name.return_value = 'azure'
        resources = deployment.config.resources
        resources.list_resource_groups.return_value = ['sebs_resource_group_fixture', 'sebs_resource_group_other']
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'config.json'
            config.write_text('{}')
            args = ['resources','remove','resource-groups','sebs_resource_group_fixture',
                    '--config',str(config),'--deployment','azure']
            if dry_run:
                args.append('--dry-run')
            with patch('sebs.cli.parse_common_params', return_value=({}, directory, 'out.log', client, deployment)):
                result = CliRunner().invoke(cli,args)
        self.assertEqual(result.exit_code,0,result.output)
        return deployment

    def test_dry_run_lists_matching_group_without_deleting(self):
        deployment = self.invoke(True)
        deployment.config.resources.list_resource_groups.assert_called_once()
        deployment.config.resources.delete_resource_group.assert_not_called()

    def test_actual_removal_deletes_only_matching_group(self):
        deployment = self.invoke(False)
        deployment.config.resources.delete_resource_group.assert_called_once_with(
            deployment.cli_instance, 'sebs_resource_group_fixture', True)


if __name__ == '__main__':
    unittest.main()
