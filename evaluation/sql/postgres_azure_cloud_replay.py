"""Paired direct-PostgreSQL/Functions replay on the existing Azure cloud driver.

Uses the shared SeBS arrival scheduler, exact deployed SQL wrapper and official
SF1 validation. Explicit submission is checkpointed; SQL is never replayed.
"""
import argparse
import copy
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import subprocess
import sys
import threading

from evaluation.sql.postgres_azure_baseline import ROOT,save
from evaluation.sql.postgres_azure_cloud_driver import BASE,DriverCommands,context
from evaluation.sql.postgres_azure_cloud_preflight import source_package,verify_live_function
from evaluation.sql.postgres_cloud_replay import DirectPostgresClient,official_answers,reference_validator
from evaluation.sql.replay_service import run as replay
from sebs.azure.triggers import HTTPTrigger
from sebs.utils import serialize

RUN_ID=re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
ORDER=('q6','q1','q12','q3','q21')
BACKENDS=('postgresql','functions')


def utc():return datetime.now(timezone.utc).isoformat()


def trace(plan,workload,count,limit):
    if workload not in ('moderate','burst') or type(count) is not int or count<5 or count%5:
        raise ValueError('Use moderate/burst and a positive multiple of five requests')
    if type(limit) is not int or limit not in (2,8):raise ValueError('Use the AWS-matched two/eight slot limits')
    if plan['input']['scale_factor']!=1 or plan['input']['connection_policy']!='fresh':
        raise ValueError('The paired study requires the existing fresh-connection SF1 input')
    requests=[{'at':index*(2 if workload=='moderate' else 0),'query':ORDER[index%5]} for index in range(count)]
    return {'input':copy.deepcopy(plan['input']),'requests':requests,'scale_factor':1,
        'settings':{'benchmark':'720.sql-postgresql','input-size':'small','memory':512,
                    'max-in-flight':limit,'requests':copy.deepcopy(requests)},
        'validation_in_timing':False,'warmup':False,'automatic_retries':False}


class FunctionClient:
    backend='azure-functions-postgresql'
    cold_start_semantics='Native SeBS environment marker; observed cold and warm requests retained'
    def __init__(self,plan,payload):
        self.plan=plan;self.input=copy.deepcopy(payload);self.endpoint=plan['function_endpoint']
        self.trigger=HTTPTrigger(self.endpoint);self.native={};self.lock=threading.Lock()
    def health(self):
        return {'engine':'postgresql','function':self.plan['function_name'],
            'input_fingerprint':self.input['input_fingerprint'],
            'source_package_sha256':self.plan['native_package_sha256'],
            'source':'Host-verified native publication and frozen source package'}
    def verify(self,health,trace):
        if trace['input']!=self.input or health['input_fingerprint']!=self.plan['input_fingerprint']:
            raise ValueError('Function path/input differs from the paired snapshot')
    def invoke(self,query):
        payload=copy.deepcopy(self.input);payload['queries']=[query]
        result=self.trigger.sync_invoke(payload)
        # Snapshot before normalizing. Full native request/time/body records are
        # preserved for provider telemetry and Flex meters collected later.
        native=json.loads(serialize(result))
        raw=result.output['result']
        if query not in raw['result']['queries'] or query not in raw['measurement']:
            raise ValueError('Function returned another query')
        with self.lock:self.native[result.request_id]=native
        result.output={**raw,'request_id':result.request_id,'query':query,
            'provider_metrics':{'handler_execution_us':result.times.benchmark,'native_billing':None},
            'execution_environment':{'container_id':native['output'].get('container_id'),
                                     'is_cold_worker':native['output'].get('is_cold_worker')}}
        return result


