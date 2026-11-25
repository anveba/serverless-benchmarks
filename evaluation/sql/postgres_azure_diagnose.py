"""Azure PostgreSQL snapshot and EXPLAIN-only plans; no benchmark query replay."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re

from evaluation.sql.postgres_diagnose import REMOTE
from evaluation.sql.postgres_azure_baseline import ROOT, save
from sebs.storage.azure_postgresql import AzurePostgreSQL
from sebs.storage.sql_config import sql_config


def script(payload):
    if not re.fullmatch(r'sebs_[a-z0-9_]+',payload['namespace']) or not re.fullmatch(r'[a-f0-9]{64}',payload['fingerprint']):
        raise ValueError('Invalid dataset identifiers')
    # Reuse the existing AWS diagnostic body: metadata queries and EXPLAIN only.
    # Replace only its S3 transport with Azure's existing managed-identity Blob path.
    body=REMOTE.split('\nboto3.client(')[0].replace('import boto3\n','')
    body=body.replace('__PAYLOAD__',repr(payload))
    body+="\nfrom azure.identity import ManagedIdentityCredential\nfrom azure.storage.blob import BlobServiceClient\nblob=BlobServiceClient(request['account_url'],credential=ManagedIdentityCredential())\nblob.get_blob_client('sql',request['report_key']).upload_blob(json.dumps(report,default=str).encode(),overwrite=True)\nprint('DIAGNOSTICS_SAVED; benchmark_queries_executed=0')\n"
    return "set -eu\n/opt/sebs-postgres/venv/bin/python - <<'SEBS_AZURE_DIAGNOSTICS'\n"+body+"\nSEBS_AZURE_DIAGNOSTICS\n"


def diagnose(storage,folder,label):
    if not re.fullmatch(r'[a-z0-9-]{1,40}',label):raise ValueError('Use a short lowercase diagnostic label')
    config=sql_config(json.loads(storage.read_text())['sql'])
    if config.provider!='azure':raise ValueError('An existing Azure SQL storage attachment is required')
    backend=AzurePostgreSQL(config)
    plan,outputs=backend.info()
    datasets=[json.loads(path.read_text()) for path in (Path(config.state_dir)/'datasets').glob('sebs_720_*.json')]
    matches=[item for item in datasets if item.get('metadata',{}).get('scale_factor')==10]
    if len(matches)!=1:raise ValueError('Expected one existing SF10 dataset report; do not generate/reload it')
    dataset=matches[0]
    spec=importlib.util.spec_from_file_location('diagnostic_sql',ROOT/'benchmarks/700.analytics/720.sql-postgresql/python/queries.py')
    queries=importlib.util.module_from_spec(spec);spec.loader.exec_module(queries)
    payload={'namespace':dataset['namespace'],'fingerprint':dataset['fingerprint'],
             'queries':queries.QUERIES,'account_url':'https://'+plan['storage_account']+'.blob.core.windows.net',
             'report_key':'reports/'+label+'.json'}
    operation='diagnose-'+label+'-'+hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:12]
    print('Collecting disk/VM metadata, PostgreSQL counters and EXPLAIN-only plans. No benchmark SQL executes.',flush=True)
    with backend.control() as cli:
        backend.owned_group(cli,plan)
        vm=cli.run(['vm','show','--ids',outputs['VmId']])
        disk_id=vm['storageProfile']['osDisk']['managedDisk']['id']
        disk=cli.run(['disk','show','--ids',disk_id])
        cloud={'vm_size':vm['hardwareProfile']['vmSize'],'disk':{
            key:disk.get(key) for key in ('name','sku','diskSizeGb','diskIOPSReadWrite',
                'diskMBpsReadWrite','burstingEnabled','tier')},
            'os_disk_caching':vm['storageProfile']['osDisk'].get('caching'),
            'snapshot_note':'Current snapshot; counters cumulative, not isolated benchmark attribution'}
        backend.command(operation,script(payload),cli,timeout=180)
        report=json.loads(backend.blob(cli,plan).get_blob_client('sql',payload['report_key']).download_blob().readall())
    if report['namespace']!=dataset['namespace'] or report['fingerprint']!=dataset['fingerprint']:
        raise ValueError('Diagnostic snapshot belongs to another dataset')
    report['azure_resources']=cloud
    save(folder/(label+'.json'),report)
    print('AZURE:',json.dumps(cloud))
    for key in ('disk','memory','vmstat','container_stats','cgroups'):
        print(key.upper()+':\n'+report[key].get('stdout','Unavailable'))
    for key in ('container','settings','database_bytes','database_counters','tables'):
        print(key+':',json.dumps(report[key]))
    def walk(value,depth=0):
        if isinstance(value,list):
            for item in value:walk(item,depth)
        elif isinstance(value,dict):
            if 'Node Type' in value:
                print(' '*depth+value['Node Type']+' '+value.get('Relation Name','')+' '+value.get('Index Name',''))
            for key in ('Plan','Plans'):
                if key in value:walk(value[key],depth+1)
    for query,value in report['plans'].items():
        print('PLAN',query);walk(value)
    print('Saved:',folder/(label+'.json'))
    print('No benchmark query executed or database/disk configuration changed.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage-configuration',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--label',default='sf10-01')
    args=parser.parse_args()
    diagnose(args.storage_configuration,args.output_dir,args.label)


if __name__=='__main__':main()
