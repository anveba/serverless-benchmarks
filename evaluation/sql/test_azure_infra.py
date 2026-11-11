"""Offline checks for scoped Azure provisioning, private access and secret handling."""
import json
import io
from contextlib import redirect_stdout
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evaluation.sql.azure_control import AzureControl
from evaluation.sql.azure_infra import bootstrap, initial_plan, vm_args, write, can_submit, submit_request, status


class AzureInfraTests(unittest.TestCase):
    def plan(self):
        return initial_plan(dict(function_name='sebs-fixture-710-sql-query-python-3-11',
            resource_group='original-sebs-group', subscription_id='subscription', region='northeurope'))

    def test_status_handles_null_security_profile_and_checks_both_bootstraps(self):
        for profile, expected in [(None, 'Standard'), ({}, 'Standard'),
                                  ({'securityType': None}, 'Standard'),
                                  ({'securityType': 'TrustedLaunch'}, 'TrustedLaunch')]:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                write(folder / 'infra.json', self.plan())
                vm = {'provisioningState': 'Succeeded', 'powerState': 'VM running',
                      'securityProfile': profile}
                bootstrap_result = {'value': [{'message': 'BOOTSTRAP_READY'}]}
                with patch('evaluation.sql.azure_infra.AzureControl') as factory:
                    cli = factory.return_value.__enter__.return_value
                    cli.run.side_effect = [
                        {'provisioningState': 'Provisioning'}, vm, bootstrap_result,
                        vm, bootstrap_result]
                    output = io.StringIO()
                    with redirect_stdout(output):
                        status(folder)
                    self.assertEqual(output.getvalue().count('BOOTSTRAP_READY'), 2)
                    self.assertEqual(output.getvalue().count('security=' + expected), 2)
                    self.assertEqual(cli.run.call_count, 5)

    def test_ordinary_vm_omits_confidential_image_security_flags(self):
        plan = self.plan()
        plan.update(vm_size='Standard_D2s_v5', vm_security_type='Standard')
        args = vm_args(plan, 'server', 'ordinary-image', '/tmp/bootstrap.sh', '/tmp/key.pub')
        self.assertIn('Standard_D2s_v5', args)
        for flag in ['ConfidentialVM', '--os-disk-security-encryption-type', '--enable-vtpm']:
            self.assertNotIn(flag, args)

    def test_rejected_submission_can_retry_without_discarding_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'request.json'
            rejected = Mock(side_effect=RuntimeError('ValidationFailed CustomerSubscriptionNotRegisteredWithSqlRp'))
            with self.assertRaises(RuntimeError):
                submit_request(path, {'workspace': 'same-workspace'}, rejected)
            self.assertTrue(can_submit(path))
            self.assertEqual(json.loads(path.read_text())['status'], 'rejected')
            accepted = Mock()
            submit_request(path, {'workspace': 'same-workspace'}, accepted)
            self.assertFalse(can_submit(path))
            self.assertEqual(json.loads(path.read_text())['status'], 'submitted')
            with self.assertRaisesRegex(RuntimeError, 'already recorded'):
                submit_request(path, {'workspace': 'same-workspace'}, accepted)
            accepted.assert_called_once()

    def test_transport_error_preserves_unknown_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'request.json'
            with self.assertRaises(RuntimeError):
                submit_request(path, {'workspace': 'same'}, Mock(side_effect=RuntimeError('connection timed out')))
            self.assertFalse(can_submit(path))
            self.assertEqual(json.loads(path.read_text())['status'], 'requested')

    def test_quota_sized_confidential_vms_do_not_open_ssh(self):
        plan = self.plan()
        self.assertNotEqual(plan['resource_group'], plan['function_resource_group'])
        self.assertEqual(plan['vm_size'], 'Standard_EC2as_v5')
        for role in ['server', 'driver']:
            args = vm_args(plan, role, 'pinned-image', '/tmp/bootstrap', '/tmp/public-key')
            self.assertEqual(args[args.index('--security-type') + 1], 'ConfidentialVM')
            self.assertEqual(args[args.index('--nsg-rule') + 1], 'NONE')
            self.assertEqual(args[args.index('--private-ip-address') + 1], plan[role + '_private_ip'])
            self.assertEqual(args[args.index('--os-disk-delete-option') + 1], 'Delete')
            self.assertNotIn('--generate-ssh-keys', args)
            self.assertIn('--no-wait', args)
        self.assertNotEqual(plan['server_private_ip'], plan['driver_private_ip'])

    def test_bootstrap_parses_and_contains_no_credentials_or_sql(self):
        result = subprocess.run(['bash', '-n'], input=bootstrap(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('bootstrap.complete', bootstrap())
        self.assertNotIn('AZURE_SECRET_PASSWORD', bootstrap())
        self.assertNotIn('SELECT', bootstrap())

    def test_private_state_is_mode_600(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private.json'
            write(path, {'password': 'fixture'}, private=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {'password': 'fixture'})

    def test_cli_errors_redact_secrets_and_do_not_echo_argv(self):
        control = AzureControl.__new__(AzureControl)
        control.cli, control.secrets = Mock(), ['private-value']
        control.cli.docker_instance.exec_run.return_value = (1, (b'private stdout', b'bad private-value'))
        with self.assertRaisesRegex(RuntimeError, r'bad \[REDACTED\]') as error:
            control.run(['command', '--password', 'private-value'])
        self.assertNotIn('private stdout', str(error.exception))
        self.assertNotIn('--password', str(error.exception))


if __name__ == '__main__':
    unittest.main()
