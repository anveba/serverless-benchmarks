"""Synchronize an existing SQL driver to the exact newly deployed SeBS package.

Checks package/source/network identity, preserves the old profile and files, and
uses SSM to update Python sources only. No SQL queries or resource creation.
"""
import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from zipfile import ZipFile
import zlib

import boto3
from botocore.config import Config

from evaluation.sql.postgres_cloud_driver import check_owner
from evaluation.sql.postgres_cloud_preflight import ROOT, SOURCES, verify_lambda, verify_package
from sebs.storage.aws_postgresql import AWSPostgreSQL
from sebs.storage.sql_config import sql_config


REMOTE = r'''
import base64, hashlib, json, os, subprocess, sys, zlib
from pathlib import Path
import boto3

payload=json.loads(zlib.decompress(base64.b64decode(__PAYLOAD__)))
root=Path('/opt/sebs-pg-driver/repo')
prefix=Path('/opt/sebs-pg-driver')
os.chdir(root)
for path in (prefix/'runs').glob('*/status.json'):
    if json.loads(path.read_text()).get('state')=='running':
        raise RuntimeError('A comparison is still running; do not refresh its sources')
plan_path=root/'comparison/preflight.json'
current=json.loads(plan_path.read_text())
if current['lambda_code_sha256'] not in (payload['before']['lambda_code_sha256'],payload['after']['lambda_code_sha256']):
    raise RuntimeError('Driver profile differs from this recorded refresh')
live=boto3.client('lambda',region_name=payload['after']['region']).get_function_configuration(
    FunctionName=payload['after']['lambda_function'])
if live['CodeSha256']!=payload['after']['lambda_code_sha256']:
    raise RuntimeError('Lambda changed again; inspect before applying this refresh')
allowed=__ALLOWED__
if set(payload['files'])!=set(allowed):
    raise RuntimeError('Unexpected refresh source paths')
for name,entry in payload['files'].items():
    if hashlib.sha256(entry['text'].encode()).hexdigest()!=entry['sha256']:
        raise RuntimeError('Refresh source checksum mismatch')
checkpoint=prefix/'refreshes'/payload['label']
checkpoint.mkdir(parents=True,exist_ok=True)

def atomic(path,data):
    temporary=path.with_name(path.name+'.refresh-tmp')
    temporary.write_bytes(data)
    temporary.replace(path)

if not (checkpoint/'before-plan.json').exists():
    (checkpoint/'before-plan.json').write_bytes(plan_path.read_bytes())
    for name in allowed:
        backup=checkpoint/'before'/name
        backup.parent.mkdir(parents=True,exist_ok=True)
        backup.write_bytes((root/name).read_bytes())
try:
    for name,entry in payload['files'].items():
        atomic(root/name,entry['text'].encode())
    atomic(plan_path,json.dumps(payload['after'],indent=2).encode())
    result=subprocess.run([str(prefix/'venv/bin/python'),'-B','-m',
        'evaluation.sql.postgres_cloud_driver','verify-local','--state-dir','comparison'],
        capture_output=True,text=True,env={**os.environ,'HOME':'/root','AWS_DEFAULT_REGION':payload['after']['region']})
    if result.returncode:
        print(result.stderr[-4000:],file=sys.stderr)
        raise RuntimeError('Refreshed driver readiness check failed; previous files restored')
except BaseException:
    for name in allowed:
        atomic(root/name,(checkpoint/'before'/name).read_bytes())
    atomic(plan_path,(checkpoint/'before-plan.json').read_bytes())
    raise
report={'status':'DRIVER_REFRESHED','lambda_code_sha256':payload['after']['lambda_code_sha256'],
    'source_sha256':{name:entry['sha256'] for name,entry in payload['files'].items()},
    'benchmark_queries_submitted':0,'resource_allocation_changed':False}
(checkpoint/'report.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
'''


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n')
    temporary.replace(path)


def allowed_paths():
    return list(SOURCES.values())+[member.replace('function/','driver_function/') for member in SOURCES]


def exact_sources(archive,code_sha,root=ROOT):
    verify_package(archive,code_sha,root)
    result={}
    with ZipFile(archive) as package:
        versions={name.split('psycopg-')[1].split('.dist-info/')[0] for name in package.namelist()
                  if '/psycopg-' in name and name.endswith('.dist-info/METADATA')}
        if versions!={'3.3.9'}:
            raise ValueError('Driver refresh expects deployed Psycopg 3.3.9')
        for member,source in SOURCES.items():
            data=package.read(member)
            entry={'text':data.decode('utf-8'),'sha256':hashlib.sha256(data).hexdigest()}
            result[source]=entry
            result[member.replace('function/','driver_function/')]=entry
    return result


def refresh_script(payload):
    encoded=base64.b64encode(zlib.compress(json.dumps(payload).encode(),9)).decode()
    program=REMOTE.replace('__PAYLOAD__',repr(encoded)).replace('__ALLOWED__',repr(allowed_paths()))
    return ("set -eu\nexport HOME=/root\ncd /opt/sebs-pg-driver/repo\n"
            "/opt/sebs-pg-driver/venv/bin/python -B - <<'REFRESH'\n"+program+'\nREFRESH\n')


