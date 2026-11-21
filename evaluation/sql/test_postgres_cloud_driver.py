"""Offline deployment checks; no AWS calls or benchmark SQL are performed."""
import ast
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from botocore.exceptions import ClientError

from evaluation.sql.postgres_cloud_driver import (
    bootstrap, bundle, check_owner, find_stack, submit_stack, template,
)
from evaluation.sql.postgres_cloud_preflight import SOURCES


class PostgresCloudDriverTests(unittest.TestCase):
    def plan(self):
        return dict(region='us-east-1', secret_arn='arn:aws:secretsmanager:us-east-1:123:secret:owned',
                    lambda_function='sql-function', staging_bucket='staging', driver_ami='ami-owned',
                    driver_instance_type='m7i-flex.large', subnet_id='subnet-owned',
                    lambda_security_group_id='sg-existing')

    def test_instance_role_can_only_read_exact_secret_invoke_exact_function_and_write_reports(self):
        artifact = dict(key='evaluation/postgres/a.tar.gz', sha256='a'*64, report_prefix='reports/owned/')
        result = template(self.plan(), artifact, '/dev/sda1')
        statements = result['Resources']['Role']['Properties']['Policies'][0]['PolicyDocument']['Statement']
        actions = {action for statement in statements for action in statement['Action']}
        self.assertEqual(actions, {'secretsmanager:GetSecretValue', 'lambda:InvokeFunction',
                                  'lambda:GetFunctionConfiguration', 's3:GetObject', 's3:PutObject'})
        put = next(s for s in statements if s['Action'] == ['s3:PutObject'])
        self.assertEqual(put['Resource'], 'arn:aws:s3:::staging/reports/owned/*')
        invoke = next(s for s in statements if 'lambda:InvokeFunction' in s['Action'])
        self.assertEqual(invoke['Resource'], 'arn:aws:lambda:us-east-1:123:function:sql-function')
        props = result['Resources']['Driver']['Properties']
        self.assertEqual(props['NetworkInterfaces'][0]['GroupSet'], ['sg-existing'])
        self.assertEqual(props['MetadataOptions']['HttpTokens'], 'required')
        self.assertTrue(props['BlockDeviceMappings'][0]['Ebs']['DeleteOnTermination'])
        self.assertFalse(any(r['Type'] == 'AWS::EC2::SecurityGroup' for r in result['Resources'].values()))

    def test_bootstrap_compiles_and_checks_bundle_without_submitting_queries(self):
        script = bootstrap('bucket', 'code.tar.gz', 'a'*64, 'us-east-1')
        syntax = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        ast.parse(script.split("<<'DOWNLOAD'\n")[1].split('\nDOWNLOAD\n')[0])
        self.assertIn('checksum mismatch', script)
        self.assertIn('verify-local', script)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', script)
        self.assertNotIn('.invoke(', script)

    def test_retry_reuses_owned_stack_without_a_second_create(self):
        request = dict(StackName='driver', ClientRequestToken='owned')
        cf = Mock()
        cf.describe_stacks.return_value = {'Stacks': [dict(StackId='stack-id', StackName='driver',
            StackStatus='CREATE_IN_PROGRESS', Tags=[dict(Key='sebs-driver-owner', Value='owned')])]}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            submit_stack(cf, request, folder)
            self.assertEqual(json.loads((folder/'stack.json').read_text())['stack_id'], 'stack-id')
        cf.create_stack.assert_not_called()

    def test_foreign_stack_is_never_adopted(self):
        with self.assertRaisesRegex(ValueError, 'another driver'):
            check_owner({'Tags': [{'Key': 'sebs-driver-owner', 'Value': 'foreign'}]}, {'ClientRequestToken': 'owned'})

    def test_absent_stack_submits_saved_request_and_records_identifier(self):
        cf = Mock()
        cf.describe_stacks.side_effect = ClientError({'Error': {'Code': 'ValidationError',
            'Message': 'Stack with id driver does not exist'}}, 'DescribeStacks')
        cf.create_stack.return_value = {'StackId': 'created'}
        request = dict(StackName='driver', ClientRequestToken='owned')
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            submit_stack(cf, request, folder)
            self.assertEqual(json.loads((folder/'stack.json').read_text())['stack_id'], 'created')
            with self.assertRaisesRegex(ValueError, 'previously created'):
                submit_stack(cf, request, folder)
        cf.create_stack.assert_called_once_with(**request)

    def test_access_denied_is_not_treated_as_missing_stack(self):
        cf = Mock()
        cf.describe_stacks.side_effect = ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'denied'}}, 'DescribeStacks')
        with self.assertRaises(ClientError):
            find_stack(cf, 'driver')

    def test_bundle_contains_verbatim_deployed_sources_and_refuses_version_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package_path = root/'lambda.zip'
            for source in SOURCES.values():
                path = root/source
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# exact source: ' + source)
            def package(version):
                with ZipFile(package_path, 'w') as archive:
                    for member, source in SOURCES.items():
                        archive.writestr(member, (root/source).read_bytes())
                    archive.writestr(f'.python_packages/lib/site-packages/psycopg-{version}.dist-info/METADATA', '')
                return base64.b64encode(hashlib.sha256(package_path.read_bytes()).digest()).decode()
            def fake_source(destination, plan, trace):
                with tarfile.open(destination, 'w:gz') as target:
                    entry = tarfile.TarInfo('sebs/__init__.py')
                    entry.size = 0
                    target.addfile(entry, io.BytesIO(b''))
            plan = dict(source_archive=str(package_path), lambda_code_sha256=package('3.3.9'))
            with patch('evaluation.sql.postgres_cloud_driver.source_bundle', side_effect=fake_source):
                destination = root/'bundle.tar.gz'
                bundle(destination, plan, root/'trace.json', root=root)
                with tarfile.open(destination) as archive:
                    self.assertEqual(archive.extractfile('driver_function/sql.py').read(),
                                     (root/SOURCES['function/sql.py']).read_bytes())
                    self.assertNotIn('lambda.zip', archive.getnames())
                plan['lambda_code_sha256'] = package('wrong')
                with self.assertRaisesRegex(ValueError, 'Psycopg 3.3.9'):
                    bundle(destination, plan, root/'trace.json', root=root)


if __name__ == '__main__':
    unittest.main()
