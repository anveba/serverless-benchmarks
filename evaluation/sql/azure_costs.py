"""Price the saved Azure workload at public USD list rates, without replaying SQL."""
import argparse
import datetime
import json
from pathlib import Path
import urllib.parse
import urllib.request

from evaluation.sql.azure_infra import write
from evaluation.sql.azure_metrics import collect as collect_meters, synapse_delta
from evaluation.sql.azure_run import job_folder

PRICE_API = 'https://prices.azure.com/api/retail/prices'


def fetch_prices(directory, region, vm_size, refresh=False):
    queries = {
        'vm': f"armRegionName eq '{region}' and armSkuName eq '{vm_size}' and priceType eq 'Consumption'",
        'functions': f"armRegionName eq '{region}' and serviceName eq 'Functions' and priceType eq 'Consumption'",
        'synapse': f"armRegionName eq '{region}' and serviceName eq 'Azure Synapse Analytics' and priceType eq 'Consumption'"}
    records = {}
    for name, query in queries.items():
        path = directory / (name + '.json')
        if refresh or not path.exists():
            url = PRICE_API + '?' + urllib.parse.urlencode({'$filter': query, 'currencyCode': "'USD'"})
            pages, current = [], url
            while current:
                with urllib.request.urlopen(current, timeout=30) as response:
                    page = json.load(response)
                pages.append(page)
                current = page.get('NextPageLink')
            combined = dict(pages[0])
            combined['Items'] = [item for page in pages for item in page['Items']]
            combined['NextPageLink'] = None
            write(path, {'source_url': url,
                'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'response': combined})
        records[name] = json.loads(path.read_text())
    return records


def select_rates(records, region, vm_size):
    def one(name, predicate, unit):
        candidates = [item for item in records[name]['response']['Items'] if predicate(item)
                      and item.get('currencyCode') == 'USD' and item.get('armRegionName') == region
                      and item.get('type') == 'Consumption' and item.get('retailPrice', 0) > 0]
        if not candidates:
            raise ValueError('No unambiguous positive retail price found for ' + name)
        date = max(item['effectiveStartDate'] for item in candidates)
        candidates = [item for item in candidates if item['effectiveStartDate'] == date]
        if len(candidates) != 1 or candidates[0]['unitOfMeasure'] != unit:
            raise ValueError('Unexpected or ambiguous price/unit for ' + name)
        return candidates[0]
    vm = one('vm', lambda i: i.get('armSkuName') == vm_size and i.get('skuName') == vm_size
             and 'windows' not in i.get('productName', '').lower(), '1 Hour')
    execution = one('functions', lambda i: i.get('productName') == 'Functions'
                    and i.get('meterName') == 'Standard Execution Time', '1 GB Second')
    requests = one('functions', lambda i: i.get('productName') == 'Functions'
                   and i.get('meterName') == 'Standard Total Executions', '10')
    synapse = one('synapse', lambda i: i.get('productName') == 'Azure Synapse Analytics Serverless SQL Pool'
                  and i.get('meterName') == 'Standard Data Processed', '1 TB')
    return {'vm_usd_per_hour': vm['retailPrice'], 'functions_usd_per_gb_second': execution['retailPrice'],
            'functions_usd_per_request': requests['retailPrice']/10,
            'synapse_usd_per_tb': synapse['retailPrice'],
            'selected_records': {'vm': vm, 'function_execution': execution, 'function_requests': requests, 'synapse': synapse}}


def descriptors(root):
    settings = json.loads((root / 'command.json').read_text())['settings']
    return settings.get('tasks') or [{'id': name, 'backend': name, 'workload': 'initial'}
                                     for name in settings['backends']]


def calculate(root, rates, synapse_mb_per_tb=1000000):
    if synapse_mb_per_tb <= 0:
        raise ValueError('Synapse MB per TB must be positive')
    totals = {name: {'queries': 0, 'observation_seconds': 0., 'cost_usd': None}
              for name in ['vm', 'functions', 'synapse']}
    workloads = {}
    matched_seconds = 0.
    for task in descriptors(root):
        directory = root / task['id']
        summary = json.loads((directory / 'summary.json').read_text())
        if summary['submitted'] != summary['valid'] or summary['execution_failures']:
            raise ValueError('Cannot price an incomplete/invalid comparison as successful: ' + task['id'])
        backend, workload = task['backend'], task.get('workload', 'initial')
        total = totals[backend]
        total['queries'] += summary['submitted']
        total['observation_seconds'] += summary['observation_seconds']
        matched_seconds += summary['observation_seconds']
        group = workloads.setdefault(workload, {}).setdefault(backend, {'runs': 0, 'queries': 0, 'observation_seconds': 0.,
                                                                       'cost_usd': None, 'cost_per_100_queries_usd': None})
        group['runs'] += 1
        group['queries'] += summary['submitted']
        group['observation_seconds'] += summary['observation_seconds']
        if backend == 'vm':
            cost = summary['observation_seconds']*rates['vm_usd_per_hour']/3600
        elif backend == 'synapse':
            delta = synapse_delta(json.loads((directory / 'data-processed.json').read_text()))
            mb = next((delta[p] for p in ['monthly', 'weekly', 'daily'] if p in delta), None)
            if mb is None:
                raise ValueError('No non-resetting Synapse data counter for ' + task['id'])
            cost = mb/synapse_mb_per_tb*rates['synapse_usd_per_tb']
            total['data_processed_mb'] = total.get('data_processed_mb', 0)+mb
            group['data_processed_mb'] = group.get('data_processed_mb', 0)+mb
        else:
            continue
        total['cost_usd'] = (total['cost_usd'] or 0)+cost
        group['cost_usd'] = (group['cost_usd'] or 0)+cost
        group['cost_per_100_queries_usd'] = group['cost_usd']/group['queries']*100
    if len({value['queries'] for value in totals.values()}) != 1:
        raise ValueError('Backend query totals differ; collect all completed runs before comparison')
    usage = json.loads((root / 'azure-meters/usage-summary.json').read_text())
    units = usage.get('function_gb_seconds')
    count = usage.get('function_execution_count')
    function = totals['functions']
    function.update(gb_seconds=units, native_execution_count=count,
                    meter_count_matches_replay=count == function['queries'])
    if units is not None:
        # Request count is independently known from validated responses. The
        # resource meter remains provisional if its execution count mismatches.
        function['cost_usd'] = units*rates['functions_usd_per_gb_second'] + function['queries']*rates['functions_usd_per_request']
    function['status'] = ('native-meter estimate' if function['meter_count_matches_replay'] and units is not None
                          else 'provisional; native meter count/usage incomplete or not aligned')
    for value in totals.values():
        value['cost_per_100_queries_usd'] = value['cost_usd']/value['queries']*100 if value['cost_usd'] is not None else None
    overhead = {'shared_driver_matched_observation_seconds': matched_seconds,
                'shared_driver_matched_window_compute_usd': matched_seconds*rates['vm_usd_per_hour']/3600,
                'vm_continuous_allocation_proxies': {},
                'excluded_unknown': ['OS disks', 'public IPv4', 'Blob storage and operations',
                                     'monitoring/logs', 'network charges', 'taxes'],
                'note': 'Driver overhead is separate from backend comparison. Creation-to-snapshot estimates '
                        'assume continuous allocation; they are not verified billing history.'}
    for role in ['server', 'driver']:
        path = root / 'azure-meters' / (role + '-vm.json')
        if path.exists():
            vm = json.loads(path.read_text())
            if vm.get('timeCreated') and vm.get('captured_at'):
                start = datetime.datetime.fromisoformat(vm['timeCreated'].replace('Z', '+00:00'))
                end = datetime.datetime.fromisoformat(vm['captured_at'].replace('Z', '+00:00'))
                seconds = max(0., (end-start).total_seconds())
                overhead['vm_continuous_allocation_proxies'][role] = {
                    'hours': seconds/3600, 'through_utc': vm['captured_at'],
                    'compute_usd': seconds*rates['vm_usd_per_hour']/3600}
    return {'currency': 'USD', 'pricing_basis': 'Public paid list rates; excludes free grants, credits and discounts',
            'scope': 'VM own observation windows including idle time; Functions execution meters plus validated request count; '
                     'Synapse workspace processed-data deltas. Not total Azure account charges.',
            'rates': rates, 'synapse_mb_per_tb': synapse_mb_per_tb,
            'synapse_conversion_note': 'Explicit decimal MB-to-TB estimate; use --synapse-mb-per-tb 1048576 '
                'to reproduce a binary conversion. Reconcile unit convention against billed usage before claiming invoice accuracy.',
            'totals': totals, 'workloads': workloads, 'overhead': overhead,
            'function_pattern_note': 'Functions per-workload native billing is not attributed from delayed/shared minute buckets; '
                'aggregate native cost is shown. Per-pattern attribution requires finer billing data.'}


def render(report):
    lines = ['# Azure SQL cost evaluation', '', report['pricing_basis'], '', report['scope'], '',
             '| Backend | Queries | Estimated USD | USD per 100 queries |',
             '|---|---:|---:|---:|']
    for name, data in report['totals'].items():
        cost = f"${data['cost_usd']:.6f}" if data['cost_usd'] is not None else 'unknown'
        per = f"${data['cost_per_100_queries_usd']:.6f}" if data['cost_per_100_queries_usd'] is not None else 'unknown'
        label = name + (' (provisional)' if name == 'functions' and not data['meter_count_matches_replay'] else '')
        lines.append(f"| {label} | {data['queries']} | {cost} | {per} |")
    fn = report['totals']['functions']
    lines += ['', f"Functions meter: {fn['native_execution_count']} executions versus {fn['queries']} validated requests; "
              f"{fn['gb_seconds']} GB-seconds. {fn['status']}.", '', report['function_pattern_note'], '',
              '| Workload | VM USD per 100 | Synapse USD per 100 |', '|---|---:|---:|']
    for name, group in report['workloads'].items():
        vm, synapse = group.get('vm', {}).get('cost_per_100_queries_usd'), group.get('synapse', {}).get('cost_per_100_queries_usd')
        if vm is not None and synapse is not None:
            lines.append(f'| {name} | ${vm:.6f} | ${synapse:.6f} |')
    lines += ['', report['synapse_conversion_note'], '', 'Separate experiment overhead:', '',
              f"- Shared driver during all matched observation windows: ${report['overhead']['shared_driver_matched_window_compute_usd']:.6f}."]
    for role, data in report['overhead']['vm_continuous_allocation_proxies'].items():
        lines.append(f"- {role} VM creation-to-snapshot proxy: {data['hours']:.2f} hours, ${data['compute_usd']:.4f} "
                     f"through {data['through_utc']}, assuming continuous allocation.")
    lines += ['- Uncomputed: ' + ', '.join(report['overhead']['excluded_unknown']) + '.', '',
              report['overhead']['note'], '',
              'Rates and raw Azure Retail Prices API responses are saved under pricing/.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path('results/sql-azure-sweden'))
    parser.add_argument('--run-id', default='workloads-100')
    parser.add_argument('--refresh-meters', action='store_true', help='Read native meters again using a padded reporting window; no query replay')
    parser.add_argument('--refresh-prices', action='store_true')
    parser.add_argument('--synapse-mb-per-tb', type=float, default=1000000)
    args = parser.parse_args()
    if args.refresh_meters:
        collect_meters(args.state_dir, args.run_id)
    root = job_folder(args.state_dir, args.run_id)
    plan = json.loads((args.state_dir / 'comparison.json').read_text())
    raw = fetch_prices(root/'pricing', plan['region'], plan['vm_size'], args.refresh_prices)
    report = calculate(root, select_rates(raw, plan['region'], plan['vm_size']), args.synapse_mb_per_tb)
    write(root/'cost-report.json', report)
    (root/'cost-report.md').write_text(render(report))
    print(render(report))
    print('Saved:', root/'cost-report.json', root/'cost-report.md')


if __name__ == '__main__':
    main()
