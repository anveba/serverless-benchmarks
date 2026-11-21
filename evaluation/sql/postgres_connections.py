"""Inspect concurrent credential/TLS connections on the existing cloud driver.

No benchmark SQL, role changes, database changes or automatic connection retries.
The probe uses the deployed SQL wrapper's real credential/certificate path.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

import boto3
from botocore.config import Config


REMOTE = r'''
import json, os, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import boto3
import psycopg
from driver_function import sql

payload = __PAYLOAD__
plan = json.loads(Path('comparison/preflight.json').read_text())
os.environ['SEBS_SQL_CONFIG'] = json.dumps({
    'type':'postgresql', 'region':plan['region'], 'secret_arn':plan['secret_arn']})
rows = []
for round_index in range(payload['rounds']):
    barrier = Barrier(payload['workers'])
    def check(index):
        settings, stage, connection = {}, 'credentials', None
        item = {'round':round_index+1, 'worker':index, 'ok':False}
        barrier.wait(timeout=30)
        started = time.perf_counter()
        try:
            # New per-attempt wrapper mirrors simultaneous first-use workers.
            settings, hit = sql.sql().credentials()
            stage = 'connect'
            cert = Path(settings['sslrootcert'])
            item['rootcert_bytes_before_connect'] = cert.stat().st_size
            connection = psycopg.connect(**settings, autocommit=True,
                prepare_threshold=None, options='-c default_transaction_read_only=on')
            item['ok'] = True
        except Exception as error:
            item.update(stage=stage, error_type=type(error).__name__)
            if stage == 'connect':
                # Preserve libpq's useful TLS/network detail, without the secret
                # password. Credential-service exceptions are class-only.
                message = str(error).replace(str(settings.get('password','')), '[REDACTED]') if settings.get('password') else str(error)
                item['message'] = message[:1500]
        finally:
            if connection is not None:
                connection.close()
            item['seconds'] = time.perf_counter() - started
        return item
    with ThreadPoolExecutor(max_workers=payload['workers']) as pool:
        rows.extend(pool.map(check, range(payload['workers'])))

report = {'workers':payload['workers'], 'rounds':payload['rounds'],
    'attempts':len(rows), 'successful_connections':sum(row['ok'] for row in rows),
    'benchmark_queries_executed':0, 'automatic_retries':False,
    'note':'Credentials and TLS/authentication only; no benchmark SELECT statements', 'attempts_detail':rows}
report_path = Path('/opt/sebs-pg-driver/connection-checks') / Path(payload['key']).stem / 'report.json'
report_path.parent.mkdir(parents=True,exist_ok=True)
report_path.write_text(json.dumps(report,indent=2)+'\n')
print('REPORT_SAVED_ON_DRIVER',str(report_path),flush=True)
print(json.dumps({key:value for key,value in report.items() if key!='attempts_detail'}),flush=True)
for item in [row for row in rows if not row['ok']][:4]:
    print(json.dumps(item),flush=True)
boto3.client('s3',region_name=plan['region']).put_object(Bucket=payload['bucket'],Key=payload['key'],
    Body=json.dumps(report).encode(),ContentType='application/json')
'''


def script(payload):
    return ("set -eu\nexport HOME=/root\ncd /opt/sebs-pg-driver/repo\n"
            "/opt/sebs-pg-driver/venv/bin/python -B - <<'PROBE'\n"
            + REMOTE.replace('__PAYLOAD__',repr(payload)) + '\nPROBE\n')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value,indent=2)+'\n')


def server_log_script(begin, end):
    source = '''import json, subprocess
request = __REQUEST__
result = subprocess.run(['docker','logs','--since',request['begin'],'--until',request['end'],
    'sebs-postgresql'],capture_output=True,text=True,timeout=30)
if result.returncode:
    raise RuntimeError('Unable to read PostgreSQL container logs')
lines=(result.stdout+'\\n'+result.stderr).splitlines()
errors=[line for line in lines if any(term in line for term in
    ['ERROR:','FATAL:','PANIC:','could not accept SSL','SSL error'])]
print('POSTGRESQL ERROR LINES:',len(errors))
for line in errors[:40]:
    print(line)
info=json.loads(subprocess.check_output(['docker','inspect','sebs-postgresql'],text=True))[0]
print('CONTAINER SHARED MEMORY BYTES:',info['HostConfig']['ShmSize'])
print('CONTAINER MEMORY LIMIT BYTES:',info['HostConfig']['Memory'])
print('CURRENT SHARED MEMORY (after the run):')
print(subprocess.check_output(['docker','exec','sebs-postgresql','df','-h','/dev/shm'],text=True))
print('No benchmark SQL executed. Logs are from the saved failed-run window.')
'''.replace('__REQUEST__',repr({'begin':begin,'end':end}))
    return "set -eu\npython3 - <<'SERVER_LOGS'\n"+source+'\nSERVER_LOGS\n'


def server_logs(folder, run_id, storage_path):
    from sebs.storage.aws_postgresql import AWSPostgreSQL
    from sebs.storage.sql_config import sql_config
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,40}',run_id):
        raise ValueError('Invalid saved run ID')
    root=folder/'runs'/run_id
    status=json.loads((root/'collected/status.json').read_text())
    phase=status.get('current')
    if status.get('state')!='failed' or not phase or not re.fullmatch(r'repeat-[1-9][0-9]*/(postgresql|lambda)',phase):
        raise ValueError('Collect the failed run first; an explicit failed phase is required')
    summary=json.loads((root/'collected'/phase/'summary.json').read_text())
    begin=datetime.fromtimestamp(summary['observation_begin_unix']-10,timezone.utc).isoformat()
    end=datetime.fromtimestamp(summary['observation_end_unix']+10,timezone.utc).isoformat()
    plan=json.loads((folder/'preflight.json').read_text())
    config=sql_config(json.loads(storage_path.read_text())['sql'])
    if config.provider!='aws':
        raise ValueError('Existing AWS PostgreSQL attachment required')
    storage=AWSPostgreSQL(config)
    _, stack, outputs=storage.info()
    if stack['StackId']!=plan['stack_id'] or outputs['InstanceId']!=plan['server_instance_id']:
        raise ValueError('Storage attachment does not match the measured PostgreSQL server')
    command=server_log_script(begin,end)
    key=hashlib.sha256(command.encode()).hexdigest()[:12]
    output=storage.command('evaluation-'+run_id+'-logs-'+key,command,timeout=60)
    (root/'postgresql-server-errors.txt').write_text(output)
    print(output)
    print('Saved:',root/'postgresql-server-errors.txt')


def run(folder, label, workers, rounds):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,40}',label):
        raise ValueError('Use a short lowercase label with letters, digits and hyphens')
    if type(workers) is not int or not 1 <= workers <= 16 or type(rounds) is not int or not 1 <= rounds <= 10:
        raise ValueError('Use 1..16 workers and 1..10 rounds')
    plan=json.loads((folder/'preflight.json').read_text())
    instance=json.loads((folder/'instance.json').read_text())
    artifact=json.loads((folder/'artifact.json').read_text())
    out=folder/'connection-checks'/label
    payload={'workers':workers,'rounds':rounds,'bucket':artifact['bucket'],
             'key':artifact['report_prefix']+'connection-checks/'+label+'.json'}
    command=script(payload)
    identity=hashlib.sha256(json.dumps({'instance':instance['instance_id'],'payload':payload,'script':command},sort_keys=True).encode()).hexdigest()
    marker=out/'request.json'
    ssm=boto3.client('ssm',region_name=plan['region'],config=Config(retries={'total_max_attempts':1}))
    if marker.exists():
        request=json.loads(marker.read_text())
        if request['identity']!=identity:
            raise ValueError('This label belongs to another probe/configuration; preserve it and use a new label')
        if not request.get('command_id'):
            raise ValueError('Previous probe submission is ambiguous; inspect SSM before using a new label')
        command_id=request['command_id']
    else:
        save(marker,{'identity':identity,'command_id':None})
        response=ssm.send_command(InstanceIds=[instance['instance_id']],DocumentName='AWS-RunShellScript',
            Parameters={'commands':[command]},TimeoutSeconds=180,Comment='SeBS connection-only probe '+label)
        command_id=response['Command']['CommandId']
        save(marker,{'identity':identity,'command_id':command_id})
    print('Connection probe command:',command_id,flush=True)
    for _ in range(12):
        try:
            result=ssm.get_command_invocation(CommandId=command_id,InstanceId=instance['instance_id'])
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(2)
            continue
        if result['Status'] in ('Pending','InProgress','Delayed'):
            time.sleep(2)
            continue
        save(out/'ssm-result.json',result)
        print('Probe status:',result['Status'])
        if result['Status']!='Success':
            print(result.get('StandardOutputContent',''))
            print(result.get('StandardErrorContent',''))
            return
        boto3.client('s3',region_name=plan['region']).download_file(payload['bucket'],payload['key'],str(out/'report.json'))
        report=json.loads((out/'report.json').read_text())
        print('Connections:',report['successful_connections'],'/',report['attempts'])
        sizes=sorted({row['rootcert_bytes_before_connect'] for row in report['attempts_detail']
                      if 'rootcert_bytes_before_connect' in row})
        print('Observed CA file sizes before connect:',sizes)
        for row in report['attempts_detail']:
            if not row['ok']:
                print(json.dumps(row,indent=2))
        print('Saved:',out/'report.json')
        print('Benchmark queries executed:',report['benchmark_queries_executed'])
        return
    print('Probe is still pending. Repeat the same command to retrieve it; it will not resubmit.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=Path('results/postgresql-aws-cloud'))
    parser.add_argument('--label',default='connections-8-01')
    parser.add_argument('--workers',type=int,default=8)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--server-run-id',help='Read server errors from the collected failed phase instead of probing connections')
    parser.add_argument('--storage-configuration',type=Path,default=Path('results/postgresql-aws-storage.json'))
    args=parser.parse_args()
    if args.server_run_id:
        server_logs(args.state_dir,args.server_run_id,args.storage_configuration)
    else:
        run(args.state_dir,args.label,args.workers,args.rounds)


if __name__=='__main__':
    main()
