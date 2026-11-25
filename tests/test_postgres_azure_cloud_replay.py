"""Offline native Azure/SQL normalization, paired replay and no-resubmit tests."""
import ast
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace,ModuleType
import unittest
from unittest.mock import Mock,patch

from evaluation.sql import postgres_azure_cloud_replay as study
from evaluation.sql.postgres_azure_baseline import save
from sebs.faas.function import ExecutionResult


class AzurePostgresReplayTests(unittest.TestCase):
    def plan(self):
        return {'function_endpoint':'https://function/api/handler','function_name':'function',
            'input_fingerprint':'f'*64,'native_package_sha256':'a'*64,'server_vm_id':'/vm',
            'staging_account':'owned','source_sha256':{},'secret_uri':'https://vault/secrets/reader',
            'function_identity_client_id':'identity',
            'input':{'namespace':'sebs_720_fixture','input_fingerprint':'f'*64,'queries':['q6'],
                     'scale_factor':1,'connection_policy':'fresh','statement_timeout_ms':120000}}

    def test_aws_matched_trace_preserves_snapshot_and_count(self):
        plan=self.plan()
        trace=study.trace(plan,'moderate',100,2)
        self.assertEqual([row['query'] for row in trace['requests'][:5]],list(study.ORDER))
        self.assertEqual(trace['requests'][-1]['at'],198)
        burst=study.trace(plan,'burst',100,8)
        self.assertTrue(all(row['at']==0 for row in burst['requests']))
        self.assertEqual(burst['input'],plan['input'])
        self.assertEqual(burst['settings']['max-in-flight'],8)
        with self.assertRaises(ValueError):study.trace(plan,'moderate',99,2)

    def test_native_http_result_normalization_keeps_raw_invocation_and_connection_flags(self):
        plan=self.plan();client=study.FunctionClient(plan,plan['input'])
        raw={'result':{'queries':{'q3':[[42]]}},'measurement':{'q3':{'query_time':100}},
             'connection_reused':False,'credential_cache_hit':True,'connection_policy':'fresh'}
        result=ExecutionResult.from_times(datetime.now(),datetime.now());result.request_id='id'
        result.stats.cold_start=True;result.times.benchmark=1000
        result.output={'result':raw,'container_id':'container','is_cold_worker':True}
        client.trigger.sync_invoke=Mock(return_value=result)
        client.verify(client.health(),{'input':plan['input']})
        output=client.invoke('q3')
        self.assertEqual(client.trigger.sync_invoke.call_args.args[0]['queries'],['q3'])
        self.assertEqual(plan['input']['queries'],['q6'])
        self.assertEqual(output.output['result'],{'queries':{'q3':[[42]]}})
        self.assertTrue(output.output['credential_cache_hit'])
        self.assertEqual(client.native['id']['output']['result'],raw)
        self.assertNotIn('provider_metrics',client.native['id']['output']['result'])
        self.assertIsNone(output.output['provider_metrics']['native_billing'])

    def test_remote_pairs_alternate_validate_and_upload_without_real_cloud(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            folder=Path(temp)/'run';folder.mkdir()
            plan=self.plan()
            settings={'plan':plan,'trace':study.trace(plan,'moderate',5,2),'expected':{},
                'repetitions':3,'blob_prefix':'results/run/'}
            save(folder/'settings.json',settings)
            order=[]
            class Client:
                def __init__(self,plan,event):self.native={}
            def replay(trace,client,path,validator):
                path.mkdir(parents=True);order.append(path.relative_to(folder).as_posix())
                save(path/'summary.json',{'observation_seconds':1})
                save(path/'requests.json',[]);return True
            with patch.object(study,'DirectPostgresClient',Client),patch.object(study,'FunctionClient',Client), \
                 patch.object(study,'verify_runtime'),patch.object(study,'reference_validator'), \
                 patch.object(study,'replay',side_effect=replay),patch.object(study,'upload_files') as upload, \
                 patch.dict(sys.modules,{'azure.identity':SimpleNamespace(ManagedIdentityCredential=Mock())}),patch('azure.storage.blob.BlobServiceClient'):
                study.remote_run(folder)
                self.assertEqual(order,['repeat-1/postgresql','repeat-1/functions',
                    'repeat-2/functions','repeat-2/postgresql','repeat-3/postgresql','repeat-3/functions'])
                state=json.loads((folder/'status.json').read_text())
                self.assertEqual(state['state'],'complete');self.assertEqual(state['completed_runs'],6)
                self.assertEqual(upload.call_count,7)
                with self.assertRaisesRegex(ValueError,'already started'):study.remote_run(folder)

    def test_invalid_phase_stops_without_next_backend(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            folder=Path(temp)/'run';folder.mkdir()
            plan=self.plan();save(folder/'settings.json',{'plan':plan,'trace':study.trace(plan,'burst',5,2),
                'expected':{},'repetitions':3,'blob_prefix':'results/run/'})
            with patch.object(study,'DirectPostgresClient'),patch.object(study,'verify_runtime'), \
                 patch.object(study,'reference_validator'),patch.object(study,'replay',return_value=False) as replay, \
                 patch.object(study,'upload_files'),patch.dict(sys.modules,{'azure.identity':SimpleNamespace(ManagedIdentityCredential=Mock())}), \
                 patch('azure.storage.blob.BlobServiceClient'):
                with self.assertRaisesRegex(RuntimeError,'Incomplete/invalid'):study.remote_run(folder)
                replay.assert_called_once()
            self.assertEqual(json.loads((folder/'status.json').read_text())['state'],'failed')

    def test_submission_program_has_checksum_single_start_and_active_job_guards(self):
        tree=ast.parse(study.SUBMIT.replace('__REQUEST__',repr({'run_id':'test'})))
        self.assertIn('hashlib.sha256(source)',study.SUBMIT)
        self.assertIn("status.read_text())['state']!='complete'",study.SUBMIT)
        self.assertIn('destination.exists()',study.SUBMIT)
        self.assertIn("':ro'",study.SUBMIT)
        calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
               and isinstance(node.func.value,ast.Name) and node.func.value.id=='subprocess' and node.func.attr=='run']
        self.assertEqual(len(calls),1)
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);save(folder/'runs/test/submission.json',{'recorded':True})
            with patch.object(study,'host_context') as context:
                with self.assertRaisesRegex(ValueError,'Recorded run ID'):
                    study.start(folder,'test',folder/'cache','moderate',100,2,3)
                context.assert_not_called()

    def test_pooled_summary_requires_all_answers_and_never_infers_cost(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            folder=Path(temp);local=folder/'runs/test';root=local/'collected'
            plan=self.plan();save(local/'settings.json',{'workload':'moderate','trace':study.trace(plan,'moderate',5,2),'repetitions':1})
            save(root/'status.json',{'state':'complete','completed_runs':2})
            for backend in study.BACKENDS:
                path=root/'repeat-1'/backend
                save(path/'requests.json',[{'valid':True,'failure':False,'total_latency_s':i+1,'queue_delay_s':0,
                    'cold_start':backend=='functions' and i==0} for i in range(5)])
                save(path/'summary.json',{'observation_seconds':9})
            report=study.summarize(folder,'test')
            self.assertEqual(report['backends']['postgresql']['mean_s'],3)
            self.assertEqual(report['backends']['functions']['cold_starts'],1)
            self.assertNotIn('cost_usd',report)

    def test_both_real_adapters_interoperate_with_shared_scheduler_and_validator(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            root=Path(temp);plan=self.plan();plan['server_instance_id']='/vm'
            trace=study.trace(plan,'burst',5,2)
            validator=study.reference_validator({query:[['42']] for query in study.ORDER})
            def response(event):
                query=event['queries'][0]
                return {'result':{'queries':{query:[[42]]}},'measurement':{query:{'query_time':1}},
                        'connection_policy':'fresh','connection_reused':False}
            direct=study.DirectPostgresClient(plan,plan['input'])
            direct.worker=Mock(return_value=SimpleNamespace(handler=response))
            function=study.FunctionClient(plan,plan['input'])
            def invoke(event):
                result=ExecutionResult.from_times(datetime.now(),datetime.now())
                result.request_id='id-'+event['queries'][0];result.stats.cold_start=False
                result.output={'result':response(event)}
                return result
            function.trigger.sync_invoke=Mock(side_effect=invoke)
            self.assertTrue(study.replay(trace,direct,root/'direct',validator=validator))
            self.assertTrue(study.replay(trace,function,root/'function',validator=validator))
            for name in ('direct','function'):
                self.assertEqual(json.loads((root/name/'summary.json').read_text())['valid'],5)
            self.assertEqual(len(function.native),5)

    def test_host_submission_checkpoints_before_managed_command_and_never_repeats(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            folder=Path(temp);plan=self.plan()
            plan.update(owner='owner',sql_storage_id='storage',driver_vm_id='/driver')
            save(folder/'driver-ready.json',{'status':'DRIVER_READY'})
            backend=Mock();cli=Mock()
            cli.run.return_value={'tags':{'sebs.sql.owner':'owner','sebs.sql.role':'comparison-driver'}}
            backend.control.return_value.__enter__=Mock(return_value=cli)
            backend.control.return_value.__exit__=Mock(return_value=False)
            target=Mock()
            def submit(*args,**kwargs):
                self.assertTrue((folder/'runs/test/submission.json').exists())
                self.assertIn('systemd-run',args[1])
                raise TimeoutError('Unknown managed-command submission')
            target.command.side_effect=submit
            with patch.object(study,'host_context',return_value=(object(),backend,{},plan,folder/'storage.json')), \
                 patch.object(study,'source_package',return_value=({},None,plan['native_package_sha256'],{})), \
                 patch.object(study,'official_answers',return_value={}),patch.object(study,'verify_live_function'), \
                 patch.object(study,'DriverCommands',return_value=target):
                with self.assertRaises(TimeoutError):study.start(folder,'test',folder/'cache','moderate',100,2,3)
                with self.assertRaisesRegex(ValueError,'Recorded run ID'):
                    study.start(folder,'test',folder/'cache','moderate',100,2,3)
                target.command.assert_called_once()

    def test_guest_submission_rejects_active_job_without_launching_another_container(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)
            (base/'repo/evaluation/sql').mkdir(parents=True)
            settings=json.dumps({'run_id':'test'}).encode();source=b'# public runner source'
            request={'run_id':'test','account_url':'https://owned.blob.core.windows.net',
                'settings_key':'settings','settings_sha256':hashlib.sha256(settings).hexdigest(),
                'source_key':'source','source_sha256':hashlib.sha256(source).hexdigest()}
            program=study.SUBMIT.replace('__REQUEST__',repr(request)).replace(
                "base=Path('/opt/sebs-pg-azure-driver')",'base=Path('+repr(str(base))+')')
            service=Mock()
            def blob(container,key):
                return SimpleNamespace(download_blob=lambda:SimpleNamespace(readall=lambda:settings if key=='settings' else source))
            service.get_blob_client.side_effect=blob
            with patch.dict(sys.modules,{'azure.identity':SimpleNamespace(ManagedIdentityCredential=Mock())}), \
                 patch('azure.storage.blob.BlobServiceClient',return_value=service), \
                 patch('subprocess.run') as launch,redirect_stdout(io.StringIO()):
                exec(program,{})
                launch.assert_called_once()
                self.assertEqual((base/'repo/evaluation/sql/postgres_azure_cloud_replay.py').read_bytes(),source)
                with self.assertRaisesRegex(RuntimeError,'active or unresolved'):exec(program,{})
                launch.assert_called_once()
