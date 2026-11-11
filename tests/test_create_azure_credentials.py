"""Credential helper waits for login completion and never continues after errors."""
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, patch

from tools import create_azure_credentials as helper


class AzureCredentialHelperTests(unittest.TestCase):
    def container(self, exit_code=0):
        container = Mock()
        api = container.client.api
        api.exec_create.return_value = {'Id': 'exec-1'}
        api.exec_start.return_value = iter([
            b'To sign in, use a web browser ', b'and enter a device code.\n',
            b'UserWarning: Attempted too soon.\n', b'Login finished.\n'])
        api.exec_inspect.return_value = {'Running': False, 'ExitCode': exit_code}
        return container

    def test_warning_chunks_do_not_interrupt_login(self):
        container = self.container()
        output = io.StringIO()
        with redirect_stdout(output):
            helper.login(container, 'tenant-id')
        self.assertIn('Attempted too soon', output.getvalue())
        container.client.api.exec_inspect.assert_called_once_with('exec-1')
        self.assertIn('--use-device-code', container.client.api.exec_create.call_args.args[1])
        self.assertNotIn('--only-show-errors', container.client.api.exec_create.call_args.args[1])

    def test_login_failure_never_creates_principal(self):
        container = self.container(exit_code=1)
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'login failed'):
            helper.create_principal(container, 'sebs-sql', 'subscription')
        container.exec_run.assert_not_called()

    def test_unavailable_subscription_never_creates_principal(self):
        container = self.container()
        container.exec_run.return_value = (0, (b'[{"id":"other","isDefault":true}]', None))
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'not available'):
            helper.create_principal(container, 'sebs-sql', 'missing')
        self.assertEqual(container.exec_run.call_count, 1)

    def test_scoped_creation_and_shell_quoted_secret(self):
        container = self.container()
        container.exec_run.side_effect = [
            (0, (b'[{"id":"subscription","isDefault":true}]', None)),
            (0, (None, None)),
            (0, (b'{"appId":"app","tenant":"tenant","password":"a b;$x"}', None)),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            helper.create_principal(container, 'name with spaces', 'subscription')
        command = container.exec_run.call_args.args[0]
        self.assertIn('/subscriptions/subscription', command)
        self.assertIn('Contributor', command)
        self.assertIn('name with spaces', command)
        self.assertIn("export AZURE_SECRET_PASSWORD='a b;$x'", output.getvalue())

    def test_cli_failure_does_not_print_credential_stdout(self):
        container = Mock()
        container.exec_run.return_value = (1, (b'sensitive password', b'role assignment denied'))
        with self.assertRaisesRegex(RuntimeError, 'role assignment denied') as error:
            helper.az(container, ['ad', 'sp'], expect_json=True)
        self.assertNotIn('sensitive password', str(error.exception))


if __name__ == '__main__':
    unittest.main()
