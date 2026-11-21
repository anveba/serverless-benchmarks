"""Offline allocation-cost model from saved PostgreSQL experiment evidence."""
from datetime import datetime, timezone
import math
from pathlib import Path

from evaluation.sql.postgres_costs import read, save

MONTH_SECONDS = 30 * 86400
MAIN_RUNS = ['moderate-100-2', 'burst-100-2-ca-fixed', 'burst-100-8-ca-fixed']
EXCLUSIONS = ['Secrets Manager API calls', 'PrivateLink data processing',
    'S3 storage and requests', 'CloudWatch logs/metrics/query usage',
    'network transfer', 'other account resources and taxes']


def timestamp(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Expected a timezone-aware timestamp')
    return value


def rate(catalog, usage, unit):
    """Select exactly one current OnDemand, first paid tier in USD."""
    products = [p for p in catalog['products'] if p['product']['attributes'].get('usagetype') == usage]
    candidates = []
    for product in products:
        for term in product['terms'].get('OnDemand', {}).values():
            for dimension in term['priceDimensions'].values():
                if float(dimension['beginRange']) == 0 and dimension['unit'] == unit:
                    candidates.append({'sku':product['product']['sku'], 'usage_type':usage,
                        'effective_date':term['effectiveDate'], 'unit':unit,
                        'usd_per_unit':float(dimension['pricePerUnit']['USD']),
                        'description':dimension.get('description'), 'rate_code':dimension.get('rateCode')})
    if not candidates:
        raise ValueError('Missing price/unit for ' + usage + ' / ' + unit)
    latest = max(c['effective_date'] for c in candidates)
    candidates = [c for c in candidates if c['effective_date'] == latest]
    if len(candidates) != 1 or candidates[0]['usd_per_unit'] <= 0:
        raise ValueError('Ambiguous/non-positive price for ' + usage)
    return candidates[0]


def rates_from(snapshot, inventory):
    directory = snapshot/'pricing'
    selected = {}
    specs = {'gp3_gb_month':('gp3-storage','EBS:VolumeUsage.gp3','GB-Mo'),
        'gp3_gibps_month':('gp3-throughput','EBS:VolumeP-Throughput.gp3','GiBps-mo'),
        'gp3_iops_month':('gp3-iops','EBS:VolumeP-IOPS.gp3','IOPS-Mo'),
        'lambda_gb_second':('lambda','Lambda-GB-Second','Lambda-GB-Second'),
        'lambda_request':('lambda','Request','Requests'),
        'ipv4_hour':('vpc','USE1-PublicIPv4:InUseAddress','Hrs'),
        'endpoint_hour':('vpc','USE1-VpcEndpoint-Hours','Hrs'),
        'secret_month':('secrets','USE1-AWSSecretsManager-Secrets','Secrets')}
    for resource in inventory['resources'].values():
        kind = resource['instance']['InstanceType']
        specs['compute_'+kind] = ('compute-'+kind, 'BoxUsage:'+kind, 'Hrs')
    for name, (file, usage, unit) in specs.items():
        selected[name] = rate(read(directory/(file+'.json')), usage, unit)
    return selected


def volume_monthly(volume, rates):
    if volume['VolumeType'] != 'gp3':
        raise ValueError('Cost model expects gp3 volumes')
    # Catalog throughput is per GiB/s-month; allocation uses MiB/s.
    return (volume['Size']*rates['gp3_gb_month']['usd_per_unit']
        + max(0, volume['Iops']-3000)*rates['gp3_iops_month']['usd_per_unit']
        + max(0, volume['Throughput']-125)/1024*rates['gp3_gibps_month']['usd_per_unit'])


def allocation_hourly(inventory, rates):
    result = {}
    for name, resource in inventory['resources'].items():
        instance = resource['instance']
        if instance.get('InstanceLifecycle') or instance.get('Placement',{}).get('Tenancy','default') != 'default':
            raise ValueError('Model expects shared-tenancy OnDemand Linux instances')
        result[name] = {'compute':rates['compute_'+instance['InstanceType']]['usd_per_unit'],
            'disk':sum(volume_monthly(v,rates) for v in resource['volumes'])/720,
            'public_ipv4':rates['ipv4_hour']['usd_per_unit'] if instance.get('PublicIpAddress') else 0}
    result['shared'] = {'endpoint':len(inventory['endpoint']['SubnetIds'])*rates['endpoint_hour']['usd_per_unit'],
                        'secret':rates['secret_month']['usd_per_unit']/720}
    return result


def workload(root, run_id, inventory, rates, hourly):
    folder = root/'runs'/run_id/'collected'
    settings, status = read(folder/'settings.json'), read(folder/'status.json')
    if status['state'] != 'complete' or status['completed_runs'] != 2*settings['repetitions']:
        raise ValueError('Incomplete paired run: '+run_id)
    allocation = settings['allocation']
    database = inventory['resources']['database']
    if allocation['instance_id'] != database['instance']['InstanceId'] or allocation['instance_type'] != database['instance']['InstanceType']:
        raise ValueError('Workload and inventory database differ')
    # Price the disk allocation verified when the run was submitted, not today's
    # disk profile if a treatment changed it in between.
    db_hour = hourly['database']['compute'] + hourly['database']['public_ipv4'] + volume_monthly(allocation['volume'],rates)/720
    shared_hour = sum(hourly['shared'].values())
    driver_hour = sum(hourly['driver'].values())
    report = {'run_id':run_id, 'lambda_code_sha256':settings['plan']['lambda_code_sha256'],
        'max_in_flight':settings['trace']['settings']['max-in-flight'],
        'input_fingerprint':settings['trace']['input']['input_fingerprint'], 'backends':{}}
    identity = settings['trace']
    for backend in ['postgresql','lambda']:
        n, seconds, gb_seconds = 0, 0., 0.
        for repetition in range(1, settings['repetitions']+1):
            path = folder/f'repeat-{repetition}'/backend
            summary, rows, bodies = read(path/'summary.json'), read(path/'requests.json'), read(path/'responses.json')
            expected = len(identity['requests'])
            if (read(path/'trace.json')['trace'] != identity or summary['submitted'] != expected
                or summary['valid'] != expected or summary['execution_failures']
                or len(rows) != expected or len(bodies) != expected
                or sorted(r['index'] for r in rows) != list(range(expected))
                or any(r['failure'] or not r['valid'] for r in rows)):
                raise ValueError('Invalid or mismatched saved phase: '+str(path))
            if summary['observation_seconds'] <= 0:
                raise ValueError('Invalid observation window')
            n += expected
            seconds += summary['observation_seconds']
            if backend == 'lambda':
                for body in bodies.values():
                    meter = body.get('provider_metrics',{})
                    if not meter.get('native_report_available') or not meter.get('billed_ms',0):
                        raise ValueError('Missing native Lambda billing in '+str(path))
                    gb_seconds += meter['gb_seconds']
        usage_cost = gb_seconds*rates['lambda_gb_second']['usd_per_unit'] + (n*rates['lambda_request']['usd_per_unit'] if backend=='lambda' else 0)
        components = {'database_compute_disk_ipv4_usd':seconds*db_hour/3600,
            'allocated_endpoint_secret_usd':seconds*shared_hour/3600,
            'lambda_compute_requests_usd':usage_cost}
        total = sum(components.values())
        report['backends'][backend] = {'queries':n, 'observation_seconds':seconds,
            'native_lambda_gb_seconds':gb_seconds, **components, 'modeled_subtotal_usd':total,
            'modeled_subtotal_per_100_queries_usd':total/n*100,
            'driver_instrumentation_usd_separate':seconds*driver_hour/3600}
    return report


def campaign(inventory, rates, hourly, treatment):
    end = timestamp(inventory['captured_at'])
    components = {}
    for name, resource in inventory['resources'].items():
        hours = (end-timestamp(resource['instance']['LaunchTime'])).total_seconds()/3600
        if hours < 0:
            raise ValueError('Snapshot precedes resource launch')
        components[name+'_continuous_compute_assumption'] = hours*hourly[name]['compute']
        components[name+'_continuous_current_public_ipv4_assumption'] = hours*hourly[name]['public_ipv4']
        components[name+'_disk_current_profile'] = sum((end-timestamp(v['CreateTime'])).total_seconds()/MONTH_SECONDS*volume_monthly(v,rates)
                                                         for v in resource['volumes'])
    endpoint_hours = math.ceil((end-timestamp(inventory['endpoint']['CreationTimestamp'])).total_seconds()/3600)
    secret_hours = math.ceil((end-timestamp(inventory['secret']['CreatedDate'])).total_seconds()/3600)
    components['endpoint_provisioned_hours'] = endpoint_hours*hourly['shared']['endpoint']
    components['secret_storage_hours'] = secret_hours*rates['secret_month']['usd_per_unit']/720
    if treatment:
        apply, restore = read(treatment/'apply-response.json'), read(treatment/'restore-response.json')
        volumes = inventory['resources']['database']['volumes']
        if not any(v['VolumeId']==apply['VolumeId']==restore['VolumeId'] for v in volumes):
            raise ValueError('Throughput treatment belongs to another database disk')
        if apply['OriginalThroughput'] != restore['TargetThroughput'] or apply['TargetThroughput'] != restore['OriginalThroughput']:
            raise ValueError('Throughput treatment restoration differs')
        if any(v['Throughput'] != apply['OriginalThroughput'] for v in volumes):
            raise ValueError('Current disk profile is not the treatment baseline')
        start, finish = timestamp(apply['StartTime']), timestamp(restore['StartTime'])
        if not start < finish <= end:
            raise ValueError('Invalid saved throughput billing window')
        delta = max(0,apply['TargetThroughput']-125)-max(0,apply['OriginalThroughput']-125)
        components['recorded_extra_gp3_throughput'] = delta/1024*rates['gp3_gibps_month']['usd_per_unit']*(finish-start).total_seconds()/MONTH_SECONDS
    return {'as_of':inventory['captured_at'], 'components_usd':components,
        'modeled_infrastructure_subtotal_usd':sum(components.values()),
        'conditional_assumptions':['Instances continuously running since saved latest LaunchTime; no historical stop/start verification',
            'Current public IPv4 allocation throughout those running windows',
            'Current volume profile since creation, plus separately recorded throughput treatment',
            '30-day month proration; endpoint partial hours rounded up; public list prices, no free tier/credits/discounts'],
        'actual_invoice_complete':False,
        'note':'Includes setup/idle/maintenance/failure wall time under the stated assumptions. '
            'Not final cleanup time and not a reconciled invoice. Do not add workload allocation subtotals to this uptime model.'}


def known_lambda_usage(results, driver):
    """Deduplicate saved native reports across raw/processed files and warmups.

    This is a known-usage ledger, explicitly not a guarantee every billed call was
    saved (especially interrupted clients). No files are modified.
    """
    entries, missing, interrupted = {}, set(), []
    def add(request_id, gb, source):
        if not request_id:
            return
        if gb is None:
            missing.add(request_id)
            return
        gb = float(gb)
        if gb < 0 or not math.isfinite(gb):
            raise ValueError('Invalid native usage')
        if request_id in entries and abs(entries[request_id]['gb_seconds']-gb)>1e-6:
            raise ValueError('Conflicting native billing for request '+request_id)
        row = entries.setdefault(request_id, {'gb_seconds':gb, 'sources':[]})
        row['sources'].append(str(source))
    for directory in sorted(results.glob('postgresql-aws-*')):
        if not directory.is_dir() or directory.name in ['postgresql-aws-costs','postgresql-aws-cloud']:
            continue
        if (directory/'status.json').exists() and read(directory/'status.json').get('state')=='failed':
            interrupted.append(str(directory))
        for path in directory.rglob('*.json'):
            name = path.name
            if name not in ['experiments.json','results.json','invocations.json','invocations-processed.json'] and not ('_results_' in name):
                continue
            data = read(path)
            if not isinstance(data,dict):
                continue
            runs = [row for group in data.get('_invocations',{}).values() for row in group.values()]
            runs += [a['result'] for a in data.get('statistics',{}).get('attempts',[]) if a.get('result')]
            for run in runs:
                bill = run.get('billing',{})
                add(run.get('request_id'), bill.get('_gb_seconds') if (bill.get('_billed_time') or 0)>0 else None, path)
    for path in (driver/'runs').glob('*/collected/repeat-*/lambda/responses.json'):
        for body in read(path).values():
            meter = body.get('provider_metrics',{})
            add(body.get('request_id'), meter.get('gb_seconds') if meter.get('native_report_available') else None, path)
    for path in (driver/'runs').glob('*/lambda-native-report-recovery.json'):
        for row in read(path)['reports']:
            add(row['request_id'],row['gb_seconds'],path)
    return {'requests_with_native_billing':len(entries),
        'gb_seconds':sum(row['gb_seconds'] for row in entries.values()), 'entries':entries,
        'request_ids_without_saved_billing':sorted(missing-set(entries)),
        'interrupted_or_failed_native_jobs':interrupted,
        'coverage_complete':False,
        'note':'Known saved usage only. Deduplicated by AWS request ID, including excluded initial samples '
            'and recovered failed-trial reports. Unsaved in-flight/interrupted calls and other account usage may be absent.'}


def render(report):
    lines = ['# PostgreSQL AWS cost model', '', report['basis'], '',
        '| Run | Path | Queries | Allocated DB + endpoint + secret | Lambda | Modeled subtotal / 100 | Driver separately |',
        '|---|---|---:|---:|---:|---:|---:|']
    for run in report['workloads']:
        for backend,row in run['backends'].items():
            base = row['database_compute_disk_ipv4_usd']+row['allocated_endpoint_secret_usd']
            lines.append(f"| {run['run_id']} | {backend} | {row['queries']} | ${base:.6f} | "
                f"${row['lambda_compute_requests_usd']:.6f} | ${row['modeled_subtotal_per_100_queries_usd']:.6f} | "
                f"${row['driver_instrumentation_usd_separate']:.6f} |")
    lines += ['', 'Database/shared amounts and driver amounts are totals across the saved phases, '
        'while the subtotal / 100 column is normalized. These are allocated window costs: the existing '
        'endpoint/secret costs are amortized, not separate minimum-hour bills for each short phase.', '',
        f"Infrastructure uptime model through snapshot: ${report['campaign']['modeled_infrastructure_subtotal_usd']:.4f}.",
        'This is conditional on continuous uptime/current allocations; it is not an invoice or final cleanup total.', '',
        f"Known deduplicated Lambda usage: {report['known_lambda']['requests_with_native_billing']} requests, "
        f"{report['known_lambda']['gb_seconds']:.6f} GB-seconds, ${report['known_lambda']['modeled_subtotal_usd']:.6f}.",
        'Known saved usage is incomplete as campaign accounting, especially for unsaved interrupted calls.', '',
        'Excluded/unmetered: '+', '.join(report['exclusions'])+'.', '',
        'Do not add workload allocation subtotals to campaign uptime costs: they overlap.', '',
        '## Campaign model components', '', '| Component | USD |', '|---|---:|']
    for name,value in report['campaign']['components_usd'].items():
        lines.append(f'| {name} | ${value:.6f} |')
    lines += ['', '## Model assumptions', '']+['- '+a for a in report['campaign']['conditional_assumptions']]
    lines += ['', '## Selected catalog rates', '', '| Name | USD per unit | Unit | Effective date |', '|---|---:|---|---|']
    for name,value in report['rates'].items():
        lines.append(f"| {name} | {value['usd_per_unit']:.10f} | {value['unit']} | {value['effective_date']} |")
    return '\n'.join(lines)+'\n'


def generate(snapshot, driver, results, treatment, run_ids=None):
    if not read(snapshot/'pricing-status.json')['complete']:
        raise ValueError('Pricing collection is incomplete')
    inventory = read(snapshot/'inventory.json')
    rates = rates_from(snapshot, inventory)
    hourly = allocation_hourly(inventory, rates)
    workloads = [workload(driver,name,inventory,rates,hourly) for name in (run_ids or MAIN_RUNS)]
    usage = known_lambda_usage(results,driver)
    usage['modeled_subtotal_usd'] = usage['gb_seconds']*rates['lambda_gb_second']['usd_per_unit']+usage['requests_with_native_billing']*rates['lambda_request']['usd_per_unit']
    save(snapshot/'known-lambda-ledger.json',usage)
    report = {'basis':'Saved current public USD list prices, first paid Lambda tier; no free grants, credits or discounts. '
        'Modeled allocated costs; excludes unmetered items and is not an invoice.',
        'snapshot':str(snapshot), 'rates':rates, 'hourly_allocation_usd':hourly,
        'workloads':workloads, 'campaign':campaign(inventory,rates,hourly,treatment),
        'known_lambda':{k:v for k,v in usage.items() if k!='entries'}, 'exclusions':EXCLUSIONS}
    save(snapshot/'cost-report.json',report)
    (snapshot/'cost-report.md').write_text(render(report))
    for run in workloads:
        for backend,row in run['backends'].items():
            print(run['run_id'],backend,'modeled USD/100='+f"{row['modeled_subtotal_per_100_queries_usd']:.6f}",
                'driver USD separate='+f"{row['driver_instrumentation_usd_separate']:.6f}")
    print('Conditional infrastructure uptime model USD:',round(report['campaign']['modeled_infrastructure_subtotal_usd'],4))
    print('Known saved Lambda requests:',usage['requests_with_native_billing'],'GB-seconds:',round(usage['gb_seconds'],6))
    print('Saved:',snapshot/'cost-report.json',snapshot/'cost-report.md')
    print('Offline only. Partial cost model with explicit exclusions; no AWS calls or SQL.')
    return report
