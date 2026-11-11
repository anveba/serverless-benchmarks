"""Submit one comparison on the existing Azure driver, inspect it and collect results."""
import argparse
import base64
import datetime
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from statistics import mean, median

from azure.storage.blob import BlobServiceClient

from evaluation.sql.azure_configure import vm_command
from evaluation.sql.azure_control import AzureControl
from evaluation.sql.azure_infra import write
from evaluation.sql.service import manifest_fingerprint

RUN_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,47}')

REMOTE_RUNNER = r'''import copy
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from azure.storage.blob import BlobServiceClient

BASE = Path('/opt/sebs-azure')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def workload_trace(source, workload, count):
    intervals = {'moderate': 2, 'burst': 0, 'sparse': 30}
    if workload not in intervals or type(count) is not int or count < 5 or count % 5:
        raise ValueError('Invalid workload or count')
    trace = copy.deepcopy(source)
    order = ['q6', 'q1', 'q12', 'q3', 'q21']
    requests = [{'at': i * intervals[workload], 'query': order[i % 5]} for i in range(count)]
    trace['requests'] = requests
    trace.setdefault('settings', {}).update(requests=copy.deepcopy(requests), **{'max-in-flight': 2})
    trace.update(validation_in_timing=False, warmup=False, automatic_retries=False)
    return trace


def execute(settings, base=BASE):
    (base / 'runs').mkdir(parents=True, exist_ok=True)
    with (base / 'runs/.comparison.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another comparison is already active on this driver') from None
        return execute_locked(settings, base)


def execute_locked(settings, base=BASE):
    root = base / 'runs' / settings['run_id']
    repo = base / 'repo'
    trace_path = repo / 'comparison/trace.json'
    trace = json.loads(trace_path.read_text())
    if digest(trace) != settings['trace_sha256']:
        raise ValueError('Driver trace differs from the local prepared trace')
    if not (base / 'configure-ready.json').exists():
        raise ValueError('Driver application setup is not ready')
    tasks = settings.get('tasks') or [{'id': backend, 'backend': backend} for backend in settings['backends']]
    if len({task['id'] for task in tasks}) != len(tasks):
        raise ValueError('Duplicate task ID')
    if (root / 'run.json').exists() or any((root / task['id']).exists() for task in tasks):
        raise ValueError('Run output already exists; refusing duplicate replay')
    private = json.loads((base / 'private.json').read_text())
    plan = json.loads((repo / 'comparison/private-driver.json').read_text())
    blob = BlobServiceClient.from_connection_string(private['storage_connection_string'])
    container = blob.get_container_client(trace['input']['bucket']['bucket'])
    root.mkdir(parents=True, exist_ok=True)
    (root / 'manifest.json').write_text(json.dumps({'trace': trace, 'settings': settings,
        'region': plan['region'], 'server_vm_size': plan['vm_size'],
        'source_code_sha256': settings['source_code_sha256'],
        'validation_in_timing': False, 'analytical_warmup': False,
        'note': 'Synapse setup and metadata health checks can initialize its endpoint. '
                'See the recorded task plan for repetitions; no claim of matched cold starts or identical hardware.'}, indent=2))
    state = {'run_id': settings['run_id'], 'status': 'running', 'current_backend': None,
             'current_task': None, 'planned_runs': len(tasks),
             'completed_backends': [], 'backend_results': {}, 'started_at':
             datetime.datetime.now(datetime.timezone.utc).isoformat()}

    def save():
        temporary = root / 'run.json.tmp'
        temporary.write_text(json.dumps(state, indent=2) + '\n')
        temporary.replace(root / 'run.json')

    uploaded = {}

    def upload():
        for path in sorted(root.rglob('*')):
            if path.is_file() and path.suffix in ('.json', '.log'):
                identity = (path.stat().st_mtime_ns, path.stat().st_size)
                if uploaded.get(path) == identity:
                    continue
                key = settings['blob_prefix'] + path.relative_to(root).as_posix()
                with path.open('rb') as stream:
                    container.upload_blob(key, stream, overwrite=True)
                uploaded[path] = identity

    save()
    try:
        for task in tasks:
            backend, task_id = task['backend'], task['id']
            selected_trace = trace_path
            if 'workload' in task:
                selected_trace = root / 'schedules' / (task['workload'] + '.json')
                selected_trace.parent.mkdir(exist_ok=True)
                selected_trace.write_text(json.dumps(workload_trace(trace, task['workload'], task['count']), indent=2))
            state.update(current_backend=backend, current_task=task_id)
            save()
            print('BEGIN task=' + task_id, flush=True)
            output = root / task_id
            output.parent.mkdir(parents=True, exist_ok=True)
            with (root / (task_id + '.log')).open('w') as log:
                result = subprocess.run([sys.executable, '-m', 'evaluation.sql.azure_replay',
                    '--backend', backend, '--trace', str(selected_trace), '--output-dir', str(output)],
                    cwd=repo, stdout=log, stderr=subprocess.STDOUT,
                    env={**os.environ, 'HOME': '/root', 'PYTHONUNBUFFERED': '1'})
            summary_path = output / 'summary.json'
            summary = json.loads(summary_path.read_text()) if summary_path.exists() else None
            state['backend_results'][task_id] = {'returncode': result.returncode, 'summary': summary}
            if result.returncode:
                state.update(status='failed', failure='Replay or answer validation failed; inspect saved logs/results.')
                save()
                upload()
                return 1
            state['completed_backends'].append(task_id)
            save()
            upload()
            print('SAVED task=' + task_id, flush=True)
        state.update(status='complete', current_backend=None, current_task=None,
                     finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        save()
        upload()
        print('COMPARISON COMPLETE', flush=True)
        return 0
    except Exception as error:
        message = str(error)
        for value in private.values():
            if isinstance(value, str) and value:
                message = message.replace(value, '[REDACTED]')
        state.update(status='failed', failure=type(error).__name__ + ': ' + message[:2000])
        save()
        print(state['failure'], flush=True)
        return 1


if __name__ == '__main__':
    settings = json.loads(Path(sys.argv[1]).read_text())
    raise SystemExit(execute(settings))
'''


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def job_folder(folder, run_id):
    if not RUN_ID.fullmatch(run_id):
        raise ValueError('run-id must contain lowercase letters, digits and hyphens, at most 48 characters')
    return folder / 'runs' / run_id