def verify_runtime(plan):
    import psycopg
    if sys.version_info[:2]!=(3,11) or psycopg.__version__!='3.3.9':raise ValueError('Driver Python/Psycopg differs')
    for member,digest in plan['source_sha256'].items():
        if hashlib.sha256((ROOT/'driver_function'/Path(member).name).read_bytes()).hexdigest()!=digest:
            raise ValueError('Driver sources differ from the verified Function package')
    expected={'type':'postgresql','provider':'azure','secret_uri':plan['secret_uri'],
              'identity_client_id':plan['function_identity_client_id']}
    if json.loads(os.environ['SEBS_SQL_CONFIG'])!=expected:raise ValueError('Runtime SQL identity/secret differs')


def upload_files(folder,settings,service):
    container=service.get_container_client('sql')
    for path in sorted(folder.rglob('*')):
        if path.is_file() and path.suffix in ('.json','.log'):
            with path.open('rb') as stream:
                container.upload_blob(settings['blob_prefix']+path.relative_to(folder).as_posix(),stream,overwrite=True)


def remote_run(folder):
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobServiceClient
    settings=json.loads((folder/'settings.json').read_text())
    plan=settings['plan'];plan['server_instance_id']=plan['server_vm_id']
    with (folder.parent/'.comparison.lock').open('a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        if (folder/'started.json').exists():raise ValueError('Recorded run already started; no replay')
        save(folder/'started.json',{'started_at':utc()})
        state={'state':'running','completed_runs':0,'total_runs':2*settings['repetitions'],
               'current':None,'started_at':utc()}
        save(folder/'status.json',state)
        service=BlobServiceClient('https://'+plan['staging_account']+'.blob.core.windows.net',credential=ManagedIdentityCredential())
        validator=reference_validator(settings['expected'])
        try:
            for repetition in range(1,settings['repetitions']+1):
                for backend in (BACKENDS if repetition%2 else tuple(reversed(BACKENDS))):
                    phase=f'repeat-{repetition}/{backend}'
                    state['current']=phase;save(folder/'status.json',state)
                    verify_runtime(plan)
                    client=DirectPostgresClient(plan,settings['trace']['input']) if backend=='postgresql' else FunctionClient(plan,settings['trace']['input'])
                    try:
                        valid=replay(settings['trace'],client,folder/phase,validator=validator)
                    finally:
                        if backend=='functions':
                            save(folder/phase/'azure-invocations.json',{'_invocations':{plan['function_name']:client.native},
                                'provider_metrics_pending':True,'billing_scope':'Native Flex meters still required'})
                    if not valid:raise RuntimeError('Incomplete/invalid answers; inspect saved results, no query retried')
                    summary=json.loads((folder/phase/'summary.json').read_text())
                    summary['cost_note']='Azure native Flex billing and shared database/driver allocation collected separately; no cost inferred from handler time'
                    save(folder/phase/'summary.json',summary)
                    state['completed_runs']+=1;save(folder/'status.json',state)
                    upload_files(folder,settings,service)
            state.update(state='complete',current=None,finished_at=utc())
        except BaseException as error:
            state.update(state='failed',error=type(error).__name__+': '+str(error),finished_at=utc())
            raise
        finally:
            save(folder/'status.json',state)
            try:upload_files(folder,settings,service)
            except Exception as error:print('Artifact upload incomplete:',type(error).__name__,'collect from existing VM; no replay',flush=True)


SUBMIT=r"""
import fcntl,hashlib,json,subprocess
from pathlib import Path
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
request=__REQUEST__
base=Path('/opt/sebs-pg-azure-driver');runs=base/'runs';runs.mkdir(exist_ok=True)
service=BlobServiceClient(request['account_url'],credential=ManagedIdentityCredential())
with (runs/'.submission.lock').open('a') as lock:
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
    active=runs/'active.json'
    if active.exists():
        old=json.loads(active.read_text());status=runs/old['run_id']/'status.json'
        if not status.exists() or json.loads(status.read_text())['state']!='complete':
            raise RuntimeError('Another driver run is active or unresolved; inspect it, no new job submitted')
    destination=runs/request['run_id']
    if destination.exists():raise RuntimeError('Run directory exists; no duplicate submission')
    data=service.get_blob_client('sql',request['settings_key']).download_blob().readall()
    source=service.get_blob_client('sql',request['source_key']).download_blob().readall()
    if hashlib.sha256(data).hexdigest()!=request['settings_sha256'] or hashlib.sha256(source).hexdigest()!=request['source_sha256']:
        raise RuntimeError('Settings/runner checksum differs; no SQL submitted')
    if json.loads(data).get('run_id')!=request['run_id']:
        raise RuntimeError('Settings refer to another run; no SQL submitted')
    destination.mkdir();(destination/'settings.json').write_bytes(data)
    for name in ('runtime.json','driver-ready.json'):
        path=base/name
        if path.exists():(destination/('driver-'+name)).write_bytes(path.read_bytes())
    (base/'repo/evaluation/sql/postgres_azure_cloud_replay.py').write_bytes(source)
    active.write_text(json.dumps({'run_id':request['run_id']}))
    command=['systemd-run','--unit','sebs-pg-azure-'+request['run_id'],'--collect',
        '--property=StandardOutput=append:'+str(destination/'driver.log'),
        '--property=StandardError=append:'+str(destination/'driver.log'),
        'docker','run','--rm','--name','sebs-pg-azure-'+request['run_id'],'--network','host',
        '--env-file',str(base/'runtime.env'),
        '-v',str(base/'repo')+':'+str(base/'repo')+':ro',
        '-v',str(runs)+':'+str(runs),'-w',str(base/'repo'),
        'sebs-pg-azure-driver:ready','python','-B','-m','evaluation.sql.postgres_azure_cloud_replay',
        'remote-run','--state-dir',str(destination)]
    subprocess.run(command,check=True)
print('Paired comparison submitted; use status, then collect. Do not submit again.')
"""


def validate_id(run_id):
    if not RUN_ID.fullmatch(run_id):raise ValueError('Use a short lowercase alphanumeric/hyphen run ID')


def host_context(folder):
    plan=json.loads((folder/'driver-plan.json').read_text())
    storage=Path(plan['sql_storage_configuration'])
    return (*context(storage,folder),storage)


def start(folder,run_id,cache,workload,count,limit,repetitions):
    validate_id(run_id)
    if type(repetitions) is not int or not 1<=repetitions<=10:raise ValueError('Use 1..10 paired repetitions')
    local=folder/'runs'/run_id
    if local.exists() and any(local.iterdir()):raise ValueError('Recorded run ID; use status/collect, never resubmit')
    config,backend,sql_plan,plan,storage=host_context(folder)
    if json.loads((folder/'driver-ready.json').read_text())['status']!='DRIVER_READY':raise ValueError('Driver readiness is not complete')
    function,_,digest,_=source_package(cache)
    if digest!=plan['native_package_sha256']:raise ValueError('Function package changed after driver verification')
    selected_trace=trace(plan,workload,count,limit)
    expected=official_answers(ORDER)
    source=Path(__file__).read_bytes();source_hash=hashlib.sha256(source).hexdigest()
    prefix='evaluation/postgresql-azure/'+plan['sql_storage_id']+'/runs/'+run_id+'/'
    settings={'run_id':run_id,'plan':plan,'trace':selected_trace,'expected':expected,
        'repetitions':repetitions,'workload':workload,'blob_prefix':prefix,'runner_sha256':source_hash,
        'methodology':'Same existing SF1 input, alternating backend order, no warmup/retry; validation outside timing'}
    raw=json.dumps(settings,indent=2).encode();settings_hash=hashlib.sha256(raw).hexdigest()
    settings_key=prefix+'settings.json';source_key='evaluation/postgresql-azure/runners/'+source_hash+'.py'
    with backend.control() as cli:
        backend.owned_group(cli,sql_plan)
        verify_live_function(cli,config,sql_plan,function,digest)
        vm=cli.run(['vm','show','--ids',plan['driver_vm_id']])
        if (vm.get('tags') or {}).get('sebs.sql.owner')!=plan['owner'] or vm['tags'].get('sebs.sql.role')!='comparison-driver':
            raise ValueError('Driver VM ownership differs')
        blob=backend.blob(cli,sql_plan)
        blob.get_blob_client('sql',settings_key).upload_blob(raw,overwrite=False)
        blob.get_blob_client('sql',source_key).upload_blob(source,overwrite=True)
        save(local/'settings.json',settings)
        request={'run_id':run_id,'account_url':'https://'+plan['staging_account']+'.blob.core.windows.net',
            'settings_key':settings_key,'settings_sha256':settings_hash,'source_key':source_key,'source_sha256':source_hash}
        operation='submit-'+run_id+'-'+settings_hash[:12]
        save(local/'submission.json',{'operation':operation,'recorded_before_submission':True,'request':request})
        program=SUBMIT.replace('__REQUEST__',repr(request))
        script="set -eu\n"+BASE+"/control-venv/bin/python - <<'SUBMIT_PAIRED_JOB'\n"+program+"\nSUBMIT_PAIRED_JOB\n"
        target=DriverCommands(config,folder,sql_plan,plan['driver_vm_id'])
        result=target.command(operation,script,cli,timeout=180)
        print(result['properties']['instanceView'].get('output',''))
    print('Run:',run_id,'planned backend runs:',2*repetitions,'requests/backend/run:',count)
    print('Expected moderate duration about 20–25 minutes for 3x100/backend. No automatic warmup or retries.')


def status(folder,run_id):
    validate_id(run_id)
    config,backend,sql_plan,plan,_=host_context(folder)
    request={'path':BASE+'/runs/'+run_id}
    program=r"""
import json
from pathlib import Path
request=__REQUEST__
root=Path(request['path']);path=root/'status.json'
if path.exists():
    state=json.loads(path.read_text());print(json.dumps(state,indent=2))
    current=state.get('current')
    if current and (root/current/'requests.json').exists():
        try:
            records=json.loads((root/current/'requests.json').read_text());print('Current phase recorded responses:',len(records))
        except ValueError:print('Current phase records are being updated; check status again later')
else:print('Remote status not created yet; inspect submission/log before assuming it did not start')
log=root/'driver.log'
if log.exists():print(log.read_text()[-1800:])
""".replace('__REQUEST__',repr(request))
    with backend.control() as cli:
        target=DriverCommands(config,folder,sql_plan,plan['driver_vm_id'])
        result=target.command('status-'+run_id+'-'+os.urandom(6).hex(),
            "set -eu\n"+BASE+"/control-venv/bin/python - <<'STATUS_PAIRED_JOB'\n"+program+"\nSTATUS_PAIRED_JOB\n",cli,timeout=120)
        print(result['properties']['instanceView'].get('output',''))
        print(result['properties']['instanceView'].get('error',''))


def collect(folder,run_id):
    validate_id(run_id)
    config,backend,sql_plan,plan,_=host_context(folder)
    local=folder/'runs'/run_id
    settings=json.loads((local/'settings.json').read_text())
    # Export existing VM files only, including partial/failed jobs; never run SQL.
    program=r"""
import json
from pathlib import Path
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
request=__REQUEST__
root=Path(request['path'])
settings=json.loads((root/'settings.json').read_text())
service=BlobServiceClient(request['account_url'],credential=ManagedIdentityCredential())
for path in sorted(root.rglob('*')):
    if path.is_file() and path.suffix in ('.json','.log'):
        with path.open('rb') as stream:service.get_blob_client('sql',settings['blob_prefix']+path.relative_to(root).as_posix()).upload_blob(stream,overwrite=True)
print('Existing artifacts exported. No SQL submitted.')
""".replace('__REQUEST__',repr({'path':BASE+'/runs/'+run_id,
                                 'account_url':'https://'+plan['staging_account']+'.blob.core.windows.net'}))
    with backend.control() as cli:
        target=DriverCommands(config,folder,sql_plan,plan['driver_vm_id'])
        target.command('collect-'+run_id+'-'+os.urandom(6).hex(),
            "set -eu\n"+BASE+"/control-venv/bin/python - <<'COLLECT_PAIRED_JOB'\n"+program+"\nCOLLECT_PAIRED_JOB\n",cli,timeout=180)
        blob=backend.blob(cli,sql_plan);prefix=settings['blob_prefix'];count=0
        for item in blob.get_container_client('sql').list_blobs(name_starts_with=prefix):
            relative=PurePosixPath(item.name[len(prefix):])
            if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe artifact path')
            destination=local/'collected'/Path(*relative.parts);destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_bytes(blob.get_blob_client('sql',item.name).download_blob().readall());count+=1
    print('Downloaded',count,'files to',local/'collected','No SQL submitted.')


def summarize(folder,run_id):
    from statistics import mean,median
    import math
    local=folder/'runs'/run_id;root=local/'collected'
    state=json.loads((root/'status.json').read_text())
    settings=json.loads((local/'settings.json').read_text())
    if state['state']!='complete' or state['completed_runs']!=2*settings['repetitions']:
        raise ValueError('Paired study incomplete; inspect saved status/results')
    report={'workload':settings['workload'],'max_in_flight':settings['trace']['settings']['max-in-flight'],
            'repetitions':settings['repetitions'],'backends':{},'cost':'Native Flex/database/driver costs remain separate'}
    for backend in BACKENDS:
        records=[];durations=[]
        for repetition in range(1,settings['repetitions']+1):
            path=root/f'repeat-{repetition}'/backend
            rows=json.loads((path/'requests.json').read_text())
            if len(rows)!=len(settings['trace']['requests']) or any(row['failure'] or not row['valid'] for row in rows):
                raise ValueError('Incomplete/invalid results in '+str(path))
            records.extend(rows);durations.append(json.loads((path/'summary.json').read_text())['observation_seconds'])
        values=sorted(row['total_latency_s'] for row in records)
        item={'requests':len(records),'valid':len(records),'failures':0,'mean_s':mean(values),'median_s':median(values),
            'p95_s':values[math.ceil(.95*len(values))-1],'queue_mean_s':mean(row['queue_delay_s'] for row in records),
            'mean_run_duration_s':mean(durations),'cold_starts':sum(row.get('cold_start') is True for row in records) if backend=='functions' else None}
        report['backends'][backend]=item
        print(f"{backend:10} valid={len(records)}/{len(records)} mean={item['mean_s']:.3f}s median={item['median_s']:.3f}s "
              f"p95={item['p95_s']:.3f}s queue={item['queue_mean_s']:.3f}s")
    save(local/'comparison-summary.json',report)
    print('Saved:',local/'comparison-summary.json')
    print('Native Azure billing and infrastructure costs remain to be collected; no dollar estimate inferred.')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['start','status','collect','summarize','remote-run'])
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--run-id')
    parser.add_argument('--cache',type=Path,default=Path('cache-postgresql-azure'))
    parser.add_argument('--workload',choices=['moderate','burst'],default='moderate')
    parser.add_argument('--count',type=int,default=100)
    parser.add_argument('--max-in-flight',type=int,default=2)
    parser.add_argument('--repetitions',type=int,default=3)
    args=parser.parse_args()
    if args.command=='remote-run':remote_run(args.state_dir);return
    if not args.run_id:parser.error('--run-id is required')
    if args.command=='start':start(args.state_dir,args.run_id,args.cache,args.workload,args.count,args.max_in_flight,args.repetitions)
    elif args.command=='status':status(args.state_dir,args.run_id)
    elif args.command=='collect':collect(args.state_dir,args.run_id)
    else:summarize(args.state_dir,args.run_id)


if __name__=='__main__':main()
