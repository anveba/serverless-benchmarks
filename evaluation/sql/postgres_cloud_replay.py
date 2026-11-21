"""Submit, observe and collect paired direct-PostgreSQL/Lambda cloud replays.

The local CLI never runs workload queries. An explicit start installs a small
verified runner on the existing driver through SSM and starts one systemd unit.
Retries of start never resubmit workload execution. No new resources are created.
"""
import argparse
import base64
import copy
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
from statistics import mean, median
import sys
import threading
import time
import uuid
import zlib

import boto3
from botocore.config import Config

from evaluation.sql.postgres_cloud_driver import PREFIX, check_owner
from evaluation.sql.postgres_cloud_preflight import ROOT, SOURCES
from evaluation.sql.postgres_volume import modification
from sebs.benchmark import load_benchmark_input
from sebs.experiments.sql_workload import validate_trace
from sebs.faas.function import ExecutionResult


UPDATES = ['evaluation/sql/postgres_cloud_replay.py', 'evaluation/sql/lambda_client.py',
           'evaluation/sql/replay_service.py']
BACKENDS = ('postgresql', 'lambda')
INTERVALS = {'moderate': 2, 'burst': 0, 'sparse': 30}
QUERY_ORDER = ('q6', 'q1', 'q12', 'q3', 'q21')
RUN_ID = re.compile(r'[a-z][a-z0-9-]{0,40}')


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, default=json_default) + '\n')
    temporary.replace(path)


def read(path):
    return json.loads(path.read_text())


def workload_trace(original, workload=None, count=None, max_in_flight=None):
    """Keep the prepared input; optionally replace only arrivals and concurrency."""
    trace = copy.deepcopy(original)
    if workload is None:
        if count is not None:
            raise ValueError('--count requires --workload')
    else:
        if workload not in INTERVALS:
            raise ValueError('Unknown workload')
        count = 100 if count is None else count
        if type(count) is not int or count < 1:
            raise ValueError('--count must be a positive integer')
        requests = [{'at': index * INTERVALS[workload], 'query': QUERY_ORDER[index % len(QUERY_ORDER)]}
                    for index in range(count)]
        trace['requests'] = requests
        trace['settings']['requests'] = copy.deepcopy(requests)
        trace['evaluation_workload'] = {'name': workload, 'count': count, 'interval_seconds': INTERVALS[workload]}
    if max_in_flight is not None:
        if type(max_in_flight) is not int or max_in_flight < 1:
            raise ValueError('--max-in-flight must be a positive integer')
        trace['settings']['max-in-flight'] = max_in_flight
    validate_trace(trace['requests'])
    return trace


def official_answers(queries):
    """Export official SF1 answers once; no data generation or cloud SQL queries."""
    import duckdb
    expected = {}
    with duckdb.connect(config={'autoinstall_known_extensions': False}) as con:
        installed = con.execute("SELECT installed FROM duckdb_extensions() WHERE extension_name='tpch'").fetchone()
        # A cached extension may have an HTTP origin. INSTALL FROM HTTPS would
        # reject that origin even though the extension is already usable.
        if not installed or not installed[0]:
            con.execute("INSTALL tpch FROM 'https://extensions.duckdb.org'")
        con.execute('LOAD tpch')
        for query in sorted(set(queries)):
            answer = con.execute('SELECT answer FROM tpch_answers() WHERE scale_factor=1 AND query_nr=?',
                                 [int(query[1:])]).fetchone()
            if answer is None:
                raise ValueError('No official SF1 reference for ' + query)
            expected[query] = list(csv.reader(io.StringIO(answer[0]), delimiter='|'))[1:]
    return expected


def reference_validator(expected):
    # Reuse the benchmark's row ordering, numeric tolerance and error reporting.
    # Only replace the reference provider on this newly loaded module, to avoid
    # generating a second SF1 database on the driver or fetching extensions there.
    validator = load_benchmark_input(str(ROOT / 'benchmarks/700.analytics/710.sql-query'))
    def rows(scale, query):
        if scale != 1:
            raise ValueError('This paired reference is SF1 only')
        return expected[query]
    validator._expected_rows = rows
    return validator


