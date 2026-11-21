"""Offline cleanup checks: archive before deletion and fail closed on errors."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from evaluation.sql.postgres_cleanup import archive_and_delete, probe, run, verify
from evaluation.sql.postgres_costs import save


def error(code):
    return ClientError({'Error':{'Code':code,'Message':'fixture'}},'fixture')


class CleanupTests(unittest.TestCase):
    def logs(self):
        logs=Mock()
        logs.get_paginator.side_effect=lambda name: {
            'describe_log_groups':self.group_pages,
            'filter_log_events':self.report_pages}[name]
        return logs

    def setUp(self):
        self.group={'logGroupName':'/aws/lambda/function','creationTime':1,'arn':'arn:log'}
        self.group_pages=Mock()
        self.group_pages.paginate.return_value=[{'logGroups':[self.group]}]
        self.report_pages=Mock()
        event={'eventId':'e','timestamp':1,'message':'REPORT RequestId: id\tBilled Duration: 1 ms'}
        self.report_pages.paginate.return_value=[{'events':[event,event,{'eventId':'other','message':'application message'}]}]
        self.lam=Mock()
        self.lam.get_function_configuration.side_effect=error('ResourceNotFoundException')

    def test_archives_reports_before_delete_and_deduplicates_events(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            folder=Path(temp);logs=self.logs()
            def delete(**kwargs):
                self.assertTrue((folder/'archive-complete.json').exists())
                self.assertTrue((folder/'lambda-billing-reports.json').exists())
                self.assertNotIn('application message',(folder/'lambda-billing-reports.json').read_text())
            logs.delete_log_group.side_effect=delete
            archive_and_delete(logs,self.lam,'function',folder)
            logs.delete_log_group.assert_called_once_with(logGroupName='/aws/lambda/function')
            self.assertIn('"reports": 1',(folder/'archive-complete.json').read_text())

    def test_failed_archive_never_deletes(self):
        with tempfile.TemporaryDirectory() as temp:
            logs=self.logs()
            with patch('evaluation.sql.postgres_cleanup.save',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    archive_and_delete(logs,self.lam,'function',Path(temp))
            logs.delete_log_group.assert_not_called()

    def test_existing_lambda_or_corrupt_archive_blocks_deletion(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            folder=Path(temp);logs=self.logs()
            existing=Mock();existing.get_function_configuration.return_value={}
            with self.assertRaisesRegex(ValueError,'not confirmed absent'):
                archive_and_delete(logs,existing,'function',folder)
            logs.delete_log_group.assert_not_called()
            archive_and_delete(logs,self.lam,'function',folder)
            (folder/'lambda-billing-reports.json').write_text('corrupt')
            logs.delete_log_group.reset_mock()
            with self.assertRaisesRegex(ValueError,'hash'):
                archive_and_delete(logs,self.lam,'function',folder)
            logs.delete_log_group.assert_not_called()

    def test_access_denied_is_not_absence(self):
        call=Mock(side_effect=error('AccessDenied'))
        self.assertEqual(probe(call,['ResourceNotFoundException'],lambda r:'present'),'ERROR:AccessDenied')

    def test_wrong_account_blocks_log_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            save(root/'inventory.json',{'region':'us-east-1',
                'lambda':{'FunctionName':'sebs_a16b1210_720_python_3_12_x64'},
                'resources':{'database':{'stack':{'StackId':'arn:aws:cloudformation:us-east-1:expected:stack/name/id'}}}})
            session=Mock()
            session.client.return_value.get_caller_identity.return_value={'Account':'wrong'}
            with self.assertRaisesRegex(ValueError,'account differs'):
                run(root,root/'out',True,session)
            session.client.assert_called_once_with('sts',region_name='us-east-1')

    def test_verify_checks_exact_ids_and_flags_remaining_bucket(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            clients={name:Mock() for name in ['cloudformation','ec2','lambda','s3','logs','secretsmanager']}
            session=Mock();session.client.side_effect=lambda name,**kw:clients[name]
            clients['cloudformation'].describe_stacks.return_value={'Stacks':[{'StackStatus':'DELETE_COMPLETE'}]}
            clients['ec2'].describe_instances.return_value={'Reservations':[{'Instances':[{'State':{'Name':'terminated'}}]}]}
            clients['ec2'].describe_volumes.side_effect=error('InvalidVolume.NotFound')
            clients['ec2'].describe_vpc_endpoints.side_effect=error('InvalidVpcEndpointId.NotFound')
            clients['secretsmanager'].describe_secret.side_effect=error('ResourceNotFoundException')
            clients['lambda'].get_function_configuration.side_effect=error('ResourceNotFoundException')
            clients['logs'].get_paginator.return_value.paginate.return_value=[{'logGroups':[]}]
            clients['s3'].head_bucket.side_effect=error('404')
            resource={'stack':{'StackId':'stack'},'instance':{'InstanceId':'instance'},'volumes':[{'VolumeId':'volume'}]}
            inv={'resources':{'database':resource,'driver':resource},'endpoint':{'VpcEndpointId':'vpce'},'secret':{'ARN':'secret'}}
            result=verify(session,inv,'us-east-1','function','a16b1210',Path(temp))
            self.assertTrue(result['all_checked_resources_removed'])
            clients['s3'].head_bucket.assert_called_once_with(Bucket='sebs-benchmarks-a16b1210')
            clients['s3'].head_bucket.side_effect=None
            clients['s3'].head_bucket.return_value={}
            self.assertFalse(verify(session,inv,'us-east-1','function','a16b1210',Path(temp))['all_checked_resources_removed'])


if __name__=='__main__':
    unittest.main()
