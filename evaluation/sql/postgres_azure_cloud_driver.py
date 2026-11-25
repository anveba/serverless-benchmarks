"""Own one separate Azure cloud driver for the native PostgreSQL comparison.

Native SeBS still owns the Function and SQL storage. This evaluation helper owns
only driver-named resources in that existing group. Launch/readiness do not run
TPC-H queries. Preserve the state directory until cleanup is complete.
"""
import argparse
import base64
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import tarfile
import uuid
from zipfile import ZipFile

from evaluation.sql.postgres_azure_baseline import ROOT, save
from evaluation.sql.postgres_azure_cloud_preflight import source_package
from sebs.storage.azure_postgresql import AzurePostgreSQL
from sebs.storage.azure_postgresql_template import BLOB_CONTRIBUTOR
from sebs.storage.sql_config import sql_config

BASE='/opt/sebs-pg-azure-driver'
DRIVER='postgresql-driver'


class DriverCommands(AzurePostgreSQL):
    def __init__(self,config,folder,plan,vm_id):
        super().__init__(config)
        self.folder=folder/'driver-commands'
        self.driver_plan=plan;self.vm_id=vm_id
    def info(self):
        return self.driver_plan,{'VmId':self.vm_id}


def bundle(destination,plan,root=ROOT):
    archive=Path(plan['native_package'])
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=plan['native_package_sha256']:
        raise ValueError('Verified native Function ZIP changed')
    files=set()
    for name in ('sebs','evaluation','tools','dockerfiles'):
        for path in (root/name).rglob('*'):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts:
                if name in ('evaluation','tools') and path.suffix not in ('.py','.sh'):
                    continue
                if path.suffix in ('.py','.json','.sh','.toml','.md','.txt') or path.name=='Dockerfile':files.add(path)
    for name in ('700.analytics/710.sql-query','700.analytics/720.sql-postgresql','wrappers'):
        for path in (root/'benchmarks'/name).rglob('*'):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts and path.suffix in ('.py','.json','.txt'):
                files.add(path)
    for name in ('pyproject.toml','README.md','LICENSE.md','configs/systems.json',
                 'configs/python.json','configs/nodejs.json','configs/java.json','configs/cpp.json'):
        files.add(root/name)
    files.update((root/'configs').glob('*.example.json'))
    for name in ('benchmarks/__init__.py','configs/__init__.py'):
        if (root/name).is_file():files.add(root/name)
    def add(archive,name,data):
        info=tarfile.TarInfo(name);info.mode=0o644;info.size=len(data)
        archive.addfile(info,io.BytesIO(data))
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(destination,'w:gz') as output,ZipFile(archive) as native:
        for path in sorted(files):add(output,path.relative_to(root).as_posix(),path.read_bytes())
        add(output,'comparison/plan.json',json.dumps(plan,indent=2).encode())
        add(output,'driver_function/__init__.py',b'')
        for member,digest in plan['source_sha256'].items():
            content=native.read(member)
            if hashlib.sha256(content).hexdigest()!=digest:raise ValueError('Native source member changed: '+member)
            add(output,'driver_function/'+Path(member).name,content)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def bootstrap(plan,artifact):
    payload={'account_url':'https://'+plan['staging_account']+'.blob.core.windows.net',
             'key':artifact['key'],'sha256':artifact['sha256']}
    return r'''#!/bin/bash
set -euo pipefail
export HOME=/root
export DEBIAN_FRONTEND=noninteractive
mkdir -p /opt/sebs-pg-azure-driver/artifacts
if test -f /opt/sebs-pg-azure-driver/driver-ready.json; then exit 0; fi
exec >>/opt/sebs-pg-azure-driver/bootstrap.log 2>&1
trap 'touch /opt/sebs-pg-azure-driver/bootstrap.failed' ERR
apt-get update
apt-get install -y docker.io python3-venv
systemctl enable --now docker
python3 -m venv /opt/sebs-pg-azure-driver/control-venv
/opt/sebs-pg-azure-driver/control-venv/bin/pip install azure-identity azure-storage-blob
/opt/sebs-pg-azure-driver/control-venv/bin/python - <<'DOWNLOAD_DRIVER'
import hashlib,json,tarfile,time
from pathlib import Path
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import HttpResponseError
request=__ARTIFACT__
base=Path('/opt/sebs-pg-azure-driver')
blob=BlobServiceClient(request['account_url'],credential=ManagedIdentityCredential())
for attempt in range(60):
    try:
        data=blob.get_blob_client('sql',request['key']).download_blob().readall()
        break
    except HttpResponseError as error:
        if error.status_code not in (401,403) or attempt==59:raise
        time.sleep(5)
if hashlib.sha256(data).hexdigest()!=request['sha256']:raise RuntimeError('Driver bundle checksum differs')
archive=base/'code.tar.gz';archive.write_bytes(data)
repo=base/'repo';repo.mkdir(exist_ok=True)
with tarfile.open(archive,'r:gz') as files:files.extractall(repo,filter='data')
plan=json.loads((repo/'comparison/plan.json').read_text())
config={'type':'postgresql','provider':'azure','secret_uri':plan['secret_uri'],
        'identity_client_id':plan['function_identity_client_id']}
(base/'runtime.env').write_text('SEBS_SQL_CONFIG='+json.dumps(config)+'\n')
(base/'runtime.env').chmod(0o600)
DOWNLOAD_DRIVER
docker pull python:3.11-slim-bookworm
/opt/sebs-pg-azure-driver/control-venv/bin/python - <<'DRIVER_IMAGE'
import json,subprocess
from pathlib import Path
base=Path('/opt/sebs-pg-azure-driver')
image=json.loads(subprocess.check_output(['docker','image','inspect','python:3.11-slim-bookworm']))[0]
reference=image['RepoDigests'][0]
dockerfile=('FROM '+reference+'\n'
 'RUN apt-get update && apt-get install -y --no-install-recommends build-essential libcurl4-openssl-dev libssl-dev pkg-config && rm -rf /var/lib/apt/lists/*\n'
 'COPY repo /opt/sebs-pg-azure-driver/repo\n'
 'WORKDIR /opt/sebs-pg-azure-driver/repo\n'
 'RUN pip install --no-cache-dir -e ".[sql]" azure-identity\n')
(base/'Dockerfile').write_text(dockerfile)
(base/'.dockerignore').write_text('/control-venv\n/code.tar.gz\n/bootstrap.log\n/runtime.env\n/artifacts\n/runtime.json\n/base-image.json\n/driver-ready.json\n')
(base/'base-image.json').write_text(json.dumps({'base_image':reference,'base_image_id':image['Id']}))
DRIVER_IMAGE
docker build -t sebs-pg-azure-driver:ready /opt/sebs-pg-azure-driver
/opt/sebs-pg-azure-driver/control-venv/bin/python - <<'RECORD_IMAGE'
import json,subprocess
from pathlib import Path
base=Path('/opt/sebs-pg-azure-driver')
image=json.loads(subprocess.check_output(['docker','image','inspect','sebs-pg-azure-driver:ready']))[0]
value=json.loads((base/'base-image.json').read_text());value['driver_image_id']=image['Id']
(base/'runtime.json').write_text(json.dumps(value,indent=2))
RECORD_IMAGE
docker run --rm --network host --env-file /opt/sebs-pg-azure-driver/runtime.env \
  sebs-pg-azure-driver:ready python -m evaluation.sql.postgres_azure_driver_ready \
  > /opt/sebs-pg-azure-driver/driver-ready.tmp
mv /opt/sebs-pg-azure-driver/driver-ready.tmp /opt/sebs-pg-azure-driver/driver-ready.json
docker run --rm sebs-pg-azure-driver:ready pip freeze \
  > /opt/sebs-pg-azure-driver/artifacts/pip-freeze.txt
rm -f /opt/sebs-pg-azure-driver/bootstrap.failed
'''.replace('__ARTIFACT__',repr(payload))


