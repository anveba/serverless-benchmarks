"""Service-principal passwords remain a single argument and stay out of errors."""
import unittest
from unittest.mock import Mock
from sebs.azure.cli import AzureCLI


class AzureCLILoginTests(unittest.TestCase):
    def client(self):
        client=AzureCLI.__new__(AzureCLI)
        client.docker_instance=Mock()
        client.wrapper=Mock()
        return client

    def test_password_with_leading_dash_spaces_and_metacharacters_is_one_argument(self):
        client=self.client()
        client.docker_instance.exec_run.return_value=(0,(b'[{"id":"subscription"}]',b'warning'))
        secret='-fixture with spaces;$x'
        result=client.login('application','tenant',secret)
        args=client.docker_instance.exec_run.call_args.args[0]
        self.assertIsInstance(args,list)
        self.assertIn('--password='+secret,args)
        self.assertNotIn('-p',args)
        self.assertEqual(result,b'[{"id":"subscription"}]')
        self.assertTrue(client.docker_instance.exec_run.call_args.kwargs['demux'])
        client.wrapper.info.assert_called_once_with('Azure login successful')

    def test_failure_redacts_password_and_does_not_echo_stdout_or_argv(self):
        client=self.client()
        secret='-fixture secret'
        client.docker_instance.exec_run.return_value=(1,(b'private login stdout',b'failed '+secret.encode()))
        with self.assertRaisesRegex(RuntimeError,'\\[REDACTED\\]') as error:
            client.login('application','tenant',secret)
        self.assertNotIn(secret,str(error.exception))
        self.assertNotIn('private login stdout',str(error.exception))
        self.assertNotIn('--password=',str(error.exception))
        client.wrapper.info.assert_not_called()

    def test_absent_stdout_keeps_bytes_return_type(self):
        client=self.client()
        client.docker_instance.exec_run.return_value=(0,(None,None))
        self.assertEqual(client.login('application','tenant','fixture'),b'')


if __name__ == '__main__':
    unittest.main()