def launch_script(settings):
    payload = base64.b64encode(json.dumps(settings).encode()).decode()
    source = base64.b64encode(REMOTE_RUNNER.encode()).decode()
    run_id = settings['run_id']
    if not RUN_ID.fullmatch(run_id):
        raise ValueError('Invalid run-id')
    return f'''set -eu
umask 077
export HOME=/root
/opt/sebs-azure/venv/bin/python - <<'SEBS_WRITE_RUN'
import base64
from pathlib import Path
root = Path('/opt/sebs-azure/runs/{run_id}')
root.mkdir(parents=True, exist_ok=False)
(root / 'args.json').write_bytes(base64.b64decode({payload!r}))
(root / 'runner.py').write_bytes(base64.b64decode({source!r}))
SEBS_WRITE_RUN
systemd-run --unit sebs-sql-run-{run_id} --collect \\
  --setenv=HOME=/root --setenv=PYTHONUNBUFFERED=1 \\
  --working-directory=/opt/sebs-azure/repo \\
  --property=StandardOutput=append:/opt/sebs-azure/runs/{run_id}/job.log \\
  --property=StandardError=append:/opt/sebs-azure/runs/{run_id}/job.log \\
  /opt/sebs-azure/venv/bin/python /opt/sebs-azure/runs/{run_id}/runner.py \\
  /opt/sebs-azure/runs/{run_id}/args.json
echo SEBS_RUN_SUBMITTED
'''


def make_tasks(workload, count=100, repetitions=None):
    defaults = {'moderate': 3, 'burst': 3, 'sparse': 2}
    if workload not in (*defaults, 'all') or type(count) is not int or count < 5 or count % 5:
        raise ValueError('Choose moderate/burst/sparse/all and a positive count divisible by five')
    if repetitions is not None and (type(repetitions) is not int or repetitions < 1):
        raise ValueError('repetitions must be a positive integer')
    tasks = []
    backends = ['vm', 'functions', 'synapse']
    for name in (list(defaults) if workload == 'all' else [workload]):
        for repetition in range(1, (repetitions or defaults[name]) + 1):
            offset = (repetition-1) % len(backends)
            order = backends[offset:] + backends[:offset]
            for backend in order:
                tasks.append({'id': f'{name}/repeat-{repetition}/{backend}', 'backend': backend,
                              'workload': name, 'count': count, 'repetition': repetition})
    return tasks