def template(config,sql_plan,plan,artifact):
    group=sql_plan['resource_group']
    prefix='/subscriptions/'+config.azure_subscription_id+'/resourceGroups/'+group+'/providers/'
    def rid(kind,name):return prefix+kind+'/'+name
    nic=rid('Microsoft.Network/networkInterfaces',DRIVER)
    public=rid('Microsoft.Network/publicIPAddresses',DRIVER+'-egress')
    nsg=rid('Microsoft.Network/networkSecurityGroups',DRIVER)
    vm=rid('Microsoft.Compute/virtualMachines',DRIVER)
    storage=rid('Microsoft.Storage/storageAccounts',sql_plan['storage_account'])
    role_name="[guid(resourceGroup().id,'postgresql-driver-blob')]"
    tags={'sebs.sql.owner':sql_plan['owner'],'sebs.sql.storage':config.storage_id,'sebs.sql.role':'comparison-driver'}
    common={'location':config.region,'tags':tags}
    publisher,offer,sku,version=plan['driver_candidate']['image'].split(':')
    resources=[{'type':'Microsoft.Network/networkSecurityGroups','apiVersion':'2023-11-01','name':DRIVER,**common,
        'properties':{'securityRules':[{'name':'DenyInbound','properties':{'priority':1000,'protocol':'*','access':'Deny',
            'direction':'Inbound','sourceAddressPrefix':'*','sourcePortRange':'*','destinationAddressPrefix':'*','destinationPortRange':'*'}}]}},
        {'type':'Microsoft.Network/publicIPAddresses','apiVersion':'2023-11-01','name':DRIVER+'-egress',**common,
         'sku':{'name':'Standard'},'properties':{'publicIPAllocationMethod':'Static'}},
        {'type':'Microsoft.Network/networkInterfaces','apiVersion':'2023-11-01','name':DRIVER,**common,
         'dependsOn':[nsg,public],'properties':{'networkSecurityGroup':{'id':nsg},'ipConfigurations':[{'name':'private','properties':{
             'privateIPAllocationMethod':'Static','privateIPAddress':str(ipaddress.ip_address(plan['server_private_ip'])+1),
             'subnet':{'id':rid('Microsoft.Network/virtualNetworks','sql')+'/subnets/server'},
             'publicIPAddress':{'id':public,'deleteOption':'Delete'}}}]}},
        {'type':'Microsoft.Compute/virtualMachines','apiVersion':'2024-03-01','name':DRIVER,**common,'dependsOn':[nic],
         'identity':{'type':'SystemAssigned, UserAssigned','userAssignedIdentities':{plan['function_identity_id']:{}}},
         'properties':{'hardwareProfile':{'vmSize':plan['driver_candidate']['vm_size']},
             'storageProfile':{'imageReference':{'publisher':publisher,'offer':offer,'sku':sku,'version':version},
                 'osDisk':{'createOption':'FromImage','diskSizeGB':32,'deleteOption':'Delete','managedDisk':{'storageAccountType':'StandardSSD_LRS'}}},
             'osProfile':{'computerName':'sebs-pg-driver','adminUsername':'azureuser',
                 'customData':base64.b64encode(bootstrap(plan,artifact).encode()).decode(),
                 'linuxConfiguration':{'disablePasswordAuthentication':True,'provisionVMAgent':True,
                     'ssh':{'publicKeys':[{'path':'/home/azureuser/.ssh/authorized_keys','keyData':sql_plan['ssh_public_key']}]}}},
             'networkProfile':{'networkInterfaces':[{'id':nic,'properties':{'primary':True,'deleteOption':'Delete'}}]},
             'securityProfile':{'securityType':'TrustedLaunch','uefiSettings':{'secureBootEnabled':True,'vTpmEnabled':True}}}},
        {'type':'Microsoft.Authorization/roleAssignments','apiVersion':'2022-04-01','name':role_name,
         'scope':storage,'dependsOn':[vm],
         'properties':{'principalId':"[reference('"+vm+"','2024-03-01','Full').identity.principalId]",
            'principalType':'ServicePrincipal','roleDefinitionId':"[subscriptionResourceId('Microsoft.Authorization/roleDefinitions','"+BLOB_CONTRIBUTOR+"')]"}}]
    return {'$schema':'https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#',
        'contentVersion':'1.0.0.0','resources':resources,'outputs':{'DriverVmId':{'type':'string','value':vm}}}


