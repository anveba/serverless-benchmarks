"""Offline tests for durable SQL receipts and one-shot Azure job submission."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, mock_open, patch
from urllib.error import HTTPError

from evaluation.sql import postgres_azure_long_query as jobs
from evaluation.sql.postgres_azure_baseline import ROOT, save

WRAPPER=ROOT/'benchmarks/wrappers/azure/python/handler.py'
JOB_ID='a'*32
URL='https://owned.blob.core.windows.net/sebs-sql-results/'+JOB_ID+'.json?sig=private'


class DurableSQLWrapperTests(unittest.TestCase):
    def setUp(self):
        import azure
        azure_functions=ModuleType('azure.functions')
        azure_functions.HttpRequest=object;azure_functions.Context=object
        azure_functions.HttpResponse=lambda body,**kwargs:SimpleNamespace(body=body)
        self.package=ModuleType('durable_sql_fixture');self.package.__path__=[str(WRAPPER.parent)]
        self.function=ModuleType('durable_sql_fixture.function')
        self.function.handler=Mock(return_value={'result':{'queries':{'q6':[[42]]}},'measurement':{'q6':{}}})
        self.modules=patch.dict(sys.modules,{'azure.functions':azure_functions,
            'durable_sql_fixture':self.package,'durable_sql_fixture.function':self.function})
        self.modules.start();self.addCleanup(self.modules.stop)
        self.attribute=patch.object(azure,'functions',azure_functions,create=True)
        self.attribute.start();self.addCleanup(self.attribute.stop)
        self.environment=patch.dict(os.environ,{'SEBS_SQL_CONFIG':json.dumps({'type':'postgresql','provider':'azure'}),
            'AzureWebJobsStorage':'AccountName=owned;AccountKey=SECRET_KEY_DO_NOT_PERSIST','CONTAINER_NAME':'fixture'},clear=True)
        self.environment.start();self.addCleanup(self.environment.stop)
        spec=importlib.util.spec_from_file_location('durable_sql_fixture.handler',WRAPPER)
        self.wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.wrapper)
        self.context=SimpleNamespace(invocation_id='invocation')

    def payload(self):return {'queries':['q6'],'sebs_sql_result':{'job_id':JOB_ID,'upload_url':URL}}

    def test_health_probe_never_claims_or_executes_sql(self):
        with patch.object(self.wrapper,'_sql_job_upload') as upload:
            response=self.wrapper.main(SimpleNamespace(method='GET',params={'sebs-sql-health':'1'}),self.context)
        self.assertEqual(json.loads(response.body)['sql_result_receipt_version'],1)
        self.function.handler.assert_not_called();upload.assert_not_called()

    def test_ordinary_requests_do_not_create_receipts(self):
        with patch.object(self.wrapper,'_sql_job_upload') as upload,patch('builtins.open',mock_open(read_data='worker')),patch('os.path.exists',return_value=True):
            response=self.wrapper.main(SimpleNamespace(get_json=lambda:{'queries':['q6']}),self.context)
        self.assertEqual(json.loads(response.body)['request_id'],'invocation')
        upload.assert_not_called();self.function.handler.assert_called_once()

    def test_claim_precedes_sql_and_success_is_saved_before_http_response(self):
        events=[]
        def upload(receipt,value,create=False):events.append(value['state']);return 'etag'
        self.function.handler.side_effect=lambda event:events.append('sql') or {'measurement':{'q6':{}}}
        with patch.object(self.wrapper,'_sql_job_upload',side_effect=upload),patch('builtins.open',mock_open(read_data='worker')),patch('os.path.exists',return_value=True):
            self.wrapper.main(SimpleNamespace(get_json=self.payload),self.context)
        self.assertEqual(events,['running','sql','complete'])

    def test_duplicate_claim_never_executes_sql(self):
        error=HTTPError(URL,412,'Already claimed',{},None)
        with patch.object(self.wrapper,'_sql_job_upload',side_effect=error),self.assertRaises(HTTPError):
            self.wrapper.main(SimpleNamespace(get_json=self.payload),self.context)
        self.function.handler.assert_not_called()

    def test_sql_exception_is_persisted_once_without_retry(self):
        error=RuntimeError('SQL request failed at query (QueryCanceled); no query was retried')
        self.function.handler.side_effect=error
        with patch.object(self.wrapper,'_sql_job_upload',return_value='etag') as upload,self.assertRaises(RuntimeError):
            self.wrapper.main(SimpleNamespace(get_json=self.payload),self.context)
        self.function.handler.assert_called_once()
        saved=upload.call_args.args[1]
        self.assertEqual(saved['state'],'failed')
        self.assertIn('QueryCanceled',saved['exception'])
        self.assertNotIn('private',json.dumps(saved))

    def test_receipts_reject_external_account_or_path(self):
        for url in ['https://other.blob.core.windows.net/sebs-sql-results/'+JOB_ID+'.json?sig=x',
                    URL.replace('https:','http:'),URL.replace('/sebs-sql-results/','/other/'),
                    URL+'#fragment']:
            payload=self.payload();payload['sebs_sql_result']['upload_url']=url
            with patch.object(self.wrapper,'_sql_job_upload') as upload,self.assertRaises(ValueError):
                self.wrapper._sql_job_receipt(payload,self.context)
            upload.assert_not_called()

    def test_uploads_are_conditional_and_do_not_follow_redirects(self):
        response=Mock();response.__enter__=Mock(return_value=SimpleNamespace(headers={'ETag':'next'}))
        response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response
        receipt={'upload_url':URL,'etag':'claimed'}
        with patch('urllib.request.build_opener',return_value=opener):
            self.wrapper._sql_job_upload(receipt,{'state':'running'},create=True)
            headers={key.lower():value for key,value in opener.open.call_args.args[0].header_items()}
            self.assertEqual(headers['if-none-match'],'*')
            self.wrapper._sql_job_upload(receipt,{'state':'complete'})
            headers={key.lower():value for key,value in opener.open.call_args.args[0].header_items()}
            self.assertEqual(headers['if-match'],'claimed')
            self.assertEqual(opener.open.call_count,2)


class AzureLongQueryTests(unittest.TestCase):
    def test_http_timeout_collects_receipt_without_a_second_post(self):
        from sebs.azure.triggers import HTTPTrigger
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            root=Path(temp);folder=root/'job'
            base=json.loads((ROOT/'configs/sql-postgresql-azure.example.json').read_text())
            save(root/'config.json',base)
            save(root/'storage.json',{'sql':{'postgresql':{'provider':'azure','region':'swedencentral',
                'azure_subscription_id':base['deployment']['azure']['credentials']['subscription_id']}}})
            event={'queries':['q21'],'scale_factor':10,'statement_timeout_ms':120000,
                   'namespace':'fixture','input_fingerprint':'f'*64}
            benchmark=SimpleNamespace(prepare_input=Mock(return_value=event),benchmark_config=SimpleNamespace(memory=512,timeout=300))
            function=SimpleNamespace(name='function',code_hash='hash',triggers_all=lambda:[HTTPTrigger('https://function.azurewebsites.net/api/handler')],
                function_storage=SimpleNamespace(account_name='owned',connection_string='AccountName=owned;AccountKey=SECRET_KEY_DO_NOT_PERSIST'),
                sql_resources={'app_id':'/subscriptions/sub/resourceGroups/group/providers/Microsoft.Web/sites/function','owner':'owner'})
            deployment=Mock();deployment.get_function.return_value=function
            sebs=Mock();sebs.get_deployment.return_value=deployment;sebs.get_benchmark.return_value=benchmark
            manager=Mock();manager.__enter__=Mock(return_value=sebs);manager.__exit__=Mock(return_value=False)
            service=Mock()
            def collect(folder,blob):
                state=json.loads((folder/'status.json').read_text())
                self.assertEqual(state['http_status'],502)
                self.assertNotIn('SECRET_KEY_DO_NOT_PERSIST',json.dumps(state))
                self.assertNotIn('SECRET_SAS_DO_NOT_PERSIST',json.dumps(state))
                state['state']='complete';save(folder/'status.json',state)
            with patch('sebs.sebs.SeBS',return_value=manager), \
                 patch('azure.storage.blob.BlobServiceClient.from_connection_string',return_value=service), \
                 patch('azure.storage.blob.generate_blob_sas',return_value='SECRET_SAS_DO_NOT_PERSIST'), \
                 patch('urllib.request.OpenerDirector.open',side_effect=HTTPError('https://function',502,'timeout',{},None)) as post, \
                 patch.object(jobs,'wait_for_receipt_wrapper',return_value={'attempts':1}), \
                 patch.object(jobs,'wait_for_result',side_effect=collect):
                jobs.start(root/'config.json',root/'storage.json',root/'cache',folder,query='q21',statement_timeout_ms=120000)
                post.assert_called_once()
                deployment.get_function.assert_called_once()
                deployment.shutdown.assert_called_once()
                payload=json.loads(post.call_args.args[0].data)
                self.assertEqual(payload['statement_timeout_ms'],120000)
                self.assertEqual(payload['queries'],['q21'])
                self.assertFalse(json.loads((folder/'config.json').read_text())['experiments']['update_code'])
                self.assertIn('sebs_sql_result',payload)
                self.assertNotIn('sebs_sql_result',json.loads((folder/'input.json').read_text()))
                with self.assertRaisesRegex(ValueError,'Recorded/nonempty'):
                    jobs.start(root/'config.json',root/'storage.json',root/'cache',folder)
                post.assert_called_once()

    def test_failed_receipt_is_preserved_without_fabricated_timings(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            folder=Path(temp);save(folder/'status.json',{'job_id':JOB_ID})
            self.assertTrue(jobs.validate_result(folder,{'job_id':JOB_ID,'state':'failed',
                'invocation_id':'invocation','exception':'QueryCanceled'}))
            state=json.loads((folder/'status.json').read_text())
            self.assertEqual(state['state'],'failed')
            self.assertFalse((folder/'summary.json').exists())
            with self.assertRaisesRegex(ValueError,'another job'):
                jobs.validate_result(folder,{'job_id':'other','state':'complete'})

    def test_completed_blob_answer_uses_native_sf10_validation(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            folder=Path(temp);save(folder/'status.json',{'job_id':JOB_ID})
            event={'queries':['q6'],'scale_factor':10}
            save(folder/'input.json',event)
            response={'result':{'result':{'queries':{'q6':[[42]]}},'measurement':{'q6':{
                'handler_time':2000000,'query_time':1000000,'credential_time':10000,'connect_time':20000}}}}
            validator=Mock();validator.validate_output.return_value=None
            with patch('sebs.benchmark.load_benchmark_input',return_value=validator):
                self.assertTrue(jobs.validate_result(folder,{'job_id':JOB_ID,'state':'complete',
                    'invocation_id':'invocation','response':response}))
            validator.validate_output.assert_called_once_with(None,event,response['result'],'python')
            result=json.loads((folder/'summary.json').read_text())
            self.assertTrue(result['valid'])
            self.assertEqual(result['function_s'],2)
            self.assertEqual(result['sql_s'],1)

    def test_readiness_probes_only_get_until_new_wrapper_is_ready(self):
        response=Mock();response.__enter__=Mock(return_value=SimpleNamespace(read=lambda:json.dumps({
            'sql_result_receipt_version':1,'request_id':'probe'}).encode()))
        response.__exit__=Mock(return_value=False)
        with patch('urllib.request.OpenerDirector.open',side_effect=[
                HTTPError('https://function',503,'starting',{},None),response]) as request, \
             patch.object(jobs.time,'sleep'):
            result=jobs.wait_for_receipt_wrapper('https://function/api/handler')
        self.assertEqual(result['attempts'],2)
        self.assertTrue(all(call.args[0].method=='GET' for call in request.call_args_list))

    def test_collected_summary_uses_recorded_query_and_deadline(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            folder=Path(temp)
            save(folder/'status.json',{'job_id':JOB_ID,'query':'q21','statement_timeout_ms':120000})
            save(folder/'input.json',{'queries':['q21'],'scale_factor':10})
            response={'result':{'measurement':{'q21':{'handler_time':2000000,'query_time':1000000,
                'connect_time':1000,'credential_time':0}},'result':{'queries':{'q21':[[42]]}}}}
            validator=Mock();validator.validate_output.return_value=None
            with patch('sebs.benchmark.load_benchmark_input',return_value=validator):
                jobs.validate_result(folder,{'job_id':JOB_ID,'state':'complete',
                    'invocation_id':'invocation','response':response})
            result=json.loads((folder/'summary.json').read_text())
            self.assertEqual(result['query'],'q21')
            self.assertEqual(result['statement_timeout_ms'],120000)
