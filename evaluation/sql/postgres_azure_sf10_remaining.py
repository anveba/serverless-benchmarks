"""Remaining SF10 checks through durable receipts, excluding the recorded Q6 job."""
import argparse
import json
from pathlib import Path

from evaluation.sql.postgres_azure_baseline import save
from evaluation.sql.postgres_azure_long_query import start as start_query

QUERIES=('q1','q12','q3','q21')


def summarize(folder):
    state=json.loads((folder/'status.json').read_text())
    report={'state':state['state'],'scale_factor':10,'statement_timeout_ms':120000,
            'queries':state['queries'],'scope':'One attempt per query; original Q6 and its extended run remain separate'}
    report['valid_queries']=sum(item.get('valid') is True for item in state['queries'].values())
    report['execution_failures']=sum(item['state']=='failed' for item in state['queries'].values())
    save(folder/'summary.json',report)
    for query,item in report['queries'].items():
        timing=f" SQL={item['sql_s']:.3f}s function={item['function_s']:.3f}s" if item.get('valid') else ''
        print(query,item['state']+timing)
    print(f"Validated: {report['valid_queries']}/{len(QUERIES)}; execution failures: {report['execution_failures']}")
    print('Saved:',folder/'summary.json')
    return report


def run(config,storage,cache,folder):
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('Recorded batch; inspect status/collect individual jobs instead of resubmitting')
    save(folder/'plan.json',{'queries':list(QUERIES),'scale_factor':10,'repetitions':1,
        'statement_timeout_ms':120000,'connection_policy':'fresh','memory_mb':512,
        'dataset':'Existing SF10 snapshot, no extra indexes/disk changes',
        'q6':'Excluded; preserve original 120s failure and separate successful 240s treatment'})
    state={'state':'running','current':None,'completed_queries':0,'total_queries':len(QUERIES),'queries':{}}
    save(folder/'status.json',state)
    try:
        for query in QUERIES:
            state['current']=query;save(folder/'status.json',state)
            start_query(config,storage,cache,folder/query,query=query,statement_timeout_ms=120000)
            job=json.loads((folder/query/'status.json').read_text())
            if job['state']=='complete' and job.get('valid') is True:
                item=json.loads((folder/query/'summary.json').read_text())
            elif job['state']=='failed':
                receipt=json.loads((folder/query/'receipt.json').read_text())
                item={'state':'failed','valid':False,'exception':receipt.get('exception'),
                      'invocation_id':receipt.get('invocation_id')}
            else:
                raise RuntimeError('Invalid/unknown '+query+' outcome; inspect preserved job, no query retried')
            state['queries'][query]=item
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
