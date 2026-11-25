"""Single-query SF10 validation through the native Azure Function with private Blob receipts.

The query/deadline are explicit; Q6 defaults to its separate 240-second treatment. No SQL or HTTP POST is
retried. Native SeBS builds/deploys the same benchmark; only transport receipts
are added outside its measured SQL handler. Partial outcomes remain unresolved.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid

from evaluation.sql.postgres_azure_baseline import ROOT, configuration, save

CONTAINER='sebs-sql-results'


def validate_result(folder, result):
    state=json.loads((folder/'status.json').read_text())
    if result.get('job_id')!=state['job_id']:
        raise ValueError('Collected result belongs to another job')
    save(folder/'receipt.json',result)
    if result['state']=='running':
        return False
    if result['state']=='failed':
        state.update(state='failed',exception=result.get('exception'),invocation_id=result.get('invocation_id'))
        save(folder/'status.json',state)
        print(state.get('query','q6'),'failed:',result.get('exception','No exception details'))
        return True
    if result['state']!='complete':raise ValueError('Unknown receipt state')
    from sebs.benchmark import load_benchmark_input
    validator=load_benchmark_input(str(ROOT/'benchmarks/700.analytics/720.sql-postgresql'))
    event=json.loads((folder/'input.json').read_text())
    response=result['response']
    error=validator.validate_output(None,event,response['result'],'python')
    state.update(state='complete' if error is None else 'invalid',valid=error is None,
                 validation_error=error,invocation_id=result['invocation_id'])
    save(folder/'status.json',state)
    query=state.get('query','q6')
    measurement=response['result']['measurement'][query]
    summary={'valid':error is None,'state':state['state'],'query':query,'scale_factor':10,
        'statement_timeout_ms':state.get('statement_timeout_ms',240000),'function_s':measurement['handler_time']/1e6,
        'sql_s':measurement['query_time']/1e6,'connect_ms':measurement['connect_time']/1e3,
        'credentials_ms':measurement['credential_time']/1e3,'invocation_id':result['invocation_id'],
        'scope':'Single-observation SF10 validation at the recorded deadline; Blob overhead excluded from SQL handler timing',
        'cost':'No provider duration or native Flex billing inferred'}
    save(folder/'summary.json',summary)
    print(json.dumps(summary,indent=2))
    return True


def wait_for_result(folder, blob, wait_seconds=420):
    from azure.core.exceptions import ResourceNotFoundError
    deadline=time.monotonic()+wait_seconds
    while True:
        try:
            result=json.loads(blob.download_blob().readall())
        except ResourceNotFoundError:
            result=None
        if result is not None and validate_result(folder,result):
            return
        if time.monotonic()>=deadline:
            state=json.loads((folder/'status.json').read_text())
            state.update(state='unresolved',note='No final durable receipt; use collect later, never repeat the POST')
            save(folder/'status.json',state)
            raise TimeoutError('No completed SQL receipt yet. Use collect; no query retried.')
        time.sleep(5)


def wait_for_receipt_wrapper(endpoint, timeout=180):
    # Read-only probe avoids sending SQL to a stale/restarting HTTP worker.
    deadline=time.monotonic()+timeout
    attempts=0
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener=urllib.request.build_opener(NoRedirect())
    while True:
        attempts+=1
        request=urllib.request.Request(endpoint+'?sebs-sql-health=1',method='GET')
        try:
            with opener.open(request,timeout=15) as response:
                value=json.loads(response.read())
            if value.get('sql_result_receipt_version')==1:
                return {'attempts':attempts,'request_id':value.get('request_id')}
        except (urllib.error.URLError,TimeoutError,OSError,ValueError):
            pass
        if time.monotonic()>=deadline:
            raise TimeoutError('Receipt wrapper is not ready; no SQL POST was sent')
        time.sleep(5)


def start(config_path, storage, cache, folder, query='q6', statement_timeout_ms=240000):
    if query not in ('q6','q1','q12','q3','q21') or type(statement_timeout_ms) is not int or not 1 <= statement_timeout_ms <= 240000:
        raise ValueError('Use a supported query and a 1..240000ms SQL deadline')
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('Recorded/nonempty job directory. Use status/collect; never repeat the POST.')
    from sebs.sebs import SeBS
    from sebs.azure.triggers import HTTPTrigger
    from azure.storage.blob import BlobServiceClient, BlobSasPermissions, generate_blob_sas
    from azure.core.exceptions import ResourceExistsError
    config=configuration(json.loads(config_path.read_text()),query,'fresh')
    attachment=json.loads(storage.read_text())
    sql=attachment['sql']['postgresql']
    azure=config['deployment']['azure']
    if sql.get('provider')!='azure' or sql['region']!=azure['region'] or sql['azure_subscription_id'].lower()!=azure['credentials']['subscription_id'].lower():
        raise ValueError('SQL storage/deployment region or subscription differ')
    config['experiments']['benchmark-inputs']['720.sql-postgresql']['statement_timeout_ms']=statement_timeout_ms
    config['experiments'].update(update_code=False,update_storage=False)
    config['deployment']['azure'].setdefault('storage',{}).update(attachment)
    save(folder/'config.json',config)
    state={'state':'preparing','job_id':uuid.uuid4().hex,'query':query,'scale_factor':10,
           'statement_timeout_ms':statement_timeout_ms,'http_posts':0}
    save(folder/'status.json',state)
    # This path uses the native deployment client; no alternative Function App.
    with SeBS(str(cache),str(folder/'prepare'),verbose=False) as sebs:
        deployment=sebs.get_deployment(config)
        try:
            deployment.initialize()
            settings=sebs.get_experiment_config(config['experiments'])
            benchmark=sebs.get_benchmark('720.sql-postgresql',deployment,settings)
            event=benchmark.prepare_input(deployment.system_resources,'large',replace_existing=False)
            if event['queries']!=[query] or event['statement_timeout_ms']!=statement_timeout_ms or event['scale_factor']!=10:
                raise ValueError('Prepared input differs from the query/SF10/deadline plan')
            benchmark.benchmark_config.memory=512
            benchmark.benchmark_config.timeout=300
            function=deployment.get_function(benchmark)
            endpoints=[trigger.url for trigger in function.triggers_all() if isinstance(trigger,HTTPTrigger)]
            if len(endpoints)!=1:raise ValueError('Expected one native HTTP endpoint')
            connection=function.function_storage.connection_string
            parts=dict(item.split('=',1) for item in connection.split(';') if '=' in item)
            account=function.function_storage.account_name
            service=BlobServiceClient.from_connection_string(connection)
            try:service.create_container(CONTAINER)
            except ResourceExistsError:pass
            key=state['job_id']+'.json'
            sas=generate_blob_sas(account_name=account,container_name=CONTAINER,blob_name=key,
                account_key=parts['AccountKey'],permission=BlobSasPermissions(create=True,write=True),
                expiry=datetime.now(timezone.utc)+timedelta(hours=1),protocol='https')
            url='https://'+account+'.blob.core.windows.net/'+CONTAINER+'/'+key+'?'+sas
            save(folder/'input.json',event)  # SAS is never written to local public state.
            metadata=function.sql_resources
            state.update(subscription=sql['azure_subscription_id'],app_id=metadata['app_id'],
                owner=metadata['owner'],storage_account=account,
                storage_resource_group=metadata['app_id'].split('/')[4],function_name=function.name,
                function_hash=function.code_hash if hasattr(function,'code_hash') else function.serialize()['hash'],
                source_transport='native HTTP with durable private Blob receipt',function_timeout_s=300,memory_mb=512)
            state['readiness_probe']=wait_for_receipt_wrapper(endpoints[0])
            # Checkpoint before sending. A crash or network error must never cause resubmission.
            state.update(state='submission_recorded',http_posts=1,
                         submitted_at=datetime.now(timezone.utc).isoformat())
            save(folder/'status.json',state)
            payload={**event,'sebs_sql_result':{'job_id':state['job_id'],'upload_url':url}}
            request=urllib.request.Request(endpoints[0],data=json.dumps(payload).encode(),
                                           headers={'Content-Type':'application/json'},method='POST')
            print(f'Submitting one {query.upper()} attempt with {statement_timeout_ms/1000:g}-second SQL limit. Results will be collected from Blob.',flush=True)
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
            try:
                with urllib.request.build_opener(NoRedirect()).open(request,timeout=280) as response:
                    state['http_status']=response.status
                    response.read()
            except urllib.error.HTTPError as error:
                state['http_status']=error.code
                print('HTTP status:',error.code,'Collecting the independent result receipt; not resubmitting.',flush=True)
            except (urllib.error.URLError,TimeoutError,OSError) as error:
                state['http_error_type']=type(error).__name__
                print('HTTP observation interrupted. Collecting the receipt; not resubmitting.',flush=True)
            state['state']='collecting';save(folder/'status.json',state)
            wait_for_result(folder,service.get_blob_client(CONTAINER,key))
        finally:deployment.shutdown()


def collect(folder):
    from sebs.azure.control import AzureControl
    from azure.storage.blob import BlobServiceClient
    state=json.loads((folder/'status.json').read_text())
    if not state.get('http_posts'):raise ValueError('No recorded submission; inspect preparation before taking action')
    with AzureControl(state['subscription']) as cli:
        account=cli.run(['storage','account','show','-g',state['storage_resource_group'],'-n',state['storage_account']])
        if (account.get('tags') or {}).get('sebs.sql.owner')!=state['owner']:
            raise ValueError('Function storage ownership differs')
        keys=cli.run(['storage','account','keys','list','-g',state['storage_resource_group'],'-n',state['storage_account']])
        cli.secrets.extend(key['value'] for key in keys)
        service=BlobServiceClient(account_url='https://'+state['storage_account']+'.blob.core.windows.net',credential=keys[0]['value'])
        wait_for_result(folder,service.get_blob_client(CONTAINER,state['job_id']+'.json'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['start','status','collect'])
    parser.add_argument('--config',type=Path)
    parser.add_argument('--storage-configuration',type=Path)
    parser.add_argument('--cache',type=Path)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--query',choices=['q6','q1','q12','q3','q21'],default='q6')
    parser.add_argument('--statement-timeout-ms',type=int,default=240000)
    args=parser.parse_args()
    if args.command=='start':
        if not all([args.config,args.storage_configuration,args.cache]):parser.error('start requires config, storage and cache')
        start(args.config,args.storage_configuration,args.cache,args.output_dir,args.query,args.statement_timeout_ms)
    elif args.command=='status':print((args.output_dir/'status.json').read_text())
    else:collect(args.output_dir)


if __name__=='__main__':main()