def start(folder, run_id, workload=None, count=100, repetitions=None):
    out = job_folder(folder, run_id)
    command = out / 'command.json'
    if command.exists():
        raise ValueError('This run was already submitted or its submission is unknown. Use status/collect; do not replay.')
    plan = json.loads((folder / 'comparison.json').read_text())
    trace = json.loads((folder / 'trace.json').read_text())
    if datetime.datetime.fromisoformat(plan['input_sas_expires_at']) <= datetime.datetime.now(datetime.timezone.utc):
        raise ValueError('Synapse input SAS expired; renew application setup before a new measured run')
    for role in ['server', 'driver']:
        ready = json.loads((folder / (role + '-ready.json')).read_text())
        if ready['input_fingerprint'] != manifest_fingerprint(trace['input']) or ready['region'] != plan['region']:
            raise ValueError('Ready state does not match region/input snapshot')
    source = json.loads((folder / 'configure.json').read_text())
    settings = {'run_id': run_id, 'backends': ['vm', 'functions', 'synapse'],
        'blob_prefix': 'evaluation/sql/azure-runs/' + run_id + '/',
        'trace_sha256': digest(trace), 'source_code_sha256': source['sha256'],
        'runner_sha256': hashlib.sha256(REMOTE_RUNNER.encode()).hexdigest()}
    if workload:
        settings['tasks'] = make_tasks(workload, count, repetitions)
        intervals = {'moderate': 2, 'burst': 0, 'sparse': 30}
        minimum = sum((task['count']-1)*intervals[task['workload']] for task in settings['tasks'])
        settings['scheduled_lower_bound_seconds'] = minimum
        expiry = datetime.datetime.fromisoformat(plan['input_sas_expires_at'])
        if datetime.datetime.now(datetime.timezone.utc).timestamp() + minimum >= expiry.timestamp():
            raise ValueError('Synapse SAS expires before the scheduled workload can finish; renew setup first')
        print(f"Planned: {len(settings['tasks'])} backend runs, {len(settings['tasks'])*count} queries; "
              f"arrival schedules alone require {minimum/3600:.2f} hours.", flush=True)
    with AzureControl(plan['subscription_id']) as cli:
        out.mkdir(parents=True, exist_ok=True)
        state = {'status': 'submission_requested', 'settings': settings}
        write(command, state)
        print("Submitting the recorded workload plan on the existing Azure driver...", flush=True)
        output = vm_command(cli, plan, 'driver', launch_script(settings))
        if 'SEBS_RUN_SUBMITTED' not in output:
            print(output[-2500:])
            raise RuntimeError('Submission not confirmed. Inspect status before any new run.')
        state['status'] = 'submitted'
        write(command, state)
        print('Comparison submitted. Use status; do not submit it again.', flush=True)


def status_script(run_id):
    if not RUN_ID.fullmatch(run_id):
        raise ValueError('Invalid run-id')
    return f'''set -eu
export HOME=/root
/opt/sebs-azure/venv/bin/python - <<'SEBS_RUN_STATUS'
import json
from pathlib import Path
root = Path('/opt/sebs-azure/runs/{run_id}')
state = root / 'run.json'
if state.exists():
    data = json.loads(state.read_text())
    print('Status:', data['status'])
    print('Current backend:', data.get('current_backend'))
    print('Current task:', data.get('current_task'))
    print('Completed runs:', len(data.get('completed_backends', [])), '/', data.get('planned_runs', 3))
    for backend, result in list(data.get('backend_results', {{}}).items())[-10:]:
        summary = result.get('summary')
        if summary:
            print(backend, 'valid=' + str(summary['valid']) + '/' + str(summary['submitted']),
                  'failures=' + str(summary['execution_failures']),
                  'duration=' + str(round(summary['observation_seconds'], 2)) + 's')
    if data.get('failure'):
        print('Failure:', data['failure'])
    current = data.get('current_task') or data.get('current_backend')
    if current:
        records = root / current / 'requests.json'
        if records.exists():
            try:
                print('Current backend records:', len(json.loads(records.read_text())))
            except json.JSONDecodeError:
                print('Request checkpoint is being updated; check status again.')
    if data['status'] == 'failed' and current:
        log = root / (current + '.log')
        if log.exists():
            print('Backend log tail:')
            print(log.read_text()[-1200:])
else:
    print('No run status file yet. Inspect job log before submitting anything again.')
    log = root / 'job.log'
    if log.exists():
        print(log.read_text()[-2500:])
SEBS_RUN_STATUS
'''


