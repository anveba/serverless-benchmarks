"""Read-only preflight for a separate Azure PostgreSQL comparison driver.

Verifies native Function sources/hosting, existing SF1 data, VM capacity and owned
private-network/identity references. No VM, role assignment or SQL is submitted.
"""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile

from evaluation.sql.postgres_azure_baseline import ROOT, save
from sebs.benchmark import Benchmark
from sebs.sebs_types import Language
from sebs.storage.azure_postgresql import AzurePostgreSQL, can_assign_roles
from sebs.storage.sql_config import sql_config

SOURCES={
    'handler/function.py':'benchmarks/700.analytics/720.sql-postgresql/python/function.py',
    'handler/queries.py':'benchmarks/700.analytics/720.sql-postgresql/python/queries.py',
    'handler/sql.py':'benchmarks/wrappers/common/python/sql.py',
    'handler/handler.py':'benchmarks/wrappers/azure/python/handler.py',
}


def source_package(cache, root=ROOT):
    path=cache/'720.sql-postgresql/config.json'
    native=json.loads(path.read_text())['azure']['python']
    functions=[value for value in native['functions'].values() if value['benchmark']=='720.sql-postgresql']
    if len(functions)!=1:raise ValueError('Expected exactly one cached native PostgreSQL Azure Function')
    function=functions[0]
    cfg=function['config']
    if (cfg['architecture']!='x64' or cfg['runtime']['version']!='3.11' or cfg['memory']!=512):
        raise ValueError('Expected the tested 512MiB/Python3.11/x64 Function allocation')
    package=native['code_package']['default']['3.11']['x64']
    current=Benchmark.hash_directory(str(root/'benchmarks/700.analytics/720.sql-postgresql/python'),'azure',Language.PYTHON)
    if function['hash']!=package['hash'] or current!=package['hash']:
        raise ValueError('Current sources and cached deployed package differ; do not silently change comparison code')
    archive=cache/package['location']/'720.sql-postgresql.zip'
    with ZipFile(archive) as files:
        hashes={}
        for member,source in SOURCES.items():
            content=files.read(member)
            if content!=(root/source).read_bytes():raise ValueError('Cached runtime member differs: '+member)
            hashes[member]=hashlib.sha256(content).hexdigest()
        versions=[name for name in files.namelist() if name.endswith('psycopg-3.3.9.dist-info/METADATA')]
        if len(versions)!=1:raise ValueError('Expected pinned Psycopg 3.3.9 in the native package')
    return function,archive,hashlib.sha256(archive.read_bytes()).hexdigest(),hashes


def quota_report(sku, quotas):
    capabilities={item['name']:item['value'] for item in sku['capabilities']}
    if sku.get('resourceType')!='virtualMachines' or any(item.get('type')=='Location' for item in sku.get('restrictions',[])):
        raise ValueError('Driver VM SKU is unavailable/restricted in this subscription region')
    if int(capabilities['vCPUs'])!=2 or float(capabilities['MemoryGB'])<4 or capabilities.get('CpuArchitectureType','x64').lower()!='x64':
        raise ValueError('Select a regional two-vCPU x64 driver with at least 4GiB RAM')
    families={'cores',sku['family'].lower()}
    selected=[{'name':item['name']['value'],'used':int(item['currentValue']),
               'limit':int(item['limit']),'needed':2,
               'headroom':int(item['currentValue'])+2<=int(item['limit'])}
              for item in quotas if item['name']['value'].lower() in families]
    if len(selected)!=2:raise ValueError('Missing regional or VM-family quota evidence')
    return capabilities,selected


