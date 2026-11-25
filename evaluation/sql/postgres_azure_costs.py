"""Collect native Flex meters and regional price evidence; never submit SQL.

The report allocates database/driver VM compute to measured windows. Disks,
network, storage, Key Vault and monitoring are explicitly outside that subtotal.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import urllib.parse
import urllib.request

RUNS = ('moderate-100-2', 'burst-100-2', 'burst-100-8')
METRICS = ('OnDemandFunctionExecutionCount', 'OnDemandFunctionExecutionUnits',
           'AlwaysReadyFunctionExecutionCount', 'AlwaysReadyFunctionExecutionUnits', 'AlwaysReadyUnits')
PRICE_API = 'https://prices.azure.com/api/retail/prices'
METER_SOURCE = 'https://learn.microsoft.com/en-us/azure/azure-functions/monitor-functions-reference'


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def phases(folder, run_ids):
    if not run_ids or len(set(run_ids)) != len(run_ids):
        raise ValueError('Choose distinct run IDs')
    result = []
    origins = set()
    for run_id in run_ids:
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', run_id):
            raise ValueError('Invalid run ID')
        root = folder / 'runs' / run_id
        settings = read(root / 'settings.json')
        status = read(root / 'collected/status.json')
        expected_runs = 2 * settings['repetitions']
        if status.get('state') != 'complete' or status.get('completed_runs') != expected_runs:
            raise ValueError('Collect all completed runs first: ' + run_id)
        plan = settings['plan']
        origins.add((plan['function_app_id'].lower(), plan['server_vm_id'].lower(),
                     plan['driver_vm_id'].lower(), plan['input_fingerprint']))
        paths = sorted((root / 'collected').glob('repeat-*/*/summary.json'))
        expected = {(f'repeat-{n}', b) for n in range(1, settings['repetitions'] + 1)
                    for b in ('postgresql', 'functions')}
        if {(p.parent.parent.name, p.parent.name) for p in paths} != expected:
            raise ValueError('Collected backend/repetition set differs: ' + run_id)
        for path in paths:
            summary = read(path)
            count = len(settings['trace']['requests'])
            if (summary['submitted'] != count or summary['valid'] != count or
                    summary['completed_records'] != count or summary['execution_failures']):
                raise ValueError('Incomplete or invalid phase: ' + str(path))
            begin, end = summary['observation_begin_unix'], summary['observation_end_unix']
            if not end > begin or summary['observation_seconds'] <= 0:
                raise ValueError('Invalid observation window')
            result.append({'run_id': run_id, 'repetition': path.parent.parent.name,
                'backend': path.parent.name, 'summary': summary,
                'metric_start': timestamp(math.floor(begin / 60) * 60),
                'metric_end': timestamp(math.ceil(end / 60) * 60)})
    if len(origins) != 1:
        raise ValueError('Runs use different input or resource snapshots')
    function_windows(result)
    return result, read(folder / 'runs' / run_ids[0] / 'settings.json')['plan']


def function_windows(records, padding_minutes=0):
    if type(padding_minutes) is not int or padding_minutes < 0:
        raise ValueError('Padding must be a nonnegative integer')
    groups = []
    counts = {}
    for phase in sorted((p for p in records if p['backend'] == 'functions'), key=lambda p: p['metric_start']):
        begin = datetime.fromisoformat(phase['metric_start'].replace('Z', '+00:00')).timestamp()
        end = datetime.fromisoformat(phase['metric_end'].replace('Z', '+00:00')).timestamp()
        begin, end = timestamp(begin - padding_minutes * 60), timestamp(end + padding_minutes * 60)
        if groups and begin < groups[-1]['metric_end']:
            group = groups[-1]
            if group['run_id'] != phase['run_id']:
                raise ValueError('Different workload minute windows overlap; cannot attribute native usage')
            group['metric_end'] = max(group['metric_end'], end)
            group['expected_requests'] += phase['summary']['submitted']
            group['repetitions'].append(phase['repetition'])
        else:
            run_id = phase['run_id']; counts[run_id] = counts.get(run_id, 0) + 1
            groups.append({'run_id': run_id, 'window_id': 'window-' + str(counts[run_id]),
                'metric_start': begin, 'metric_end': end,
                'expected_requests': phase['summary']['submitted'], 'repetitions': [phase['repetition']]})
    return groups


def metric_total(response, name):
    metrics = [m for m in (response or {}).get('value', []) if m.get('name', {}).get('value') == name]
    if len(metrics) != 1 or metrics[0].get('errorCode', 'Success') != 'Success':
        return None
    points = [point.get('total') for series in metrics[0].get('timeseries', [])
              for point in series.get('data', []) if point.get('total') is not None]
    if not points or any(not isinstance(p, (float, int)) or not math.isfinite(p) or p < 0 for p in points):
        return None
    return sum(points)


def fetch_prices(directory, key, query):
    url = PRICE_API + '?' + urllib.parse.urlencode({'$filter': query, 'currencyCode': "'USD'"})
    pages, seen, current = [], set(), url
    while current:
        parsed = urllib.parse.urlsplit(current)
        if parsed.scheme != 'https' or parsed.netloc != 'prices.azure.com' or current in seen:
            raise ValueError('Unexpected retail price pagination URL')
        seen.add(current)
        with urllib.request.urlopen(current, timeout=45) as response:
            page = json.load(response)
        pages.append(page)
        current = page.get('NextPageLink')
    save(directory / (key + '.json'), {'source_url': url,
        'fetched_at': datetime.now(timezone.utc).isoformat(), 'pages': pages})


def collect(folder, run_ids, padding_minutes=5):
    from sebs.azure.control import AzureControl
    records, plan = phases(folder, run_ids)
    windows = function_windows(records, padding_minutes)
    region = plan['region']
    if not re.fullmatch('[a-z0-9]+', region):
        raise ValueError('Unexpected ARM region')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = folder / 'cost-evidence' / stamp
    destination.mkdir(parents=True, exist_ok=False)
    save(destination / 'study.json', {'plan': {k: plan[k] for k in (
        'subscription', 'region', 'function_app_id', 'server_vm_id', 'driver_vm_id',
        'function_memory_mb', 'input_fingerprint')}, 'phases': records,
        'metric_windows': windows, 'padding_minutes': padding_minutes, 'meter_source': METER_SOURCE,
        'note': 'Minute-bucket estimates; count alignment is necessary but does not prove exclusive app use.'})
    with AzureControl(plan['subscription']) as cli:
        for phase in windows:
            directory = destination / phase['run_id'] / phase['window_id']
            for name in METRICS:
                try:
                    data = cli.run(['monitor', 'metrics', 'list', '--resource', plan['function_app_id'],
                        '--metric', name, '--aggregation', 'Total', '--interval', 'PT1M',
                        '--start-time', phase['metric_start'], '--end-time', phase['metric_end']])
                    output = {'response': data, 'error': None, 'total': metric_total(data, name)}
                except RuntimeError as error:
                    output = {'response': None, 'error': str(error), 'total': None}
                save(directory / (name + '.json'), output)
            print(phase['run_id'], phase['window_id'], 'Flex meter evidence saved;',
                  'expected executions:', phase['expected_requests'], flush=True)
        sizes = set()
        for role in ('server', 'driver'):
            vm = cli.run(['vm', 'show', '--ids', plan[role + '_vm_id'], '--show-details'])
            safe = {k: vm.get(k) for k in ('id', 'name', 'location', 'timeCreated', 'hardwareProfile',
                'storageProfile', 'powerState', 'provisioningState')}
            save(destination / (role + '-vm.json'), safe)
            sizes.add(vm['hardwareProfile']['vmSize'])
            disk_id = vm['storageProfile']['osDisk']['managedDisk']['id']
            disk = cli.run(['disk', 'show', '--ids', disk_id])
            save(destination / (role + '-disk.json'), {k: disk.get(k) for k in (
                'id', 'name', 'location', 'sku', 'diskSizeGb', 'diskIOPSReadWrite',
                'diskMBpsReadWrite', 'tier', 'timeCreated', 'burstingEnabled')})
        app = cli.run(['rest', '--method', 'get', '--uri',
                       plan['function_app_id'] + '?api-version=2024-04-01'])
        properties = app.get('properties') or {}
        flex = properties.get('functionAppConfig') or {}
        save(destination / 'function-app.json', {**{k: app.get(k) for k in ('id', 'name', 'location', 'kind')},
            **{k: properties.get(k) for k in ('state', 'serverFarmId')},
            'functionAppConfig': {k: flex.get(k) for k in ('runtime', 'scaleAndConcurrency')}})
        resources = cli.run(['resource', 'list', '-g', plan['sql_resource_group']])
        save(destination / 'sql-resources.json', [{k: row.get(k) for k in
             ('id', 'name', 'type', 'location', 'sku', 'kind')} for row in resources])
    for size in sorted(sizes):
        if not re.fullmatch('[A-Za-z0-9_]+', size):
            raise ValueError('Unexpected VM SKU')
        try:
            fetch_prices(destination / 'prices', size,
                f"armRegionName eq '{region}' and armSkuName eq '{size}' and priceType eq 'Consumption'")
        except (OSError, ValueError) as error:
            save(destination / 'prices' / (size + '.json'), {'pages': [], 'error': str(error)})
    try:
        fetch_prices(destination / 'prices', 'functions',
            f"armRegionName eq '{region}' and serviceName eq 'Functions' and priceType eq 'Consumption'")
    except (OSError, ValueError) as error:
        save(destination / 'prices/functions.json', {'pages': [], 'error': str(error)})
    save(destination / 'collection-complete.json', {'completed_at': datetime.now(timezone.utc).isoformat()})
    save(folder / 'latest-cost-evidence.json', {'directory': str(destination.resolve())})
    print('Saved:', destination)
    print('Read-only Azure collection; no SQL, deployments or configuration changes.')
    return destination


def price_rows(path):
    return [row for page in read(path)['pages'] for row in page.get('Items', [])]


def select_rate(rows, region, before, predicate, units):
    candidates = [r for r in rows if r.get('armRegionName') == region and r.get('currencyCode') == 'USD'
        and r.get('type') == 'Consumption' and r.get('retailPrice', 0) > 0
        and r.get('tierMinimumUnits', 0) >= 0 and r.get('effectiveStartDate', '') <= before
        and r.get('unitOfMeasure') in units and predicate(r)]
    if not candidates:
        raise ValueError('No verified regional rate with a recognized unit; inspect saved prices')
    newest = max(r['effectiveStartDate'] for r in candidates)
    candidates = [r for r in candidates if r['effectiveStartDate'] == newest]
    # Azure represents the free grant as a zero-price tier, followed by the
    # positive paid tier. Report marginal public prices before applying grants;
    # do not accidentally reject the paid tier or select a later volume tier.
    first_paid_tier = min(r.get('tierMinimumUnits', 0) for r in candidates)
    candidates = [r for r in candidates if r.get('tierMinimumUnits', 0) == first_paid_tier]
    rates = {r['retailPrice'] / units[r['unitOfMeasure']] for r in candidates}
    if len(rates) != 1:
        raise ValueError('Ambiguous prices; inspect saved records')
    return {'usd_per_unit': rates.pop(), 'records': candidates}


def calculate(directory):
    study = read(directory / 'study.json')
    plan = study['plan']; region = plan['region']; records = study['phases']
    cutoff = min(p['metric_start'] for p in records)
    rates, errors = {}, {}
    for role in ('server', 'driver'):
        size = read(directory / (role + '-vm.json'))['hardwareProfile']['vmSize']
        try:
            rates[role] = select_rate(price_rows(directory / 'prices' / (size + '.json')), region, cutoff,
                lambda r: r.get('skuName') == size and not any(word in
                    (r.get('productName', '') + ' ' + r.get('meterName', '')).lower()
                    for word in ('windows', 'spot', 'low priority')), {'1 Hour': 1})
        except ValueError as error:
            errors[role] = str(error)
    rows = price_rows(directory / 'prices/functions.json')
    def flex(r):
        text = ' '.join(str(r.get(k, '')) for k in ('productName', 'skuName', 'meterName')).lower()
        return 'flex' in text and ('on demand' in text or 'ondemand' in text)
    for name, units in [('function_time', {'1 GB Second': 1}),
                        ('function_requests', {'10': 10, '1': 1, '1M': 1000000, '1 Million': 1000000})]:
        try:
            rates[name] = select_rate(rows, region, cutoff, flex, units)
        except ValueError as error:
            errors[name] = str(error)
    app = read(directory / 'function-app.json')
    config = app.get('functionAppConfig') or {}
    scaling = config.get('scaleAndConcurrency') or {}
    always = scaling.get('alwaysReady')
    on_demand_only = (isinstance(scaling.get('instanceMemoryMB'), (int, float))
                      and scaling['instanceMemoryMB'] == plan['function_memory_mb']
                      and (always is None or isinstance(always, list) and
                           all(isinstance(r.get('instanceCount'), (int, float)) and
                               r['instanceCount'] == 0 for r in always)))
    report = {'rates': rates, 'price_errors': errors, 'workloads': {}, 'missing_meters': [],
        'scope': 'Measured-window VM compute plus native Flex execution/requests, before free grants/credits. '
            'Same PostgreSQL server cost in both paths. Driver compute separate. Not an invoice or full infrastructure total.',
        'excluded': ['OS/data disks and transactions', 'public IP and network transfer', 'Key Vault and API operations',
                     'Blob storage/operations', 'monitoring/logs', 'setup, inter-run idle and failed attempts outside these windows',
                     'taxes'],
        'attribution': 'Minute buckets and matching counts; assumes no unrelated executions in those buckets. '
            'Current app configuration is not historical billing evidence.',
        'source': str(directory), 'meter_source': METER_SOURCE}
    for phase in records:
        run_id, backend, summary = phase['run_id'], phase['backend'], phase['summary']
        entry = report['workloads'].setdefault(run_id, {}).setdefault(backend,
            {'requests': 0, 'observation_seconds': 0, 'database_compute_usd': 0.0,
             'driver_compute_usd_separate': 0.0, 'function_gb_seconds': 0.0,
             'function_execution_usd': 0.0, 'complete_meter_alignment': True})
        entry['requests'] += summary['submitted']; entry['observation_seconds'] += summary['observation_seconds']
        for role, field in [('server', 'database_compute_usd'), ('driver', 'driver_compute_usd_separate')]:
            if role not in rates:
                entry[field] = None
            elif entry[field] is not None:
                entry[field] += summary['observation_seconds'] / 3600 * rates[role]['usd_per_unit']
    for window in study['metric_windows']:
        entry = report['workloads'][window['run_id']]['functions']
        meters = directory / window['run_id'] / window['window_id']
        count = read(meters / 'OnDemandFunctionExecutionCount.json')['total']
        units = read(meters / 'OnDemandFunctionExecutionUnits.json')['total']
        ready = [read(meters / (m + '.json'))['total'] for m in METRICS[2:]]
        valid = (count == window['expected_requests'] and units is not None and units > 0
                 and on_demand_only and not any(v is not None and v > 0 for v in ready))
        if not valid:
            entry['complete_meter_alignment'] = False
            entry['function_gb_seconds'] = None
            report['missing_meters'].append({'run_id': window['run_id'], 'window_id': window['window_id'],
                'expected': window['expected_requests'], 'count': count, 'units': units,
                'always_ready_totals': ready, 'current_on_demand_only': on_demand_only})
        elif entry['function_gb_seconds'] is not None:
            entry['function_gb_seconds'] += units / 1024000
    for backends in report['workloads'].values():
        for backend, entry in backends.items():
            if backend == 'functions':
                if entry['function_gb_seconds'] is None or any(k not in rates for k in ('function_time', 'function_requests')):
                    entry['function_execution_usd'] = None
                else:
                    entry['function_execution_usd'] = (entry['function_gb_seconds'] * rates['function_time']['usd_per_unit']
                        + entry['requests'] * rates['function_requests']['usd_per_unit'])
            a, b = entry['database_compute_usd'], entry['function_execution_usd']
            entry['backend_compute_usd'] = None if a is None or b is None else a + b
            entry['compute_usd_per_100'] = (entry['backend_compute_usd'] / entry['requests'] * 100
                                          if entry['backend_compute_usd'] is not None else None)
    return report


def report(directory):
    result = calculate(directory)
    save(directory / 'cost-report.json', result)
    lines = ['# Azure PostgreSQL measured-window compute costs', '', result['scope'], '',
             '| Study | Path | Requests | Native GB-s | Compute USD / 100 |',
             '|---|---|---:|---:|---:|']
    def fmt(value):
        return 'unknown' if value is None else f'{value:.6f}'
    for run_id, backends in result['workloads'].items():
        for backend, entry in backends.items():
            line = f"| {run_id} | {backend} | {entry['requests']} | {fmt(entry['function_gb_seconds']) if backend == 'functions' else '—'} | {fmt(entry['compute_usd_per_100'])} |"
            lines.append(line); print(line)
    lines += ['', 'Excluded: ' + '; '.join(result['excluded']) + '.', '', result['attribution'], '',
              'Raw regional price records and native metric responses are preserved alongside this report.']
    (directory / 'cost-report.md').write_text('\n'.join(lines) + '\n')
    if result['price_errors']:
        print('Prices need inspection:', json.dumps(result['price_errors']))
        print('Saved function price candidates:', json.dumps([
            {k: r.get(k) for k in ('productName', 'skuName', 'meterName', 'unitOfMeasure', 'retailPrice', 'effectiveStartDate')}
            for r in price_rows(directory / 'prices/functions.json')]))
    if result['missing_meters']:
        print('Missing/mismatched Flex meters:', json.dumps(result['missing_meters']))
        print('Retry collection later; do not rerun SQL.')
    print('Saved:', directory / 'cost-report.json', directory / 'cost-report.md')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('collect', 'report'))
    parser.add_argument('--state-dir', type=Path, default=Path('results/postgresql-azure-cloud'))
    parser.add_argument('--run-ids', nargs='+', default=list(RUNS))
    parser.add_argument('--evidence-dir', type=Path)
    parser.add_argument('--padding-minutes', type=int, default=5,
                        help='Before/after meter-window margin; overlapping buckets are merged, never counted twice')
    args = parser.parse_args()
    if args.command == 'collect':
        directory = collect(args.state_dir, args.run_ids, args.padding_minutes)
    else:
        directory = args.evidence_dir or Path(read(args.state_dir / 'latest-cost-evidence.json')['directory'])
    report(directory)


if __name__ == '__main__':
    main()