def status(folder, run_id):
    job_folder(folder, run_id)
    plan = json.loads((folder / 'comparison.json').read_text())
    with AzureControl(plan['subscription_id']) as cli:
        print(vm_command(cli, plan, 'driver', status_script(run_id)), flush=True)


def collect(folder, run_id):
    out = job_folder(folder, run_id)
    settings = json.loads((out / 'command.json').read_text())['settings']
    function = json.loads((folder / 'function.json').read_text())
    with AzureControl(function['subscription_id']) as cli:
        connection = cli.run(['storage', 'account', 'show-connection-string',
            '-g', function['resource_group'], '-n', function['storage_account']])['connectionString']
        container_name = json.loads((folder / 'trace.json').read_text())['input']['bucket']['bucket']
        container = BlobServiceClient.from_connection_string(connection).get_container_client(container_name)
        count = 0
        for blob in container.list_blobs(name_starts_with=settings['blob_prefix']):
            relative = PurePosixPath(blob.name[len(settings['blob_prefix']):])
            if relative.is_absolute() or any(part in ('..', '.') for part in relative.parts):
                raise ValueError('Unsafe result blob path')
            if relative.suffix not in ('.json', '.log'):
                continue
            target = out.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('wb') as stream:
                container.download_blob(blob.name).readinto(stream)
            count += 1
        print('Downloaded', count, 'result files to', out, flush=True)
        if not count:
            print('No uploaded checkpoint yet. Use status; driver-side outputs may still be in progress.')
    if (out / 'run.json').exists():
        print('Saved run status:', json.loads((out / 'run.json').read_text())['status'])


def percentile(values, fraction):
    ordered = sorted(values)
    index = (len(ordered)-1)*fraction
    low, high = math.floor(index), math.ceil(index)
    return ordered[low] + (ordered[high]-ordered[low])*(index-low)


def summarize(folder, run_id):
    out = job_folder(folder, run_id)
    command = json.loads((out / 'command.json').read_text()) if (out / 'command.json').exists() else {}
    if command.get('settings', {}).get('tasks'):
        return summarize_suite(folder, run_id)
    comparison = {}
    for backend in ['vm', 'functions', 'synapse']:
        path = out / backend / 'requests.json'
        summary_path = out / backend / 'summary.json'
        if not path.exists() or not summary_path.exists():
            print(backend + ': no completed local summary; collect after it finishes')
            continue
        records = json.loads(path.read_text())
        summary = json.loads(summary_path.read_text())
        valid = [r for r in records if r.get('valid') and not r['failure']]
        data = {'submitted': summary['submitted'], 'valid': len(valid),
                'execution_failures': summary['execution_failures'],
                'observation_seconds': summary['observation_seconds'],
                'cold_starts': (sum(r.get('cold_start') is True for r in records)
                                if backend == 'functions' else None), 'cost': None}
        if valid:
            latency = [r['total_latency_s'] for r in valid]
            data.update(mean_s=mean(latency), median_s=median(latency), p95_s=percentile(latency, .95),
                        mean_driver_queue_s=mean(r['queue_delay_s'] for r in valid))
            print(f"{backend:9} valid={len(valid)}/{summary['submitted']} failures={summary['execution_failures']} "
                  f"mean={data['mean_s']:.3f}s median={data['median_s']:.3f}s p95={data['p95_s']:.3f}s "
                  f"queue={data['mean_driver_queue_s']:.3f}s duration={summary['observation_seconds']:.2f}s")
        else:
            print(backend + ': no valid successful requests')
        comparison[backend] = data
    write(out / 'comparison-summary.json', comparison)
    print('Dollar costs not computed yet; preserve native meters and full observation windows.')