def context(storage,folder):
    config=sql_config(json.loads(storage.read_text())['sql'])
    if config.provider!='azure':raise ValueError('Existing Azure SQL storage attachment required')
    backend=AzurePostgreSQL(config);sql_plan,_=backend.info()
    plan=json.loads((folder/'driver-plan.json').read_text())
    if plan['owner']!=sql_plan['owner'] or plan['sql_storage_id']!=config.storage_id:
        raise ValueError('Driver plan belongs to another SQL deployment')
    return config,backend,sql_plan,plan


def retry_failed_deployment(cli,config,sql_plan,folder):
    # Reuse the frozen template/artifact, correcting only the malformed child ID.
    original=json.loads((folder/'driver-template.json').read_text())
    document=json.loads(json.dumps(original))
    prefix='/subscriptions/'+config.azure_subscription_id+'/resourceGroups/'+sql_plan['resource_group']+'/providers/'
    correct=prefix+'Microsoft.Network/virtualNetworks/sql/subnets/server'
    malformed=prefix+'Microsoft.Network/virtualNetworks/subnets/sql/server'
    nics=[item for item in document['resources'] if item['type']=='Microsoft.Network/networkInterfaces' and item['name']==DRIVER]
    if len(nics)!=1:raise ValueError('Recorded driver NIC template differs')
    subnet=nics[0]['properties']['ipConfigurations'][0]['properties']['subnet']
    if subnet['id'] not in (correct,malformed):raise ValueError('Recorded driver subnet differs; no deployment retried')
    subnet['id']=correct
    for resource in document['resources']:
        if resource['type']=='Microsoft.Authorization/roleAssignments':continue
        if resource['name'] not in (DRIVER,DRIVER+'-egress'):
            raise ValueError('Recorded template contains a non-driver resource')
        value=cli.run(['rest','--method','get','--uri',prefix+resource['type']+'/'+resource['name']+
                       '?api-version='+resource['apiVersion']],missing_ok=True)
        if value is not None:
            tags=value.get('tags') or {}
            if (tags.get('sebs.sql.owner')!=sql_plan['owner'] or tags.get('sebs.sql.storage')!=config.storage_id
                    or tags.get('sebs.sql.role')!='comparison-driver'):
                raise ValueError('Partially created driver resource ownership differs; no deployment retried')
    attempt=folder/'driver-retries'/uuid.uuid4().hex
    save(attempt.with_suffix('.before.json'),original)
    save(attempt.with_suffix('.template.json'),document)
    save(folder/'driver-template.json',document)
    print('Resuming the known-failed driver deployment with corrected subnet ID and the same owned resources/artifact.',flush=True)
    deployed=cli.deploy(sql_plan['resource_group'],DRIVER,document)
    print('Driver deployment:',deployed['properties']['provisioningState'])
    print('Use status for bootstrap readiness; no benchmark SQL submitted.')


