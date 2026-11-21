"""Run native SeBS per-query cold/warm measurements against existing PostgreSQL.

Uses perf-cost with library triggers, complete attempt records and bounded,
fail-fast sampling. The cloud-driver latency comparison is a separate dataset;
this step reports native AWS and in-function timings, not WSL client latency.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
from statistics import mean
import subprocess
import sys
import os

from evaluation.sql.postgres_cloud_preflight import ROOT, SOURCES
from evaluation.sql.postgres_cloud_replay import official_answers, reference_validator


QUERIES=('q6','q1','q12','q3','q21')


def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data,indent=2)+'\n')
    temporary.replace(path)


def configuration(base,query,repetitions):
    if query not in QUERIES or type(repetitions) is not int or not 2 <= repetitions <= 30:
        raise ValueError('Use a supported query and 2..30 repetitions per cold/warm group')
    config=copy.deepcopy(base)
    experiments=config['experiments']
    experiments.update(type='perf-cost',deployment='aws',architecture='x64',system_variant='package',
                       update_code=False,update_storage=False,runtime={'language':'python','version':'3.12'})
    experiments['benchmark-inputs']={'720.sql-postgresql':{'queries':[query],'connection_policy':'fresh',
                                                          'statement_timeout_ms':120000}}
    experiments['perf-cost']={'benchmark':'720.sql-postgresql','input-size':'small',
        'memory-sizes':[512],'experiments':['cold','warm'],'trigger':'library','repetitions':repetitions,
        'concurrent-invocations':1,'record-attempts':True,'fail-on-error':True,'max-attempts':3*(repetitions+1)}
    config['deployment']['name']='aws'
    config['deployment']['aws'].setdefault('region','us-east-1')
    return config


def validate_query(folder,query,expected,repetitions):
    validator=reference_validator(expected)
    records=[]
    for phase in ('cold','warm'):
        data=json.loads((folder/query/'perf-cost'/(phase+'_results_512.json')).read_text())
        retained=[run for values in data['_invocations'].values() for run in values.values()]
        attempts=data['statistics'].get('attempts',[])
        if len(retained)!=repetitions or not attempts:
            raise ValueError('Incomplete samples/attempt ledger for '+query+'/'+phase)
        for run in retained:
            if run['stats']['failure'] or run['stats']['cold_start']!=(phase=='cold'):
                raise ValueError('Failure or wrong cold/warm classification in '+query+'/'+phase)
        for attempt in attempts:
            run=attempt.get('result')
            if not run or run['stats']['failure']:
                raise ValueError('Invocation failed; inspect the preserved attempt ledger')
            error=validator.validate_output(None,{'scale_factor':1,'queries':[query]},run['output']['result'],'python')
            if error is not None:
                raise ValueError('Invalid '+query+' answer: '+error)
        records.append({'phase':phase,'retained':len(retained),'attempts':len(attempts),'valid':True})
    return records


def run(base_path,storage,cache,folder,repetitions):
    if (folder/'status.json').exists():
        print('Run already recorded. Do not resubmit queries; use status or summarize.')
        print((folder/'status.json').read_text())
        return
    base=json.loads(base_path.read_text())
    attachment=json.loads(storage.read_text())['sql']['postgresql']
    if attachment.get('provider')!='aws' or attachment['region']!=base['deployment']['aws']['region']:
        raise ValueError('Benchmark config and existing SQL storage must use the same AWS region')
    manifests=[json.loads(p.read_text()) for p in (Path(attachment['state_dir'])/'datasets').glob('sebs_720_*.json')]
    recorded=[m for m in manifests if m.get('metadata')=={'scale_factor':1,'input_size':'small','workload':'TPC-H'}]
    if len(recorded)!=1:
        raise ValueError('Expected exactly one existing measured SF1 dataset report')
    configs={q:configuration(base,q,repetitions) for q in QUERIES}
    expected=official_answers(QUERIES)
    for q,config in configs.items():
        save(folder/'configs'/(q+'.json'),config)
    save(folder/'expected.json',expected)
    save(folder/'plan.json',{'repetitions':repetitions,'queries':list(QUERIES),'input_size':'small',
        'connection_policy':'fresh','memory_mb':512,'transport':'library',
        'timing_scope':'Native AWS and intra-function; local WSL client timings excluded',
        'database_state':'Existing persistent PostgreSQL; its cache is not reset',
        'namespace':recorded[0]['namespace'],'input_fingerprint':recorded[0]['fingerprint'],
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES.values()},
        'warmup_policy':'One initial sample per query/mode excluded from retained statistics but preserved for billing',
        'storage_configuration':str(storage.resolve()),'cache':str(cache.resolve())})
    state={'state':'running','completed_queries':0,'total_queries':len(QUERIES),'current':None}
    save(folder/'status.json',state)
    env=dict(os.environ)
    env.pop('SEBS_SQL_SCALE_FACTOR',None)
    try:
        for query in QUERIES:
            state['current']=query
            save(folder/'status.json',state)
            subprocess.run([sys.executable,'-B','-m','sebs.cli','experiment','invoke','perf-cost',
                '--config',str((folder/'configs'/(query+'.json')).resolve()),'--deployment','aws',
                '--storage-configuration',str(storage.resolve()),'--cache',str(cache.resolve()),
                '--output-dir',str((folder/query).resolve()),'--verbose'],cwd=ROOT,env=env,check=True)
            validate_query(folder,query,expected,repetitions)
            state['completed_queries']+=1
            save(folder/'status.json',state)
        state.update(state='complete',current=None)
    except BaseException as error:
        state.update(state='failed',error=f'{type(error).__name__}: {error}')
        raise
    finally:
        save(folder/'status.json',state)
    print('Cold/warm measurements complete; all recorded query answers validated.')


def summarize(folder):
    state=json.loads((folder/'status.json').read_text())
    if state['state']!='complete' or state['completed_queries']!=len(QUERIES):
        raise ValueError('Measurements are incomplete; inspect status before summarizing')
    report={'queries':{},'timing_scope':'Native AWS and intra-function, excludes WSL client latency',
            'cost_scope':'All saved invocation attempts, including initial samples; infrastructure costs separate'}
    total_usage=0.0
    plan=json.loads((folder/'plan.json').read_text())
    expected=json.loads((folder/'expected.json').read_text())
    for query in QUERIES:
        validate_query(folder,query,expected,plan['repetitions'])
        report['queries'][query]={}
        for phase in ('cold','warm'):
            data=json.loads((folder/query/'perf-cost'/(phase+'_results_512.json')).read_text())
            runs=[run for values in data['_invocations'].values() for run in values.values()]
            attempts=[v['result'] for v in data['statistics']['attempts'] if v.get('result')]
            measurement=lambda run:run['output']['result']['measurement'][query]
            native_complete=all(v['billing'].get('_billed_time',0)>0 for v in attempts)
            usage=sum(v['billing']['_gb_seconds'] for v in attempts) if native_complete else None
            if usage is None:
                raise ValueError('Missing native billing; preserve files and collect the missing request reports before a cost summary')
            total_usage+=usage
            item={'samples':len(runs),'attempts':len(attempts),'all_attempts_gb_seconds':usage,
                  'aws_execution_s':mean(v['provider_times']['execution']/1e6 for v in runs),
                  'aws_initialization_ms':mean(v['provider_times']['initialization']/1e3 for v in runs),
                  'query_fetch_s':mean(measurement(v)['query_time']/1e6 for v in runs),
                  'credentials_ms':mean(measurement(v)['credential_time']/1e3 for v in runs),
                  'connect_ms':mean(measurement(v)['connect_time']/1e3 for v in runs)}
            report['queries'][query][phase]=item
            print(f"{query:4} {phase:4} n={len(runs)} AWS={item['aws_execution_s']:.3f}s "
                  f"init={item['aws_initialization_ms']:.1f}ms SQL={item['query_fetch_s']:.3f}s "
                  f"credentials={item['credentials_ms']:.1f}ms connect={item['connect_ms']:.1f}ms")
    report['all_attempts_gb_seconds']=total_usage
    save(folder/'summary.json',report)
    print('All attempts, including initial samples:',round(total_usage,6),'billed GB-seconds')
    print('Saved:',folder/'summary.json')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['run','status','summarize'])
    parser.add_argument('--config',type=Path,default=Path('configs/sql-postgresql-aws.example.json'))
    parser.add_argument('--storage-configuration',type=Path,default=Path('results/postgresql-aws-storage.json'))
    parser.add_argument('--cache',type=Path,default=Path('cache-postgresql-aws'))
    parser.add_argument('--output-dir',type=Path,default=Path('results/postgresql-aws-sf1-cold-warm'))
    parser.add_argument('--repetitions',type=int,default=10)
    args=parser.parse_args()
    if args.action=='run':run(args.config,args.storage_configuration,args.cache,args.output_dir,args.repetitions)
    elif args.action=='status':print((args.output_dir/'status.json').read_text())
    else:summarize(args.output_dir)


if __name__=='__main__':
    main()