def summarize_suite(folder, run_id):
    out = job_folder(folder, run_id)
    command = json.loads((out / 'command.json').read_text())
    tasks = command['settings']['tasks']
    report = {'run_id': run_id, 'complete_and_valid': True, 'missing_runs': [], 'workloads': {},
              'p95_method': 'nearest rank, matching evaluation.sql.workloads AWS summaries',
              'cost': None}
    identities = {}
    groups = {}
    for task in tasks:
        path = out / task['id']
        if not all((path / name).exists() for name in ['summary.json', 'requests.json', 'trace.json']):
            report['missing_runs'].append(task['id'])
            report['complete_and_valid'] = False
            continue
        summary = json.loads((path / 'summary.json').read_text())
        records = json.loads((path / 'requests.json').read_text())
        trace = json.loads((path / 'trace.json').read_text())['trace']
        identity = digest({'requests': trace['requests'], 'input': trace['input'],
                           'max-in-flight': trace['settings']['max-in-flight']})
        if task['workload'] in identities and identities[task['workload']] != identity:
            raise ValueError('Backend traces differ for workload ' + task['workload'])
        identities[task['workload']] = identity
        valid = (summary['submitted'] == summary['valid'] == len(records) == task['count']
                 and summary['execution_failures'] == 0
                 and sorted(r['index'] for r in records) == list(range(task['count']))
                 and all(r.get('valid') and not r['failure'] for r in records))
        report['complete_and_valid'] &= valid
        group = groups.setdefault((task['workload'], task['backend']), {'records': [], 'summaries': []})
        group['records'].extend(records)
        group['summaries'].append(summary)
    for workload in ['moderate', 'burst', 'sparse']:
        selected = [(backend, groups[(workload, backend)]) for backend in ['vm', 'functions', 'synapse']
                    if (workload, backend) in groups]
        if not selected:
            continue
        print(workload.upper())
        report['workloads'][workload] = {}
        for backend, group in selected:
            records, summaries = group['records'], group['summaries']
            good = [r for r in records if r.get('valid') and not r['failure']]
            data = {'runs': len(summaries), 'submitted': sum(s['submitted'] for s in summaries),
                    'valid': len(good), 'failures': sum(r['failure'] for r in records),
                    'mean_run_duration_s': mean(s['observation_seconds'] for s in summaries),
                    'observation_seconds': sum(s['observation_seconds'] for s in summaries),
                    'cold_starts': sum(r.get('cold_start') is True for r in records) if backend == 'functions' else None}
            if good:
                values = sorted(r['total_latency_s'] for r in good)
                data.update(mean_latency_s=mean(values), median_latency_s=median(values),
                            p95_latency_s=values[math.ceil(.95*len(values))-1],
                            mean_queue_s=mean(r['queue_delay_s'] for r in good),
                            mean_service_latency_s=mean(r['service_latency_s'] for r in good))
                print(f"  {backend:9} runs={len(summaries)} valid={len(good)}/{data['submitted']} "
                      f"failures={data['failures']} mean={data['mean_latency_s']:.3f}s "
                      f"median={data['median_latency_s']:.3f}s p95={data['p95_latency_s']:.3f}s "
                      f"queue={data['mean_queue_s']:.3f}s duration={data['mean_run_duration_s']:.2f}s")
            report['workloads'][workload][backend] = data
    write(out / 'comparison-summary.json', report)
    print('Complete and valid:', report['complete_and_valid'])
    if report['missing_runs']:
        print('Missing run summaries:', len(report['missing_runs']))
    print('Dollar costs not computed yet; preserve native meters and full observation windows.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'status', 'collect', 'summarize'])
    parser.add_argument('--state-dir', type=Path, default=Path('results/sql-azure-sweden'))
    parser.add_argument('--run-id', default='comparison-01')
    parser.add_argument('--workload', choices=['moderate', 'burst', 'sparse', 'all'],
                        help='Replay AWS-sized traffic experiments; omitted reuses the prepared initial trace')
    parser.add_argument('--count', type=int, default=100, help='Requests per run with --workload')
    parser.add_argument('--repetitions', type=int, help='Override 3 moderate/3 burst/2 sparse repetitions')
    args = parser.parse_args()
    if args.command == 'start':
        start(args.state_dir, args.run_id, args.workload, args.count, args.repetitions)
    else:
        globals()[args.command](args.state_dir, args.run_id)


if __name__ == '__main__':
    main()