def refresh(folder,storage_file,cache,label):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,40}',label):
        raise ValueError('Use a short lowercase refresh label')
    out=folder/'refreshes'/label
    marker=out/'request.json'
    before=json.loads((folder/'preflight.json').read_text())
    instance=json.loads((folder/'instance.json').read_text())
    session=boto3.Session()
    ssm=session.client('ssm',region_name=before['region'],config=Config(retries={'total_max_attempts':1}))
    if marker.exists():
        request=json.loads(marker.read_text())
        if not request.get('command_id'):
            raise ValueError('Refresh submission was ambiguous; inspect SSM before creating another label')
        payload=json.loads((out/'payload.json').read_text())
    else:
        config=sql_config(json.loads(storage_file.read_text())['sql'])
        if config.provider!='aws' or config.region!=before['region']:
            raise ValueError('Use the existing AWS SQL storage attachment/region')
        _,stack,outputs=AWSPostgreSQL(config,session).info()
        if stack['StackId']!=before['stack_id'] or outputs['InstanceId']!=before['server_instance_id']:
            raise ValueError('SQL attachment differs from the measured deployment')
        driver=session.client('cloudformation',region_name=before['region']).describe_stacks(
            StackName=instance['stack_id'])['Stacks'][0]
        check_owner(driver,json.loads((folder/'launch-request.json').read_text()))
        if driver['StackStatus']!='CREATE_COMPLETE':
            raise ValueError('Existing driver stack is not ready')
        live=session.client('lambda',region_name=before['region']).get_function_configuration(FunctionName=before['lambda_function'])
        trace=json.loads((folder/'trace.json').read_text())
        verify_lambda(live,outputs,config,trace)
        archive=cache/'720.sql-postgresql/aws/python/code_package/default/3.12/x64/720.sql-postgresql.zip'
        files=exact_sources(archive,live['CodeSha256'],ROOT)
        after=copy.deepcopy(before)
        after.update(lambda_code_sha256=live['CodeSha256'],lambda_timeout_s=live['Timeout'],
                     source_archive=str(archive.resolve()),runtime_refreshed_at=datetime.now(timezone.utc).isoformat())
        payload={'label':label,'before':before,'after':after,'files':files}
        command=refresh_script(payload)
        if len(command.encode())>24000:
            raise ValueError('Refresh payload exceeds the small SSM transfer limit')
        save(out/'payload.json',payload)
        save(out/'before-preflight.json',before)
        save(out/'after-preflight.json',after)
        save(marker,{'instance_id':instance['instance_id'],'command_id':None})
        response=ssm.send_command(InstanceIds=[instance['instance_id']],DocumentName='AWS-RunShellScript',
            Parameters={'commands':[command]},TimeoutSeconds=120,Comment='SeBS SQL driver source refresh '+label)
        request={'instance_id':instance['instance_id'],'command_id':response['Command']['CommandId']}
        save(marker,request)
    print('Driver refresh command:',request['command_id'],flush=True)
    for _ in range(12):
        try:
            result=ssm.get_command_invocation(CommandId=request['command_id'],InstanceId=request['instance_id'])
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(2)
            continue
        if result['Status'] in ('Pending','InProgress','Delayed'):
            time.sleep(2)
            continue
        save(out/'ssm-result.json',result)
        print('Refresh status:',result['Status'])
        if result['Status']!='Success':
            print(result.get('StandardOutputContent',''))
            print(result.get('StandardErrorContent',''))
            print('Keep the refresh checkpoint; inspect before retrying a new label.')
            return
        report=json.loads(result['StandardOutputContent'].strip())
        if (report.get('status')!='DRIVER_REFRESHED'
                or report.get('lambda_code_sha256')!=payload['after']['lambda_code_sha256']
                or report.get('source_sha256')!={name:entry['sha256'] for name,entry in payload['files'].items()}):
            raise ValueError('Unexpected driver refresh confirmation; profile not updated')
        live=session.client('lambda',region_name=payload['after']['region']).get_function_configuration(
            FunctionName=payload['after']['lambda_function'])
        if live['CodeSha256']!=payload['after']['lambda_code_sha256']:
            raise ValueError('Lambda changed during refresh; inspect before replay')
        save(folder/'preflight.json',payload['after'])
        save(out/'report.json',report)
        print('Existing driver now matches the deployed Lambda package and checkout.')
        print('Benchmark queries submitted: 0. No resources created or resized.')
        print('Previous profile preserved:',out/'before-preflight.json')
        return
    print('Refresh is pending. Repeat the same command to retrieve it; it will not resubmit.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=Path('results/postgresql-aws-cloud'))
    parser.add_argument('--storage-configuration',type=Path,default=Path('results/postgresql-aws-storage.json'))
    parser.add_argument('--cache',type=Path,default=Path('cache-postgresql-aws'))
    parser.add_argument('--label',default='ca-fix-01')
    args=parser.parse_args()
    refresh(args.state_dir,args.storage_configuration,args.cache,args.label)


if __name__=='__main__':
    main()
