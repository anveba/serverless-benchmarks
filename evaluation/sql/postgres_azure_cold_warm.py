"""AWS-matched SF1 cold/warm samples using native SeBS Azure HTTP experiments."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
from statistics import mean
import subprocess
import sys

from evaluation.sql.postgres_azure_baseline import ROOT, QUERIES, save
from evaluation.sql.postgres_cloud_replay import official_answers, reference_validator


def configuration(base, query, repetitions):
    if query not in QUERIES or type(repetitions) is not int or not 2 <= repetitions <= 30:
        raise ValueError('Use a supported query and 2..30 repetitions')
    config=copy.deepcopy(base)
    experiments=config['experiments']
    experiments.update(type='perf-cost', deployment='azure', update_code=False, update_storage=False,
                       architecture='x64', system_variant='package', runtime={'language':'python','version':'3.11'})
    experiments['benchmark-inputs']={'720.sql-postgresql':{
        'queries':[query], 'connection_policy':'fresh', 'statement_timeout_ms':120000}}
    # Native benchmark default is 512 MiB. Empty sweep avoids republishing code
    # for each query; native enforce_cold_start changes environment only.
    experiments['perf-cost']={'benchmark':'720.sql-postgresql', 'input-size':'small',
        'memory-sizes':[], 'experiments':['cold','warm'], 'trigger':'http', 'repetitions':repetitions,
        'concurrent-invocations':1, 'record-attempts':True, 'fail-on-error':True,
        'max-attempts':3*(repetitions+1)}
    config['deployment']['name']='azure'
    return config


def validate_query(folder, query, expected, repetitions):
    validator=reference_validator(expected)
    phases={}
    for phase in ('cold','warm'):
        path=folder/query/'perf-cost'/(phase+'_results.json')
        data=json.loads(path.read_text())
        retained=[run for group in data['_invocations'].values() for run in group.values()]
        attempts=data['statistics'].get('attempts',[])
        if len(retained)!=repetitions or not attempts:
            raise ValueError('Incomplete samples/attempt ledger: '+query+'/'+phase)
        for run in retained:
            if run['stats']['failure'] or run['stats']['cold_start']!=(phase=='cold'):
                raise ValueError('Failure or wrong classification: '+query+'/'+phase)
        for attempt in attempts:
            run=attempt.get('result')
            if not run or run['stats']['failure']:
                raise ValueError('Failed invocation; inspect saved attempts, no query retried')
            error=validator.validate_output(None,{'scale_factor':1,'queries':[query]},run['output']['result'],'python')
            if error is not None:raise ValueError('Invalid '+query+' answer: '+error)
        phases[phase]=(retained,attempts)
    return phases


def summarize(folder):
    state=json.loads((folder/'status.json').read_text())
    if state['completed_queries']!=len(QUERIES):raise ValueError('Measurements incomplete; inspect status')
    plan=json.loads((folder/'plan.json').read_text())
    expected=json.loads((folder/'expected.json').read_text())
    report={'queries':{}, 'all_attempts':0,
            'scope':'Native function and SQL timings; local client excluded from cloud latency comparisons',
            'cost':'Flex billing not inferred from duration; preserve every attempt for later meter collection'}
    for query in QUERIES:
        report['queries'][query]={}
        for phase,(runs,attempts) in validate_query(folder,query,expected,plan['repetitions']).items():
            measurements=[run['output']['result']['measurement'][query] for run in runs]
            native=[run['provider_times'].get('execution',0) for run in runs]
            item={'samples':len(runs), 'attempts':len(attempts),
                  'function_s':mean(run['times']['benchmark']/1e6 for run in runs),
                  'provider_s':mean(native)/1e6 if all(value>0 for value in native) else None,
                  'sql_s':mean(value['query_time']/1e6 for value in measurements),
                  'credentials_ms':mean(value['credential_time']/1e3 for value in measurements),
                  'connect_ms':mean(value['connect_time']/1e3 for value in measurements)}
            report['queries'][query][phase]=item
            report['all_attempts']+=len(attempts)
            print(f"{query:3} {phase:4} n={len(runs)} function={item['function_s']:.3f}s "
                  f"SQL={item['sql_s']:.3f}s credentials={item['credentials_ms']:.1f}ms connect={item['connect_ms']:.1f}ms")
    save(folder/'summary.json',report)
    print('All invocation attempts:',report['all_attempts'])
    print('Saved:',folder/'summary.json')
    return report


def run(config_path, storage, cache, folder, repetitions):
    if (folder/'status.json').exists():
        raise ValueError('Run already recorded; inspect status or summarize, do not replay')
    if folder.exists() and any(folder.iterdir()):raise ValueError('Use a new output directory')
    base=json.loads(config_path.read_text())
    attachment=json.loads(storage.read_text())['sql']['postgresql']
    azure=base['deployment']['azure']
    if attachment.get('provider')!='azure' or attachment['region']!=azure['region'] or attachment['azure_subscription_id'].lower()!=azure['credentials']['subscription_id'].lower():
        raise ValueError('Storage/deployment provider, subscription or region differ')
    manifests=[json.loads(path.read_text()) for path in (Path(attachment['state_dir'])/'datasets').glob('sebs_720_*.json')]
    recorded=[item for item in manifests if item.get('metadata')=={'scale_factor':1,'input_size':'small','workload':'TPC-H'}]
    if len(recorded)!=1:raise ValueError('Expected one existing measured SF1 dataset report')
    configs={query:configuration(base,query,repetitions) for query in QUERIES}
    expected=official_answers(QUERIES)
    save(folder/'expected.json',expected)
    save(folder/'plan.json',{'queries':list(QUERIES),'repetitions':repetitions,'input_size':'small',
        'connection_policy':'fresh','memory_mb':512,'transport':'http',
        'namespace':recorded[0]['namespace'],'input_fingerprint':recorded[0]['fingerprint'],
        'warmup_policy':'Initial sample per phase excluded, every attempt retained',
        'database_cache':'Persistent; only function cold starts are requested',
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
            'benchmarks/wrappers/common/python/sql.py',
            'benchmarks/700.analytics/720.sql-postgresql/python/function.py',
            'benchmarks/700.analytics/720.sql-postgresql/python/queries.py')}})
    state={'state':'running','completed_queries':0,'total_queries':5,'current':None}
    save(folder/'status.json',state)
    environment=dict(os.environ);environment.pop('SEBS_SQL_SCALE_FACTOR',None)
    try:
        for query,config in configs.items():
            state['current']=query;save(folder/'status.json',state)
            path=folder/'configs'/(query+'.json');save(path,config)
            subprocess.run([sys.executable,'-B','-m','sebs.cli','experiment','invoke','perf-cost',
                '--config',str(path.resolve()),'--deployment','azure',
                '--storage-configuration',str(storage.resolve()),'--cache',str(cache.resolve()),
                '--output-dir',str((folder/query).resolve()),'--verbose'],cwd=ROOT,env=environment,check=True)
            validate_query(folder,query,expected,repetitions)
            state['completed_queries']+=1;save(folder/'status.json',state)
        summarize(folder)
        state.update(state='complete',current=None)
    except BaseException as error:
        state.update(state='failed',error=type(error).__name__+': '+str(error))
        raise
    finally:save(folder/'status.json',state)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['run','status','summarize'])
    parser.add_argument('--config',type=Path)
    parser.add_argument('--storage-configuration',type=Path)
    parser.add_argument('--cache',type=Path)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--repetitions',type=int,default=3)
    args=parser.parse_args()
    if args.command=='run':
        if not all([args.config,args.storage_configuration,args.cache]):parser.error('run requires config, storage and cache')
        run(args.config,args.storage_configuration,args.cache,args.output_dir,args.repetitions)
    elif args.command=='status':print((args.output_dir/'status.json').read_text())
    else:summarize(args.output_dir)


if __name__=='__main__':main()
