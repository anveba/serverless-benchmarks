"""One native Azure PostgreSQL SF10 attempt per query; no automatic query retries."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from evaluation.sql.postgres_azure_baseline import ROOT, QUERIES, configuration, runs, save


def outcome(output):
    path=output/'experiments.json'
    if not path.exists():
        return {'outcome':'unknown','note':'No saved invocation record; inspect errors/telemetry before continuing'}
    values=runs(path)
    if len(values)!=1:raise ValueError('Expected exactly one SF10 invocation')
    value=values[0]
    item={'request_id':value['request_id'], 'cold_start':value['stats']['cold_start']}
    if value['stats']['failure']:
        return {**item,'outcome':'execution_failure','note':'Do not infer exact SQL time or timeout cause without server evidence'}
    log=(output/'out.log').read_text()
    if log.count('output validation passed')!=1 or 'output validation failed' in log:
        return {**item,'outcome':'invalid','note':'Answer validation did not pass'}
    result=value['output']['result']
    query=next(iter(result['measurement']))
    return {**item,'outcome':'valid','function_s':value['times']['benchmark']/1e6,
            'sql_s':result['measurement'][query]['query_time']/1e6}


def summarize(folder):
    state=json.loads((folder/'status.json').read_text())
    report={'state':state['state'],'queries':state['queries'],
            'scale_factor':10,'statement_timeout_ms':120000,
            'scope':'Single observations, not replicated performance distributions; failures and unknowns preserved',
            'cost':'Native billing/infrastructure costs remain separate'}
    save(folder/'summary.json',report)
    for query,item in report['queries'].items():
        timing=f" function={item['function_s']:.3f}s SQL={item['sql_s']:.3f}s" if item['outcome']=='valid' else ''
        print(query,item['outcome']+timing)
    print('Saved:',folder/'summary.json')
    return report


def run(config_path,storage,cache,folder):
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('Recorded/nonempty run directory; use status, never resubmit an SF10 attempt')
    base=json.loads(config_path.read_text())
    attachment=json.loads(storage.read_text())['sql']['postgresql']
    azure=base['deployment']['azure']
    if attachment.get('provider')!='azure' or attachment['region']!=azure['region'] or attachment['azure_subscription_id'].lower()!=azure['credentials']['subscription_id'].lower():
        raise ValueError('Storage/deployment provider, region or subscription differ')
    save(folder/'plan.json',{'queries':list(QUERIES),'input_size':'large','scale_factor':10,
        'statement_timeout_ms':120000,'repetitions':1,'connection_policy':'fresh','memory_mb':512,
        'database_treatment':'Default schema; no extra indexes, disk change or database restart',
        'stop_policy':'Stop on unknown result or invalid answer; no SQL retries'})
    state={'state':'running','current':None,'completed_queries':0,'total_queries':5,'queries':{}}
    save(folder/'status.json',state)
    environment=dict(os.environ);environment.pop('SEBS_SQL_SCALE_FACTOR',None)
    try:
        for query in QUERIES:
            state['current']=query;save(folder/'status.json',state)
            path=folder/'configs'/(query+'.json');save(path,configuration(base,query,'fresh'))
            output=folder/query
            common=['--config',str(path.resolve()),'--deployment','azure','--cache',str(cache.resolve()),
                    '--output-dir',str(output.resolve())]
            print('BEGIN',query,'SF10, one attempt, 120-second SQL limit',flush=True)
            result=subprocess.run([sys.executable,'-B','-m','sebs.cli','benchmark','invoke',
                '720.sql-postgresql','large',*common,'--architecture','x64','--language','python',
                '--language-version','3.11','--trigger','http','--repetitions','1','--validate',
                '--storage-configuration',str(storage.resolve())],cwd=ROOT,env=environment,check=False)
            item=outcome(output);item['cli_exit_code']=result.returncode
            state['queries'][query]=item;save(folder/'status.json',state)
            if item['outcome'] in ('unknown','invalid') or (result.returncode and item['outcome']=='valid'):
                raise RuntimeError('SF10 '+query+' needs inspection; no query retried and no further queries submitted')
            subprocess.run([sys.executable,'-B','-m','sebs.cli','benchmark','process',*common],
                           cwd=ROOT,env=environment,check=True)
            state['completed_queries']+=1;save(folder/'status.json',state)
        state.update(state='complete',current=None)
    except BaseException as error:
        state.update(state='failed',error=type(error).__name__+': '+str(error))
        raise
    finally:
        save(folder/'status.json',state)
        summarize(folder)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['run','status','summarize'])
    parser.add_argument('--config',type=Path)
    parser.add_argument('--storage-configuration',type=Path)
    parser.add_argument('--cache',type=Path)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='run':
        if not all([args.config,args.storage_configuration,args.cache]):parser.error('run requires config, storage and cache')
        run(args.config,args.storage_configuration,args.cache,args.output_dir)
    elif args.command=='status':print((args.output_dir/'status.json').read_text())
    else:summarize(args.output_dir)


if __name__=='__main__':main()
