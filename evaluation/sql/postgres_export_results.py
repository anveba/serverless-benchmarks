"""Export aggregate PostgreSQL evidence without cloud identities or credentials."""
import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from statistics import mean

from evaluation.sql.postgres_costs import read, save

RUNS = ['moderate-100-2','burst-100-2-ca-fixed','burst-100-8-ca-fixed']
QUERIES = ['q6','q1','q12','q3','q21']
ROOT = Path(__file__).resolve().parents[2]


def export(results, snapshot, output):
    cleanup=read(results/'postgresql-cleanup-final/verification.json')
    if not cleanup['all_checked_resources_removed'] or any(v!='absent' for v in cleanup['findings'].values()):
        raise ValueError('Final cleanup verification is incomplete')
    report={'benchmark':'720.sql-postgresql','exported_at':datetime.now(timezone.utc).isoformat(),
        'scope':'Aggregate evidence only; native result files, resource identities and request IDs remain private',
        'allocation':{'region':'us-east-1','database_instance_type':'m7i-flex.large','database_vcpus':2,
            'database_host_memory_gib':8,'postgres_container_memory_mib':6144,'postgres_container_cpus':2,
            'postgresql':'16.16','psycopg':'3.3.9','lambda_memory_mib':512,'lambda_runtime':'python3.12',
            'lambda_architecture':'x86_64','driver_instance_type':'m7i-flex.large',
            'database_disk_gib':64,'database_disk_iops':3000,'baseline_throughput_mib_s':125},
        'query_ids':QUERIES,'baseline_fresh_reuse':{},'workloads':{},
        'limitations':['Selected TPC-H queries, not an official TPC-H benchmark result',
            'Same external PostgreSQL server on direct and Lambda paths; this does not compare database engines',
            'Burst queue is primarily driver admission waiting under a configured concurrency cap',
            'M7i-flex CPU availability and database/OS cache state can vary',
            'No sparse PostgreSQL workload was measured; the database stays provisioned on both paths',
            'Three samples per cold/warm condition do not establish tail-latency distributions',
            'Cost model has explicit exclusions and conditional uptime assumptions; not an invoice']}
    for policy in ['fresh','reuse']:
        report['baseline_fresh_reuse'][policy]={}
        for query in QUERIES:
            path=results/f'postgresql-aws-sf1-baseline-{policy}'/query/'results.json'
            data=read(path)
            runs=sorted([r for values in data['_invocations'].values() for r in values.values()],key=lambda r:r['times']['client_begin'])
            if len(runs)!=31 or any(r['stats']['failure'] for r in runs):
                raise ValueError('Incomplete native baseline: '+str(path))
            retained=runs[1:]
            measurement=lambda r:r['output']['result']['measurement'][query]
            report['baseline_fresh_reuse'][policy][query]={
                'retained_samples':len(retained),'excluded_initial_samples':1,
                'all_retained_warm':all(not r['stats']['cold_start'] for r in retained),
                'mean_aws_execution_s':mean(r['provider_times']['execution']/1e6 for r in retained),
                'mean_query_fetch_s':mean(measurement(r)['query_time']/1e6 for r in retained),
                'mean_connect_ms':mean(measurement(r)['connect_time']/1e3 for r in retained)}
    for name in RUNS:
        data=read(results/'postgresql-aws-cloud/runs'/name/'comparison-summary.json')
        report['workloads'][name]={'scale_factor':1,'max_in_flight':8 if '-8-' in name else 2,'repetitions_per_backend':3,
            'arrival_spacing_s':2 if name.startswith('moderate') else 0,'backends':{}}
        for backend,value in data['backends'].items():
            if value['requests']!=300 or value['valid']!=300 or value['failures']:
                raise ValueError('Incomplete workload '+name)
            report['workloads'][name]['backends'][backend]={key:value[key] for key in [
                'requests','valid','failures','mean_s','median_s','p95_s','mean_queue_s','mean_run_seconds','per_query']}
            if backend=='lambda':
                if not value['native_billing_complete']:raise ValueError('Missing workload billing')
                report['workloads'][name]['backends'][backend].update({key:value[key] for key in ['cold_starts','billed_gb_seconds']})
    cold=read(results/'postgresql-aws-sf1-cold-warm-3/summary.json')
    fields=['samples','attempts','all_attempts_gb_seconds','aws_execution_s','aws_initialization_ms',
            'query_fetch_s','credentials_ms','connect_ms']
    report['cold_warm']={'scale_factor':1,'connection_policy':'fresh','queries':{
        q:{phase:{key:cold['queries'][q][phase][key] for key in fields} for phase in ['cold','warm']} for q in QUERIES},
        'all_attempts_gb_seconds':cold['all_attempts_gb_seconds'],'database_remained_running':True}
    cost=read(snapshot/'cost-report.json')
    report['cost_model']={'basis':cost['basis'],'rates':cost['rates'],'exclusions':cost['exclusions'],
        'workloads':[{'run_id':r['run_id'],'backends':r['backends']} for r in cost['workloads']],
        'campaign':cost['campaign'], 'known_lambda':{key:cost['known_lambda'][key] for key in [
            'requests_with_native_billing','gb_seconds','coverage_complete','modeled_subtotal_usd','note']}}
    report['cleanup']={'checked_at':cleanup['checked_at'],'all_checked_resources_removed':True,
        'checked_resource_count':len(cleanup['findings']),
        'scope':'Recorded PostgreSQL experiment resources only; other deployments not checked',
        'archived_native_billing_reports':read(results/'postgresql-cleanup-final/archive-complete.json')['reports']}
    names=['benchmarks/700.analytics/720.sql-postgresql/python/function.py',
        'benchmarks/700.analytics/720.sql-postgresql/python/queries.py','benchmarks/wrappers/common/python/sql.py']
    report['exported_checkout_source_sha256']={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    save(output,report)
    print('Saved aggregate-only evidence:',output)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results-dir',type=Path,default=Path('results'))
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('handoff/results/postgresql-aws-summary.json'))
    args=parser.parse_args()
    export(args.results_dir,args.snapshot,args.output)


if __name__=='__main__':main()
