"""Apply the recorded AWS Q21 index profile to existing Azure SF10 storage.

Only the report transport changes. Data values, query text and allocations stay
fixed. Index/VACUUM preparation time and index bytes are reported separately.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

from evaluation.sql.postgres_q21_indexes import INDEXES, REMOTE, remote_script
from evaluation.sql.postgres_azure_baseline import ROOT, save
from sebs.storage.azure_postgresql import AzurePostgreSQL
from sebs.storage.sql_config import sql_config


def script(payload):
    remote_script(payload)  # Reuse the fixed-profile and identifier guards.
    body=REMOTE.split('\nboto3.client(')[0].replace('import boto3\n','')
    # The ownership file must end with a newline, not a literal backslash+n.
    body=body.replace(r' + "\\n")', r' + "\n")')
    body=body.replace('__PAYLOAD__',repr(payload))
    body+="\nfrom azure.identity import ManagedIdentityCredential\nfrom azure.storage.blob import BlobServiceClient\nblob=BlobServiceClient(request['account_url'],credential=ManagedIdentityCredential())\nblob.get_blob_client('sql',request['report_key']).upload_blob(json.dumps(report,default=str).encode(),overwrite=True)\nprint('INDEX_MAINTENANCE_RECORDED; benchmark_queries_executed=0')\n"
    return "set -eu\n/opt/sebs-postgres/venv/bin/python - <<'SEBS_AZURE_Q21_INDEXES'\n"+body+"\nSEBS_AZURE_Q21_INDEXES\n"


def apply(action,storage,folder):
    if action not in ('prepare','remove'):raise ValueError('Use prepare or remove')
    if action=='prepare' and (folder/'remove-report.json').exists():
        raise ValueError('Treatment already removed; use a new state directory for a new preparation')
    if (folder/(action+'-report.json')).exists():
        print('Recorded report already exists; no maintenance resubmitted.')
        show(json.loads((folder/(action+'-report.json')).read_text()))
        return
    config=sql_config(json.loads(storage.read_text())['sql'])
    if config.provider!='azure':raise ValueError('An existing Azure PostgreSQL attachment is required')
    backend=AzurePostgreSQL(config)
    plan,_=backend.info()
    datasets=[json.loads(path.read_text()) for path in (Path(config.state_dir)/'datasets').glob('sebs_720_*.json')]
    matches=[item for item in datasets if item.get('metadata',{}).get('scale_factor')==10]
    if len(matches)!=1:raise ValueError('Expected one existing SF10 dataset; do not regenerate/reload')
    dataset=matches[0]
    spec=importlib.util.spec_from_file_location('azure_q21_sql',ROOT/'benchmarks/700.analytics/720.sql-postgresql/python/queries.py')
    queries=importlib.util.module_from_spec(spec);spec.loader.exec_module(queries)
    payload={'action':action,'namespace':dataset['namespace'],'fingerprint':dataset['fingerprint'],
        'indexes':INDEXES,'query':queries.QUERIES['q21'],
        'account_url':'https://'+plan['storage_account']+'.blob.core.windows.net'}
    digest=hashlib.sha256((REMOTE+json.dumps(payload,sort_keys=True)+str(folder.resolve())).encode()).hexdigest()[:20]
    operation='azure-q21-'+action+'-'+digest
    payload['report_key']='reports/'+operation+'.json'
    save(folder/(action+'-plan.json'),payload)
    print('Running recorded Q21 index maintenance; data values and SQL are unchanged. No benchmark query executes.',flush=True)
    with backend.control() as cli:
        backend.owned_group(cli,plan)
        backend.command(operation,script(payload),cli,timeout=3600)
        report=json.loads(backend.blob(cli,plan).get_blob_client('sql',payload['report_key']).download_blob().readall())
    if report['namespace']!=dataset['namespace'] or report['fingerprint']!=dataset['fingerprint'] or report['action']!=action:
        raise ValueError('Index report belongs to another dataset/treatment')
    save(folder/(action+'-report.json'),report)
    show(report)
    print('Saved:',folder/(action+'-report.json'))


def show(report):
    for key in ('action','steps','before_table_bytes','after_table_bytes','visibility','elapsed_seconds'):
        print(key+':',json.dumps(report[key]))
    def walk(node,depth=0):
        print(' '*depth+node['Node Type']+' '+node.get('Relation Name','')+' '+node.get('Index Name',''))
        for child in node.get('Plans',[]):walk(child,depth+1)
    walk(report['estimated_plan'][0]['Plan'])
    print('No benchmark query executed. Preparation time and index storage are recorded separately.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','remove'])
    parser.add_argument('--storage-configuration',type=Path,required=True)
    parser.add_argument('--state-dir',type=Path,required=True)
    args=parser.parse_args()
    apply(args.action,args.storage_configuration,args.state_dir)


if __name__=='__main__':main()