def verify_runtime(plan):
    import psycopg
    if sys.version_info[:2] != (3, 12) or psycopg.__version__ != '3.3.9':
        raise ValueError('Use the verified Python 3.12/Psycopg 3.3.9 driver')
    for member, source in SOURCES.items():
        if (ROOT / member.replace('function/', 'driver_function/')).read_bytes() != (ROOT / source).read_bytes():
            raise ValueError('PostgreSQL sources changed since driver deployment')
    config = boto3.client('lambda', region_name=plan['region']).get_function_configuration(FunctionName=plan['lambda_function'])
    expected_storage = {'type': 'postgresql', 'region': plan['region'], 'secret_arn': plan['secret_arn']}
    if (config['CodeSha256'] != plan['lambda_code_sha256'] or config['MemorySize'] != plan['lambda_memory_mb']
            or config['Runtime'] != 'python3.12' or config['Architectures'] != ['x86_64']
            or config.get('State') != 'Active' or config.get('LastUpdateStatus') != 'Successful'
            or plan['lambda_security_group_id'] not in config.get('VpcConfig', {}).get('SecurityGroupIds', [])
            or plan['subnet_id'] not in config.get('VpcConfig', {}).get('SubnetIds', [])
            or json.loads(config.get('Environment', {}).get('Variables', {}).get('SEBS_SQL_CONFIG', '{}')) != expected_storage):
        raise ValueError('Lambda SQL configuration/source changed since preflight')
    os.environ['SEBS_SQL_CONFIG'] = json.dumps(expected_storage)
    return config