def launch(storage,cache,folder):
    config=sql_config(json.loads(storage.read_text())['sql'])
    if config.provider!='azure':raise ValueError('Existing Azure storage required')
    backend=AzurePostgreSQL(config);sql_plan,outputs=backend.info()
    plan_path=folder/'driver-plan.json';request_path=folder/'driver-request.json'
    if plan_path.exists():
        plan=json.loads(plan_path.read_text())
    else:
        plan=json.loads((folder/'preflight.json').read_text())
        if not plan['ready_to_plan_driver']:raise ValueError('Preflight did not establish headroom/permissions')
        if plan['owner']!=sql_plan['owner'] or plan['sql_storage_id']!=config.storage_id:raise ValueError('Preflight storage differs')
        _,_,digest,_=source_package(cache)
        if digest!=plan['native_package_sha256']:raise ValueError('Verified Function source changed after preflight')
        plan.update(secret_uri=outputs['SecretUri'],staging_account=sql_plan['storage_account'])
        plan['driver_vm_id']='/subscriptions/'+config.azure_subscription_id+'/resourceGroups/'+sql_plan['resource_group']+'/providers/Microsoft.Compute/virtualMachines/'+DRIVER
        save(plan_path,plan)
    if plan['owner']!=sql_plan['owner'] or plan['sql_storage_id']!=config.storage_id or plan['subscription']!=config.azure_subscription_id or plan['region']!=config.region:
        raise ValueError('Recorded driver belongs to another SQL deployment')
    with backend.control() as cli:
        backend.owned_group(cli,sql_plan)
        if request_path.exists():
            deployment=cli.run(['deployment','group','show','-g',sql_plan['resource_group'],'-n',DRIVER],missing_ok=True)
            if deployment is None:raise RuntimeError('Driver submission outcome unknown; inspect the saved request, do not resubmit')
            state=deployment['properties']['provisioningState']
            print('Existing driver deployment:',state)
            if state in ('Failed','Canceled'):
                retry_failed_deployment(cli,config,sql_plan,folder)
            else:
                print('No new VM or deployment requested. Use status.')
            return
        existing=cli.run(['vm','show','--ids',plan['driver_vm_id']],missing_ok=True)
        if existing is not None:raise ValueError('Driver VM name already exists without this launch checkpoint')
        archive=folder/'driver-code.tar.gz'
        digest=bundle(archive,plan)
        artifact={'sha256':digest,'key':'evaluation/postgresql-azure/drivers/'+digest+'/code.tar.gz'}
        blob=backend.blob(cli,sql_plan)
        with archive.open('rb') as stream:blob.get_blob_client('sql',artifact['key']).upload_blob(stream,overwrite=True)
        document=template(config,sql_plan,plan,artifact)
        save(folder/'driver-template.json',document)
        save(request_path,{'deployment':DRIVER,'artifact':artifact,'recorded_before_submission':True})
        print('Launching one separate two-vCPU Azure driver VM with 32GiB disk and egress IP; Azure charges apply.',flush=True)
        deployment=cli.deploy(sql_plan['resource_group'],DRIVER,document)
        print('Driver deployment:',deployment['properties']['provisioningState'])
        print('Bootstrap may still be installing dependencies. Use status; no benchmark SQL submitted.')


