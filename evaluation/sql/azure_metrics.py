"""Preserve Azure execution meters and resource metadata before cleanup; read-only."""
import argparse
import datetime
import json
import math
from pathlib import Path

from evaluation.sql.azure_control import AzureControl
from evaluation.sql.azure_infra import write
from evaluation.sql.azure_run import job_folder


def utc(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def metric_window(summary, padding_minutes=5):
    # Azure emits minute buckets. Retain bucket boundaries and compare native
    # execution count to replay count; never imply exact sub-minute attribution.
    if type(padding_minutes) is not int or padding_minutes < 0:
        raise ValueError('padding-minutes must be a nonnegative integer')
    padding = padding_minutes*60
    return (utc(math.floor(summary['observation_begin_unix']/60)*60-padding),
            utc(math.ceil(summary['observation_end_unix']/60)*60+padding))


def metric_total(response, name):
    values = []
    for metric in response.get('value', []):
        if metric.get('name', {}).get('value') != name:
            continue
        for series in metric.get('timeseries', []):
            for point in series.get('data', []):
                if point.get('total') is not None:
                    values.append(point['total'])
    return sum(values) if values else None


def synapse_delta(data):
    def rows(side):
        return {row['type']: float(row['data_processed_mb'])
                for row in (data.get(side, {}).get('rows') or [])}
    before, after = rows('before'), rows('after')
    changes = {}
    for period in ['daily', 'weekly', 'monthly']:
        if period in before and period in after and after[period] >= before[period]:
            changes[period] = after[period]-before[period]
    return changes


def collect(folder, run_id, padding_minutes=5):
    out = job_folder(folder, run_id)
    meter_dir = out / 'azure-meters'
    meter_dir.mkdir(parents=True, exist_ok=True)
    plan = json.loads((folder / 'comparison.json').read_text())
    function = json.loads((folder / 'function.json').read_text())
    summary_paths = ([out / 'functions/summary.json'] if (out / 'functions/summary.json').exists()
                     else sorted(out.glob('*/repeat-*/functions/summary.json')))
    if not summary_paths:
        raise ValueError('Collect completed Function run summaries before collecting meters')
    summaries = [json.loads(path.read_text()) for path in summary_paths]
    summary = {'submitted': sum(item['submitted'] for item in summaries),
               'observation_begin_unix': min(item['observation_begin_unix'] for item in summaries),
               'observation_end_unix': max(item['observation_end_unix'] for item in summaries)}
    start_time, end_time = metric_window(summary, padding_minutes)
    function_id = (f"/subscriptions/{function['subscription_id']}/resourceGroups/{function['resource_group']}"
                   f"/providers/Microsoft.Web/sites/{function['function_name']}")
    raw, totals = {}, {}
    with AzureControl(plan['subscription_id']) as cli:
        for metric in ['FunctionExecutionCount', 'FunctionExecutionUnits']:
            try:
                response = cli.run(['monitor', 'metrics', 'list', '--resource', function_id,
                    '--metric', metric, '--aggregation', 'Total', '--interval', 'PT1M',
                    '--start-time', start_time, '--end-time', end_time])
                raw[metric] = {'response': response, 'error': None}
                totals[metric] = metric_total(response, metric)
            except RuntimeError as error:
                raw[metric] = {'response': None, 'error': str(error)}
                totals[metric] = None
        write(meter_dir / 'functions-raw.json', raw)
        captured = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for role in ['server', 'driver']:
            info = cli.run(['vm', 'show', '-g', plan['resource_group'],
                '-n', plan[role + '_name'], '--show-details'], missing_ok=True)
            if info is not None:
                metadata = {key: info.get(key) for key in ['id', 'name', 'location', 'timeCreated',
                    'hardwareProfile', 'provisioningState', 'powerState', 'securityProfile']}
                metadata['captured_at'] = captured
                write(meter_dir / (role + '-vm.json'), metadata)
                print(role + ' resource metadata saved.', flush=True)
            else:
                print(role + ' VM absent; any previously saved resource metadata retained.', flush=True)
        resources = cli.run(['resource', 'list', '-g', plan['resource_group']], missing_ok=True)
        if resources is not None:
            write(meter_dir / 'evaluation-resources.json', [
                {key: item.get(key) for key in ['id', 'name', 'type', 'location', 'sku', 'kind']}
                for item in resources])
    count, units = totals['FunctionExecutionCount'], totals['FunctionExecutionUnits']
    result = {'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'function_metric_window_utc': {'start': start_time, 'end': end_time, 'padding_minutes': padding_minutes},
        'expected_function_requests': summary['submitted'], 'function_execution_count': count,
        'count_matches_replay': None if count is None else count == summary['submitted'],
        'function_execution_units_mb_ms': units,
        'function_gb_seconds': units/1024000 if units is not None else None,
        'function_meter_note': 'Azure Monitor native meters, rounded minute window. '
            'Missing data is unknown, not zero. Attribution requires matching request count '
            'and no unrelated function invocations; these meters are estimates, not an invoice.',
        'synapse_aggregate_data_processed_mb_delta': None,
        'synapse_aggregate_total_processed_mb': None,
        'cost_usd': None,
        'cost_note': 'Prices, discounts/free grants, credits and storage/driver charges are not applied. '
            'VM benchmark cost uses its full observation window; actual account usage also includes setup and idle time.'}
    data_paths = ([out / 'synapse/data-processed.json'] if (out / 'synapse/data-processed.json').exists()
                  else sorted(out.glob('*/repeat-*/synapse/data-processed.json')))
    if data_paths:
        deltas = {str(path.relative_to(out)): synapse_delta(json.loads(path.read_text())) for path in data_paths}
        selected = [next((delta[period] for period in ['monthly', 'weekly', 'daily'] if period in delta), None)
                    for delta in deltas.values()]
        result['synapse_aggregate_data_processed_mb_delta'] = (
            next(iter(deltas.values())) if len(deltas) == 1 else deltas)
        if all(value is not None for value in selected):
            result['synapse_aggregate_total_processed_mb'] = sum(selected)
    write(meter_dir / 'usage-summary.json', result)
    print('Functions executions:', count, '(expected', str(summary['submitted']) + ')')
    print('Functions native GB-seconds:', result['function_gb_seconds'])
    print('Synapse aggregate processed MB across saved runs:', result['synapse_aggregate_total_processed_mb'])
    print('Saved:', meter_dir)
    if count is None or units is None or result['count_matches_replay'] is not True:
        print('Function meters are missing or not aligned. Retry this read-only collection later; do not rerun queries.')
    print('No queries submitted or resources changed. Dollar costs remain uncomputed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path('results/sql-azure-sweden'))
    parser.add_argument('--run-id', default='comparison-01')
    parser.add_argument('--padding-minutes', type=int, default=5, help='Reporting-time margin before/after replay; inspect execution-count alignment')
    args = parser.parse_args()
    collect(args.state_dir, args.run_id, args.padding_minutes)


if __name__ == '__main__':
    main()