def verify_live_function(cli,config,plan,function,archive_hash):
    metadata=function['sql_resources']
    if metadata['storage_id']!=config.storage_id or metadata['owner']!=plan['owner']:
        raise ValueError('Cached Function belongs to another native SQL storage')
    app=cli.run(['rest','--method','get','--uri',metadata['app_id']+'?api-version=2024-04-01'])
    properties=app['properties']
    if app['location'].lower().replace(' ','')!=config.region or (app.get('tags') or {}).get('sebs.sql.owner')!=plan['owner']:
        raise ValueError('Live Function region/ownership differs')
    flex=properties['functionAppConfig']
    if (properties.get('state')!='Running' or flex['runtime']!={'name':'python','version':'3.11'}
            or flex['scaleAndConcurrency']['instanceMemoryMB']!=512
            or flex['scaleAndConcurrency'].get('alwaysReady')):
        raise ValueError('Live Function hosting/runtime/allocation differs from the tested profile')
    if properties['virtualNetworkSubnetId'].lower()!=plan['outputs']['FunctionSubnetId'].lower():
        raise ValueError('Function is attached to another SQL subnet')
    settings=cli.run(['rest','--method','post','--uri',metadata['app_id']+'/config/appsettings/list?api-version=2024-04-01'])['properties']
    cli.secrets.extend(value for value in settings.values() if isinstance(value,str) and value)
    runtime=json.loads(settings['SEBS_SQL_CONFIG'])
    if runtime!={'type':'postgresql','provider':'azure','secret_uri':plan['outputs']['SecretUri'],
                'identity_client_id':plan['outputs']['FunctionIdentityClientId']}:
        raise ValueError('Live SQL runtime identity/secret reference differs')
    group=metadata['app_id'].split('/')[4]
    publication='sebs-publish-'+archive_hash[:12]
    deployments=cli.run(['deployment','group','list','-g',group])
    publications=[]
    for deployed in sorted(deployments,key=lambda item:item['properties'].get('timestamp',''),reverse=True):
        if not deployed['name'].startswith('sebs-publish-'):
            continue
        template=cli.run(['deployment','group','export','-g',group,'-n',deployed['name']])
        selected=[item for item in template['resources'] if item['type'].lower()=='microsoft.web/sites/extensions'
                  and item['name'].lower()==(function['name']+'/onedeploy').lower()]
        if selected:
            if deployed['name']!=publication or deployed['properties']['provisioningState']!='Succeeded':
                raise ValueError('Latest native publication for this Function differs from the verified archive')
            publications=selected
            break
    if len(publications)!=1:raise ValueError('Expected native OneDeploy publication is missing')
    uri=urlsplit(publications[0]['properties']['packageUri'])
    if uri.scheme!='https' or uri.path!='/sebs-deploy/packages/'+archive_hash+'.zip':
        raise ValueError('Native publication points to another ZIP artifact')
    # Only return public artifact coordinates; never persist the package SAS.
    return {'endpoint':'https://'+properties['defaultHostName']+'/api/handler',
            'code_account':uri.hostname.split('.')[0],'code_group':group,
            'publication':publication,'runtime_config':runtime}


