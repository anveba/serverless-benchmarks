"""Sequential native PostgreSQL baselines, preserving every attempt without replay."""
import argparse
import copy
import json
import os
from pathlib import Path
from statistics import mean
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
QUERIES = ('q6', 'q1', 'q12', 'q3', 'q21')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def configuration(base, query, policy):
    if query not in QUERIES or policy not in ('fresh', 'reuse'):
        raise ValueError('Unsupported query or connection policy')
    result = copy.deepcopy(base)
    experiments = result['experiments']
    experiments.update(update_code=False, update_storage=False)
    experiments.setdefault('benchmark-inputs', {})['720.sql-postgresql'] = {
        'queries': [query], 'connection_policy': policy, 'statement_timeout_ms': 120000}
    return result


def runs(path):
    data = json.loads(path.read_text())
    return sorted((run for group in data['_invocations'].values() for run in group.values()),
                  key=lambda run: run['times']['client_begin'])


def summarize(folder):
    report = {'input_size': 'small', 'invocations_per_cell': 31, 'cells': {},
              'scope': 'Native function and SQL timings; local client excluded from cloud latency comparisons',
              'sampling': 'Initial invocation excluded; cold samples retained separately; warm counts are observed',
              'cost': 'No dollar cost or Flex billing inferred from execution duration'}
    for policy in ('fresh', 'reuse'):
        for query in QUERIES:
            values = runs(folder / policy / query / 'results.json')
            if len(values) != 31 or any(run['stats']['failure'] for run in values):
                raise ValueError('Incomplete/failed cell: ' + policy + '/' + query)
            measured = values[1:]
            warm = [run for run in measured if run['stats']['cold_start'] is False]
            cold = [run for run in measured if run['stats']['cold_start'] is True]
            if len(warm) + len(cold) != 30:
                raise ValueError('Missing cold/warm classification: ' + policy + '/' + query)
            def group_summary(group):
                if not group:
                    return {'samples': 0}
                outputs = [run['output']['result'] for run in group]
                measurements = [value['measurement'][query] for value in outputs]
                native = [run['provider_times'].get('execution', 0) for run in group]
                return {'samples': len(group),
                        'function_s': mean(run['times']['benchmark'] / 1e6 for run in group),
                        'provider_s': mean(native) / 1e6 if all(value > 0 for value in native) else None,
                        'sql_s': mean(value['query_time'] / 1e6 for value in measurements),
                        'connect_ms': mean(value['connect_time'] / 1e3 for value in measurements),
                        'credential_ms': mean(value['credential_time'] / 1e3 for value in measurements),
                        'connections_reused': sum(value['connection_reused'] for value in outputs)}
            item = {'warm': group_summary(warm), 'cold': group_summary(cold),
                    'all_attempts': len(values)}
            report['cells'][policy + '/' + query] = item
            row = item['warm']
            timing = f"{row['function_s']:.3f}s" if row['samples'] else 'unavailable'
            print(f"{policy:5} {query:3} warm={len(warm)}/30 cold={len(cold)} function={timing}")
    save(folder / 'summary.json', report)
    print('Saved:', folder / 'summary.json')


def run(config_path, storage, cache, folder):
    if (folder / 'status.json').exists():
        raise ValueError('Run already recorded; do not replay. Inspect status.json or use summarize.')
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('Use a new output directory; existing files are preserved')
    base = json.loads(config_path.read_text())
    attachment = json.loads(storage.read_text())['sql']['postgresql']
    provider = base['deployment']['name']
    if provider != 'azure' or attachment.get('provider') != provider:
        raise ValueError('This runner currently expects native Azure PostgreSQL configs')
    azure = base['deployment']['azure']
    if attachment['region'] != azure['region'] or attachment['azure_subscription_id'].lower() != azure['credentials']['subscription_id'].lower():
        raise ValueError('SQL attachment and deployment region/subscription differ')
    state = {'state': 'running', 'completed_cells': 0, 'total_cells': 10, 'current': None}
    save(folder / 'plan.json', {'queries': list(QUERIES), 'policies': ['fresh', 'reuse'],
         'scale_factor': 1, 'repetitions': 31, 'memory_mb': 512,
         'initial_sample': 'Retained in raw results, excluded from warm/cold summary',
         'database_cache': 'Persistent; no reset', 'transport': 'http',
         'client_location': 'Local client; not a paired cloud latency experiment'})
    save(folder / 'status.json', state)
    environment = dict(os.environ)
    environment.pop('SEBS_SQL_SCALE_FACTOR', None)
    try:
        for policy in ('fresh', 'reuse'):
            for query in QUERIES:
                state['current'] = policy + '/' + query
                save(folder / 'status.json', state)
                config = folder / 'configs' / (policy + '-' + query + '.json')
                save(config, configuration(base, query, policy))
                output = folder / policy / query
                common = ['--config', str(config.resolve()), '--deployment', 'azure',
                          '--cache', str(cache.resolve()), '--output-dir', str(output.resolve())]
                print('BEGIN', state['current'], '31 sequential invocations', flush=True)
                subprocess.run([sys.executable, '-B', '-m', 'sebs.cli', 'benchmark', 'invoke',
                    '720.sql-postgresql', 'small', *common, '--architecture', 'x64',
                    '--language', 'python', '--language-version', '3.11', '--trigger', 'http',
                    '--repetitions', '31', '--validate', '--storage-configuration', str(storage.resolve())],
                    cwd=ROOT, env=environment, check=True)
                invocation_log = (output / 'out.log').read_text()
                if invocation_log.count('output validation passed') != 31 or 'output validation failed' in invocation_log:
                    raise ValueError('Not all 31 answers validated; inspect preserved invocation log')
                subprocess.run([sys.executable, '-B', '-m', 'sebs.cli', 'benchmark', 'process', *common],
                               cwd=ROOT, env=environment, check=True)
                values = runs(output / 'results.json')
                if len(values) != 31 or any(value['stats']['failure'] for value in values):
                    raise ValueError('Incomplete/failed cell; inspect preserved responses')
                if any(value['output']['result']['connection_policy'] != policy for value in values):
                    raise ValueError('Returned connection policy differs from plan')
                state['completed_cells'] += 1
                save(folder / 'status.json', state)
        summarize(folder)
        state.update(state='complete', current=None)
    except BaseException as error:
        state.update(state='failed', error=type(error).__name__ + ': ' + str(error))
        raise
    finally:
        save(folder / 'status.json', state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'status', 'summarize'])
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--storage-configuration', type=Path)
    parser.add_argument('--cache', type=Path)
    args = parser.parse_args()
    if args.command == 'run':
        if not all([args.config, args.storage_configuration, args.cache]):
            parser.error('run requires --config, --storage-configuration and --cache')
        run(args.config, args.storage_configuration, args.cache, args.output_dir)
    elif args.command == 'status':
        print((args.output_dir / 'status.json').read_text())
    else:
        summarize(args.output_dir)


if __name__ == '__main__':
    main()