def status(storage,folder):
    config,backend,sql_plan,plan=context(storage,folder)
    with backend.control() as cli:
        backend.owned_group(cli,sql_plan)
        deployed=cli.run(['deployment','group','show','-g',sql_plan['resource_group'],'-n',DRIVER],missing_ok=True)
        print('Driver deployment:',deployed['properties']['provisioningState'] if deployed else 'not found')
        vm=cli.run(['vm','show','--ids',plan['driver_vm_id']],missing_ok=True)
        if vm is None:return
        if (vm.get('tags') or {}).get('sebs.sql.role')!='comparison-driver' or vm['tags'].get('sebs.sql.owner')!=plan['owner']:
            raise ValueError('Live VM ownership differs')
        target=DriverCommands(config,folder,sql_plan,plan['driver_vm_id'])
        script='set -eu\nif test -f '+BASE+'/driver-ready.json; then cat '+BASE+'/driver-ready.json; '\
            'elif test -f '+BASE+'/bootstrap.failed; then echo BOOTSTRAP_FAILED; tail -n 25 '+BASE+'/bootstrap.log; '\
            'else echo BOOTSTRAP_NOT_READY; tail -n 15 '+BASE+'/bootstrap.log 2>/dev/null || true; fi\n'
        value=target.command('status-'+uuid.uuid4().hex,script,cli,timeout=120)
        view=value['properties']['instanceView']
        print(view.get('output',''))
        print(view.get('error',''))
        text=view.get('output','').strip()
        try:ready=json.loads(text)
        except ValueError:return
        if ready.get('status')=='DRIVER_READY':save(folder/'driver-ready.json',ready)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['launch','status'])
    parser.add_argument('--storage-configuration',type=Path,required=True)
    parser.add_argument('--cache',type=Path,default=Path('cache-postgresql-azure'))
    parser.add_argument('--state-dir',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='launch':launch(args.storage_configuration,args.cache,args.state_dir)
    else:status(args.storage_configuration,args.state_dir)


if __name__=='__main__':main()