class DirectPostgresClient:
    backend = 'postgresql'
    cold_start_semantics = 'not_applicable; persistent driver with per-worker credential cache'

    def __init__(self, plan, payload):
        self.input = copy.deepcopy(payload)
        self.plan = plan
        self.endpoint = 'private-postgresql://' + payload['namespace']
        self.local = threading.local()

    def health(self):
        return {'engine': 'postgresql', 'input_fingerprint': self.input['input_fingerprint'],
                'connection_policy': self.input['connection_policy'], 'driver_version': '3.3.9',
                'source': 'verbatim deployed benchmark handler and SQL wrapper',
                'server_instance_id': self.plan['server_instance_id']}

    def verify(self, health, trace):
        if trace['input'] != self.input or trace['input'].get('connection_policy') != 'fresh':
            raise ValueError('Direct path must use the same SF1 fresh-connection trace')

    def worker(self):
        if not hasattr(self.local, 'module'):
            from driver_function import sql
            # Each worker has its own wrapper/credential cache, like separate
            # Lambda environments. A shared wrapper lock would serialize queries.
            spec = importlib.util.spec_from_file_location('driver_function.worker_' + uuid.uuid4().hex,
                                                         ROOT / 'driver_function/function.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.client = sql.sql()
            self.local.module = module
        return self.local.module

    def invoke(self, query):
        payload = copy.deepcopy(self.input)
        payload['queries'] = [query]
        begin = datetime.now()
        started = time.perf_counter_ns()
        failure = False
        try:
            # Include result serialization in the direct-path response interval.
            body = json.loads(json.dumps(self.worker().handler(payload)))
        except RuntimeError as error:
            failure = True
            body = {'error': 'Direct PostgreSQL execution failed; no query retried'}
            # The shared wrapper deliberately exposes only stage and exception
            # class. Preserve those, never an arbitrary exception string/DSN.
            detail = re.fullmatch(r'SQL request failed at (credentials|connect|dataset check|query) '
                                  r'\(([A-Za-z][A-Za-z0-9_]*)\); no query was retried', str(error))
            if detail:
                body.update(error=str(error), failure_stage=detail[1], cause_type=detail[2])
        result = ExecutionResult.from_times(begin, datetime.now())
        result.times.client = (time.perf_counter_ns() - started) // 1000
        result.stats.failure, result.stats.cold_start = failure, None
        result.request_id = str(uuid.uuid4())
        result.output = {**body, 'query': query, 'request_id': result.request_id}
        return result


def remote_run(folder):
    from evaluation.sql.lambda_client import LambdaClient
    from evaluation.sql.replay_service import run
    settings = read(folder / 'settings.json')
    if (folder / 'started.json').exists():
        raise ValueError('This run was already started; inspect saved results instead of replaying')
    write(folder / 'started.json', {'started_at': datetime.now(timezone.utc).isoformat()})
    plan, trace = settings['plan'], settings['trace']
    s3 = boto3.client('s3', region_name=plan['region'])
    status = {'state': 'running', 'completed_runs': 0, 'total_runs': 2 * settings['repetitions'], 'current': None}
    write(folder / 'status.json', status)

    def upload():
        for path in sorted(folder.rglob('*.json')):
            s3.upload_file(str(path), settings['artifact']['bucket'],
                           settings['report_prefix'] + path.relative_to(folder).as_posix())

    try:
        verify_runtime(plan)
        validator = reference_validator(settings['expected'])
        for repetition in range(1, settings['repetitions'] + 1):
            order = BACKENDS if repetition % 2 else tuple(reversed(BACKENDS))
            for backend in order:
                status['current'] = f'repeat-{repetition}/{backend}'
                write(folder / 'status.json', status)
                verify_runtime(plan)
                client = (DirectPostgresClient(plan, trace['input']) if backend == 'postgresql'
                          else LambdaClient(plan['lambda_function'], trace['input'], plan['region']))
                good = run(trace, client, folder / status['current'], validator=validator)
                if not good:
                    raise RuntimeError('Incomplete or invalid answers; inspect saved responses; no query retried')
                status['completed_runs'] += 1
                write(folder / 'status.json', status)
                upload()
        status.update(state='complete', current=None)
    except Exception as error:
        status.update(state='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        status['finished_at'] = datetime.now(timezone.utc).isoformat()
        write(folder / 'status.json', status)
        upload()


def database_allocation(plan):
    ec2 = boto3.client('ec2', region_name=plan['region'])
    instance = ec2.describe_instances(InstanceIds=[plan['server_instance_id']])['Reservations'][0]['Instances'][0]
    if instance['State']['Name'] != 'running':
        raise ValueError('PostgreSQL server is not running')
    volume_id = next(item['Ebs']['VolumeId'] for item in instance['BlockDeviceMappings']
                     if item['DeviceName'] == instance['RootDeviceName'])
    volume = ec2.describe_volumes(VolumeIds=[volume_id])['Volumes'][0]
    change = modification(ec2, volume_id)
    if (volume['VolumeType'] != 'gp3' or volume['Throughput'] != 125 or volume['Iops'] != 3000
            or (change and change['ModificationState'] != 'completed')):
        raise ValueError('Wait for completed restoration to gp3 / 125 MiB/s / 3000 IOPS before measuring')
    return {'checked_at': datetime.now(timezone.utc).isoformat(), 'instance_id': instance['InstanceId'],
            'instance_type': instance['InstanceType'], 'availability_zone': instance['Placement']['AvailabilityZone'],
            'volume': {k: volume[k] for k in ('VolumeId', 'VolumeType', 'Size', 'Iops', 'Throughput')},
            'modification': change, 'scope': 'SF1 original primary-key schema; SF10 covering indexes are separate'}


def start_script(settings, sources):
    """Install a frozen runner through SSM; no driver IAM policy change required."""
    run_id = settings['run_id']
    packed = base64.b64encode(zlib.compress(json.dumps({'settings': settings, 'sources': sources}, default=json_default).encode(), 9)).decode()
    folder = PREFIX / 'runs' / run_id
    unit = 'sebs-pg-' + run_id
    python = str(PREFIX / 'venv/bin/python')
    launch = f'exec {shlex.quote(python)} -B -m evaluation.sql.postgres_cloud_replay remote --remote-dir {shlex.quote(str(folder))} > {shlex.quote(str(folder / "runner.log"))} 2>&1'
    return f'''set -eu
export HOME=/root
cd /opt/sebs-pg-driver/repo
test -f /opt/sebs-pg-driver/bootstrap.complete
{python} - <<'INSTALL'
import base64, hashlib, json, zlib
from pathlib import Path
payload = json.loads(zlib.decompress(base64.b64decode({packed!r})))
folder = Path({str(folder)!r})
folder.mkdir(parents=True, exist_ok=False)
allowed = {UPDATES!r}
for name, entry in payload['sources'].items():
    if name not in allowed:
        raise ValueError('Unexpected runner update path')
    data = entry['data'].encode('utf-8')
    if hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise ValueError('Runner update checksum mismatch')
    Path(name).write_bytes(data)
(folder / 'settings.json').write_text(json.dumps(payload['settings'], indent=2))
INSTALL
systemd-run --unit={shlex.quote(unit)} --property=WorkingDirectory=/opt/sebs-pg-driver/repo --setenv=HOME=/root --setenv=AWS_DEFAULT_REGION={shlex.quote(settings['plan']['region'])} /bin/bash -c {shlex.quote(launch)}
echo 'Paired comparison submitted. Do not submit it again.'
'''


def start(folder, run_id, repetitions, workload=None, count=None, max_in_flight=None):
    root = folder / 'runs' / run_id
    marker = root / 'submission.json'
    if marker.exists():
        print('Submission already recorded. Use status; do not replay this run ID.')
        return
    if not 1 <= repetitions <= 10:
        raise ValueError('Use between 1 and 10 paired repetitions')
    plan, trace, instance, artifact = (read(folder / name) for name in ['preflight.json', 'trace.json', 'instance.json', 'artifact.json'])
    trace = workload_trace(trace, workload, count, max_in_flight)
    if trace['input'] != plan['input'] or trace['input']['scale_factor'] != 1 or trace['input']['connection_policy'] != 'fresh':
        raise ValueError('Expected the verified SF1 fresh-connection trace')
    cf = boto3.client('cloudformation', region_name=plan['region'])
    stack = cf.describe_stacks(StackName=instance['stack_id'])['Stacks'][0]
    check_owner(stack, read(folder / 'launch-request.json'))
    if stack['StackStatus'] != 'CREATE_COMPLETE':
        raise ValueError('Driver stack is not ready')
    allocation = database_allocation(plan)
    expected = official_answers([r['query'] for r in trace['requests']])
    sources = {name: {'data': (ROOT/name).read_bytes().decode('utf-8'),
                      'sha256': hashlib.sha256((ROOT/name).read_bytes()).hexdigest()} for name in UPDATES}
    settings = {'run_id': run_id, 'plan': plan, 'trace': trace, 'artifact': artifact,
                'report_prefix': artifact['report_prefix'] + run_id + '/', 'repetitions': repetitions,
                'expected': expected, 'allocation': allocation,
                'source_sha256': {name: entry['sha256'] for name, entry in sources.items()},
                'automatic_query_retries': False, 'validation_in_timing': False,
                'order': 'direct then Lambda on odd repetitions; reversed on even repetitions',
                'cold_start_policy': 'observed; no forced cold starts or warm-up queries'}
    command = start_script(settings, sources)
    if len(command.encode()) > 24000:
        raise ValueError('Runner payload too large for the small SSM submission; do not submit truncated code')
    write(root / 'settings.json', settings)
    # SSM SendCommand has no idempotency token. Persist ambiguity before sending;
    # subsequent starts refuse to submit even if the response was lost.
    write(marker, {'recorded_at': datetime.now(timezone.utc).isoformat(), 'command_id': None,
                   'instance_id': instance['instance_id'], 'outcome': 'submission_pending_or_unknown'})
    ssm = boto3.client('ssm', region_name=plan['region'], config=Config(retries={'total_max_attempts': 1}))
    response = ssm.send_command(InstanceIds=[instance['instance_id']], DocumentName='AWS-RunShellScript',
                               Parameters={'commands': [command]}, TimeoutSeconds=120,
                               Comment='SeBS PostgreSQL paired comparison ' + run_id)
    write(marker, {'command_id': response['Command']['CommandId'], 'instance_id': instance['instance_id']})
    print('Command ID:', response['Command']['CommandId'])
    print('Paired comparison submitted:', repetitions, 'repetitions,', len(trace['requests']), 'requests per backend per repetition.')
    print('Do not submit it again. Use status, then collect and summarize.')


def inspect_remote(folder, run_id):
    plan, instance = read(folder / 'preflight.json'), read(folder / 'instance.json')
    root = folder / 'runs' / run_id
    if not (root / 'submission.json').exists():
        raise ValueError('No recorded submission for this run ID')
    ssm = boto3.client('ssm', region_name=plan['region'], config=Config(retries={'total_max_attempts': 1}))
    submission = read(root / 'submission.json')
    if submission.get('command_id'):
        try:
            result = ssm.get_command_invocation(CommandId=submission['command_id'], InstanceId=instance['instance_id'])
            print('Submission command:', result['Status'])
            if result['Status'] in ('Failed', 'Cancelled', 'TimedOut'):
                print(result.get('StandardErrorContent', ''))
                print(result.get('StandardOutputContent', ''))
        except ssm.exceptions.InvocationDoesNotExist:
            print('Submission command has not appeared yet.')
            return
    remote = PREFIX / 'runs' / run_id
    marker = root / 'inspection.json'
    if not marker.exists():
        command = (f"if test -f {remote}/status.json; then cat {remote}/status.json; "
                   f"else echo STATUS_NOT_WRITTEN_YET; fi\n"
                   f"tail -n 12 {remote}/runner.log 2>/dev/null || true")
        result = ssm.send_command(InstanceIds=[instance['instance_id']], DocumentName='AWS-RunShellScript',
                                  Parameters={'commands': [command]}, TimeoutSeconds=60)
        write(marker, {'command_id': result['Command']['CommandId']})
    try:
        result = ssm.get_command_invocation(CommandId=read(marker)['command_id'], InstanceId=instance['instance_id'])
    except ssm.exceptions.InvocationDoesNotExist:
        print('Read-only inspection submitted; repeat status shortly.')
        return
    if result['Status'] in ('Pending', 'InProgress', 'Delayed'):
        print('Read-only inspection pending; repeat status shortly.')
        return
    write(root / 'last-inspection.json', result)
    marker.unlink()
    print(result.get('StandardOutputContent', ''))
    if result.get('StandardErrorContent'):
        print(result['StandardErrorContent'])


def collect(folder, run_id):
    root = folder / 'runs' / run_id
    settings = read(root / 'settings.json')
    s3 = boto3.client('s3', region_name=settings['plan']['region'])
    prefix = settings['report_prefix']
    count = 0
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=settings['artifact']['bucket'], Prefix=prefix):
        for entry in page.get('Contents', []):
            relative = entry['Key'][len(prefix):]
            parts = Path(relative).parts
            if not parts or Path(relative).is_absolute() or '..' in parts or not relative.endswith('.json'):
                raise ValueError('Unexpected result key under the owned report prefix')
            path = root / 'collected' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(settings['artifact']['bucket'], entry['Key'], str(path))
            count += 1
    print('Downloaded', count, 'result files to', root / 'collected')
    print('No SQL requests submitted.')


def summarize(folder, run_id):
    root = folder / 'runs' / run_id / 'collected'
    settings, status = read(root / 'settings.json'), read(root / 'status.json')
    if status['state'] != 'complete' or status['completed_runs'] != 2 * settings['repetitions']:
        raise ValueError('Comparison is incomplete; inspect status and saved responses')
    report = {'run_id': run_id, 'allocation': settings['allocation'], 'backends': {},
              'cost_note': 'Lambda native usage only; dollar costs and PostgreSQL/driver/infrastructure uptime are separate'}
    identity = json.dumps(settings['trace'], sort_keys=True)
    for backend in BACKENDS:
        records, responses, durations = [], [], []
        for repetition in range(1, settings['repetitions'] + 1):
            directory = root / f'repeat-{repetition}' / backend
            rows, bodies = read(directory / 'requests.json'), read(directory / 'responses.json')
            summary = read(directory / 'summary.json')
            if json.dumps(read(directory / 'trace.json')['trace'], sort_keys=True) != identity:
                raise ValueError('Mismatched input/schedule between backends')
            n = len(settings['trace']['requests'])
            if (len(rows) != n or len(bodies) != n or summary['valid'] != n
                    or sorted(row['index'] for row in rows) != list(range(n))
                    or any(not row.get('valid') or row['failure'] for row in rows)):
                raise ValueError('Incomplete or invalid run: ' + str(directory))
            records.extend(rows)
            responses.extend(bodies.values())
            durations.append(summary['observation_seconds'])
        latency = sorted(row['total_latency_s'] for row in records)
        item = {'requests': len(records), 'valid': len(records), 'failures': 0,
                'mean_s': mean(latency), 'median_s': median(latency),
                'p95_s': latency[max(0, __import__('math').ceil(.95 * len(latency)) - 1)],
                'mean_queue_s': mean(row['queue_delay_s'] for row in records),
                'mean_run_seconds': mean(durations), 'per_query': {}}
        for query in sorted({row['query'] for row in records}):
            subset = [row['total_latency_s'] for row in records if row['query'] == query]
            sql_times = [body['measurement'][query]['query_time']/1e6 for body in responses if body['query'] == query]
            item['per_query'][query] = {'requests': len(subset), 'mean_latency_s': mean(subset), 'mean_sql_fetch_s': mean(sql_times)}
        if backend == 'lambda':
            meters = [body.get('provider_metrics', {}) for body in responses]
            item['cold_starts'] = sum(row['cold_start'] is True for row in records)
            item['native_billing_complete'] = all(m.get('native_report_available') for m in meters)
            item['billed_gb_seconds'] = sum(m['gb_seconds'] for m in meters) if item['native_billing_complete'] else None
        report['backends'][backend] = item
        print(f"{backend:10} valid={len(records)}/{len(records)} mean={item['mean_s']:.3f}s median={item['median_s']:.3f}s "
              f"p95={item['p95_s']:.3f}s queue={item['mean_queue_s']:.3f}s")
        if backend == 'lambda':
            print('           cold_starts=', item['cold_starts'], 'billed_GB_seconds=', item['billed_gb_seconds'])
    write(root.parent / 'comparison-summary.json', report)
    print('Saved:', root.parent / 'comparison-summary.json')
    print(report['cost_note'])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'status', 'collect', 'summarize', 'remote'])
    parser.add_argument('--state-dir', type=Path, default=Path('results/postgresql-aws-cloud'))
    parser.add_argument('--run-id', default='comparison-01')
    parser.add_argument('--repetitions', type=int, default=2)
    parser.add_argument('--workload', choices=list(INTERVALS))
    parser.add_argument('--count', type=int, help='Requests per backend/repetition; default 100 with --workload')
    parser.add_argument('--max-in-flight', type=int, help='Same concurrency limit for both paths; default retains the saved trace')
    parser.add_argument('--remote-dir', type=Path)
    args = parser.parse_args()
    if not RUN_ID.fullmatch(args.run_id):
        parser.error('Run IDs must start with a letter and contain only lowercase letters, digits and hyphens (max 41 characters)')
    if args.action == 'remote':
        if args.remote_dir is None:
            parser.error('--remote-dir is required for internal remote execution')
        remote_run(args.remote_dir)
    elif args.action == 'start':
        start(args.state_dir, args.run_id, args.repetitions, args.workload, args.count, args.max_in_flight)
    elif args.action == 'status':
        inspect_remote(args.state_dir, args.run_id)
    elif args.action == 'collect':
        collect(args.state_dir, args.run_id)
    else:
        summarize(args.state_dir, args.run_id)


if __name__ == '__main__':
    main()