def preflight(config_path,storage,cache,folder,vm_size=None):
    base=json.loads(config_path.read_text())
    config=sql_config(json.loads(storage.read_text())['sql'])
    azure=base['deployment']['azure']
    if config.provider!='azure' or config.region!=azure['region'] or config.azure_subscription_id.lower()!=azure['credentials']['subscription_id'].lower():
        raise ValueError('SQL attachment and benchmark provider/region/subscription differ')
    function,archive,archive_hash,hashes=source_package(cache)
    backend=AzurePostgreSQL(config);plan,outputs=backend.info()
    datasets=[json.loads(path.read_text()) for path in (Path(config.state_dir)/'datasets').glob('sebs_720_*.json')]
    matches=[item for item in datasets if item.get('metadata')=={'scale_factor':1,'input_size':'small','workload':'TPC-H'}]
    if len(matches)!=1:raise ValueError('Expected one existing measured SF1 snapshot')
    dataset=matches[0]
    selected=vm_size or config.azure_vm_size
    with backend.control() as cli:
        backend.owned_group(cli,plan)
        live=verify_live_function(cli,config,plan,function,archive_hash)
        # Verify the content-addressed remote package, not only its filename.
        from azure.storage.blob import BlobServiceClient
        account=cli.run(['storage','account','show','-g',live['code_group'],'-n',live['code_account']])
        if (account.get('tags') or {}).get('sebs.sql.owner')!=plan['owner']:raise ValueError('Code storage ownership differs')
        keys=cli.run(['storage','account','keys','list','-g',live['code_group'],'-n',live['code_account']])
        cli.secrets.extend(item['value'] for item in keys)
        blob=BlobServiceClient('https://'+live['code_account']+'.blob.core.windows.net',credential=keys[0]['value'])
        digest=hashlib.sha256()
        for chunk in blob.get_blob_client('sebs-deploy','packages/'+archive_hash+'.zip').download_blob().chunks():digest.update(chunk)
        if digest.hexdigest()!=archive_hash:raise ValueError('Remote native package bytes differ from the local archive')
        instance=cli.run(['vm','get-instance-view','--ids',outputs['VmId']])
        statuses=[item['code'] for item in instance.get('instanceView',{}).get('statuses',[])]
        if 'PowerState/running' not in statuses:raise ValueError('Existing PostgreSQL VM is not running')
        skus=cli.run(['vm','list-skus','--location',config.region,'--size',selected,'--all'])
        exact=[item for item in skus if item.get('resourceType')=='virtualMachines' and item['name']==selected]
        if len(exact)!=1:raise ValueError('Driver VM candidate is not available')
        capabilities,quotas=quota_report(exact[0],cli.run(['vm','list-usage','--location',config.region]))
        image=cli.run(['vm','image','show','--location',config.region,'--urn',plan['resolved_image']])
        if image.get('hyperVGeneration')!='V2' or image.get('plan'):raise ValueError('Expected frozen Ubuntu Gen2 image without purchase terms')
        scope='/subscriptions/'+config.azure_subscription_id+'/resourceGroups/'+plan['resource_group']
        assignment=can_assign_roles(cli,scope)
    report={'provider':'azure','region':config.region,'subscription':config.azure_subscription_id,
        'sql_storage_configuration':str(storage.resolve()),'sql_storage_id':config.storage_id,
        'owner':plan['owner'],'sql_resource_group':plan['resource_group'],'server_vm_id':outputs['VmId'],
        'server_private_ip':plan['server_ip'],'server_subnet_prefix':plan['server_subnet_prefix'],
        'function_identity_id':outputs['FunctionIdentityId'],'function_identity_client_id':outputs['FunctionIdentityClientId'],
        'function_name':function['name'],'function_app_id':function['sql_resources']['app_id'],
        'function_endpoint':live['endpoint'],'function_memory_mb':512,'function_runtime':'python3.11',
        'driver_python_expected':'3.11','driver_psycopg_expected':'3.3.9',
        'native_package':str(archive.resolve()),'native_package_sha256':archive_hash,'source_sha256':hashes,
        'namespace':dataset['namespace'],'input_fingerprint':dataset['fingerprint'],
        'input':{'namespace':dataset['namespace'],'input_fingerprint':dataset['fingerprint'],
                 'queries':['q6'],'scale_factor':1,'connection_policy':'fresh','statement_timeout_ms':120000},
        'driver_candidate':{'vm_size':selected,'capabilities':capabilities,'image':plan['resolved_image'],'vcpus':2},
        'quotas':quotas,'role_assignment_permission':assignment,
        'capacity_headroom':all(item['headroom'] for item in quotas),
        'ready_to_plan_driver':all(item['headroom'] for item in quotas) and assignment,
        'limitations':['Capacity is not reserved','Private TCP/Key Vault driver access remains to be validated after launch',
            'No benchmark queries executed','No driver VM, role assignments or cloud resources created']}
    save(folder/'preflight.json',report)
    print('Existing PostgreSQL VM: running; existing SF1 fingerprint:',dataset['fingerprint'])
    print('Native Function source package, hosting, SQL identity, latest publication and remote ZIP bytes: verified')
    print('Driver candidate:',selected,config.region,'2 vCPUs; image:',plan['resolved_image'])
    for item in quotas:print('Quota:',item['name'],'used=',item['used'],'limit=',item['limit'],'needed=',item['needed'],'headroom=',item['headroom'])
    print('Role-assignment permission on owned SQL group:',assignment)
    print('Ready to plan driver:',report['ready_to_plan_driver'])
    print('Saved:',folder/'preflight.json')
    print('No SQL queries submitted; no driver or role assignments created.')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--storage-configuration',type=Path,required=True)
    parser.add_argument('--cache',type=Path,required=True)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--vm-size')
    args=parser.parse_args()
    preflight(args.config,args.storage_configuration,args.cache,args.state_dir,args.vm_size)


if __name__=='__main__':main()
