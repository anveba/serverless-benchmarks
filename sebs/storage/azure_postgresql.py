"""Native Azure PostgreSQL lifecycle and Blob/Managed Run Command transport."""
import gzip
import fnmatch
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import tarfile
import time
import uuid

from sebs.azure.control import AzureControl
from sebs.storage.postgresql import protected_json
from sebs.storage.azure_postgresql_bootstrap import bootstrap
from sebs.storage.azure_postgresql_template import template

PROVIDERS=['Microsoft.Compute','Microsoft.Network','Microsoft.Storage','Microsoft.KeyVault',
           'Microsoft.ManagedIdentity','Microsoft.Web','Microsoft.App','Microsoft.Insights','Microsoft.OperationalInsights']
API='2024-03-01'


def can_assign_roles(cli, scope):
    uri=scope+'/providers/Microsoft.Authorization/permissions?api-version=2022-04-01'
    target='microsoft.authorization/roleassignments/write'
    while uri:
        page=cli.run(['rest','--method','get','--uri',uri])
        for permission in page['value']:
            granted=any(fnmatch.fnmatchcase(target,a.lower()) for a in permission.get('actions',[]))
            excluded=any(fnmatch.fnmatchcase(target,a.lower()) for a in permission.get('notActions',[]))
            if granted and not excluded:return True
        uri=page.get('nextLink')
    return False


def flex_regions(cli, subscription):
    # Same public control API used by Azure CLI's Flex location command; works
    # without requiring a newer CLI command in the existing management image.
    uri='/subscriptions/'+subscription+'/providers/Microsoft.Web/geoRegions?api-version=2024-04-01&sku=FlexConsumption'
    value=cli.run(['rest','--method','get','--uri',uri])
    regions={r.get('name','').lower().replace(' ','') for r in value['value']}
    enabled={r['name'].lower() for r in cli.run(['account','list-locations'])}
    return regions & enabled


def preflight(subscription, region, vm_size='Standard_D2s_v5'):
    with AzureControl(subscription) as cli:
        print('Subscription:',subscription,'Region:',region)
        for provider in PROVIDERS:
            item=cli.run(['provider','show','--namespace',provider])
            print(provider+':',item['registrationState'])
        skus=cli.run(['vm','list-skus','--location',region,'--size',vm_size,'--all'])
        families=set()
        for item in skus:
            if item.get('resourceType')=='virtualMachines' and item['name']==vm_size:
                families.add(item.get('family','').lower())
                print('VM candidate:',json.dumps({'name':item['name'],'restrictions':item.get('restrictions',[]),
                    'capabilities':{c['name']:c['value'] for c in item.get('capabilities',[]) if c['name'] in ['vCPUs','MemoryGB']}}))
        for quota in cli.run(['vm','list-usage','--location',region]):
            if quota['name']['value'].lower() in families|{'cores'}:
                print('VM quota:',quota['name']['value'],'used=',quota['currentValue'],'limit=',quota['limit'])
        print('Flex Consumption enabled in region:',region in flex_regions(cli,subscription))
        print('Deployment identity needs resource creation plus RBAC role-assignment permissions.')
        print('No resources created. Availability checks do not reserve VM or Functions capacity.')


LOADER_RUNNER=r'''
import json
from pathlib import Path
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
from loader import load_dataset
root=Path(__file__).resolve().parent
dataset=json.loads((root/'dataset.json').read_text())
blob=BlobServiceClient(dataset['account_url'],credential=ManagedIdentityCredential())
def fetch(item,target):
    with target.open('wb') as stream:
        blob.get_blob_client('sql',item['key']).download_blob().readinto(stream)
report=load_dataset(dataset,json.loads(Path('/opt/sebs-postgres/admin.json').read_text()),fetch,replace=__REPLACE__)
payload=json.dumps(report,indent=2)
(root/'report.json').write_text(payload)
blob.get_blob_client('sql',dataset['report_key']).upload_blob(payload,overwrite=True)
print('DATASET_READY',report['namespace'],'no benchmark query executed')
'''


def normalize_outputs(outputs):
    # Azure CLI camel-cases output names; ARM identifiers are case-insensitive.
    names={name.casefold():name for name in (
        'VmId','FunctionIdentityId','FunctionIdentityClientId','FunctionSubnetId',
        'SecretUri','StorageAccount','ServerPrivateIp')}
    return {names.get(key.casefold(),key):value for key,value in outputs.items()}


class AzurePostgreSQL:
    def __init__(self, config):
        self.config=config
        self.folder=Path(config.state_dir)

    def control(self, cli=None):
        return AzureControl(self.config.azure_subscription_id,cli)

    def plan(self, create=False):
        path=self.folder/'azure.json'
        if path.exists():
            value=json.loads(path.read_text())
            if "outputs" in value:
                value["outputs"]=normalize_outputs(value["outputs"])
            return value
        if not create:
            raise RuntimeError('Azure SQL storage has no recorded deployment; run storage start sql')
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        network=ipaddress.ip_network(self.config.azure_vnet_cidr)
        public=rsa.generate_private_key(public_exponent=65537,key_size=2048).public_key().public_bytes(
            serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
        suffix=self.config.storage_id
        value={'resource_group':'sebs-postgres-'+suffix,'owner':str(uuid.uuid4()),
            'deployment':'postgresql','region':self.config.region,'subscription':self.config.azure_subscription_id,
            'storage_account':'sebspg'+suffix,'vault':'sebs-pg-'+suffix,
            'function_identity':'sql-functions','ssh_public_key':public,
            'server_ip':str(network.network_address+260),
            'server_subnet_prefix':str(ipaddress.ip_network((int(network.network_address)+256,24))),
            'function_subnet_prefix':str(ipaddress.ip_network((int(network.network_address)+512,26)))}
        protected_json(path,value)
        return value

    def owned_group(self, cli, plan, missing_ok=False):
        group=cli.run(['group','show','--name',plan['resource_group']],missing_ok=True)
        if group is None:
            if missing_ok:return None
            raise RuntimeError('Recorded Azure SQL resource group is absent')
        tags=group.get('tags') or {}
        if tags.get('sebs.sql.owner')!=plan['owner'] or tags.get('sebs.sql.storage')!=self.config.storage_id:
            raise ValueError('Azure resource group ownership differs; no resource modified')
        return group

    def info(self):
        plan=self.plan()
        if not plan.get('outputs'):
            raise RuntimeError('Azure PostgreSQL deployment outputs are not ready; run start/status')
        return plan,plan['outputs']

    def require_assignment_permission(self, cli, plan):
        scope='/subscriptions/'+self.config.azure_subscription_id+'/resourceGroups/'+plan['resource_group']
        if not can_assign_roles(cli,scope):
            raise RuntimeError('SQL managed identities require role-assignment write permission on '+scope+
                '. Contributor alone is insufficient. Grant the deployment principal RBAC administration on this owned group, then rerun the same storage start; no VM requested.')

    def start(self):
        if (self.folder/'delete-request.json').exists():
            raise ValueError('This Azure SQL deployment was scheduled for deletion; use fresh state for a new deployment')
        plan=self.plan(create=True)
        with self.control() as cli:
            if self.owned_group(cli,plan,missing_ok=True) is None:
                if (self.folder/'deployment-request.json').exists():
                    raise RuntimeError('Recorded Azure SQL resource group disappeared; use fresh state for a new deployment')
                cli.run(['group','create','--name',plan['resource_group'],'--location',self.config.region,
                    '--tags','sebs.sql.owner='+plan['owner'],'sebs.sql.storage='+self.config.storage_id])
            for provider in PROVIDERS:
                state=cli.run(['provider','show','--namespace',provider])['registrationState']
                if state!='Registered':
                    cli.run(['provider','register','--namespace',provider,'--wait'],json_output=False)
            record=self.folder/'deployment-request.json'
            if record.exists():
                deployed=cli.run(['deployment','group','show','-g',plan['resource_group'],'-n',plan['deployment']],missing_ok=True)
                if deployed is None:
                    raise RuntimeError('Deployment submission outcome is unknown; inspect the recorded resource group before retrying')
                if deployed['properties']['provisioningState'] in ('Failed','Canceled'):
                    # A new, explicit start can retry a known-terminal setup
                    # failure on the same owned IDs. Never retry an unknown send.
                    self.require_assignment_permission(cli,plan)
                    document=template(self.config,plan)
                    protected_json(self.folder/'deployment-retries'/(uuid.uuid4().hex+'.json'),document)
                    print('Resuming known-failed Azure setup in the same owned resource group; no benchmark query retried.',flush=True)
                    deployed=cli.deploy(plan['resource_group'],plan['deployment'],document)
            else:
                self.require_assignment_permission(cli,plan)
                if self.config.region not in flex_regions(cli,self.config.azure_subscription_id):
                    raise ValueError('Flex Consumption is not enabled for the chosen subscription/region')
                skus=cli.run(['vm','list-skus','--location',self.config.region,'--size',self.config.azure_vm_size,'--all'])
                candidates=[s for s in skus if s.get('resourceType')=='virtualMachines' and s['name']==self.config.azure_vm_size]
                if len(candidates)!=1:raise ValueError('Azure VM SKU is not available in the selected region')
                sku=candidates[0]
                if any(r.get('type')=='Location' for r in sku.get('restrictions',[])):
                    raise ValueError('Azure VM SKU is restricted for this subscription/region')
                capabilities={v['name']:v['value'] for v in sku['capabilities']}
                for quota in cli.run(['vm','list-usage','--location',self.config.region]):
                    if quota['name']['value'].lower() in {'cores',sku.get('family','').lower()}:
                        if int(quota['currentValue'])+int(capabilities['vCPUs'])>int(quota['limit']):
                            raise ValueError('Insufficient Azure VM vCPU quota: '+quota['name']['value'])
                if int(capabilities['vCPUs'])<self.config.cpus or float(capabilities['MemoryGB'])*1024<self.config.memory_mb+1024:
                    raise ValueError('Azure VM is too small for the configured PostgreSQL container plus host memory')
                image=cli.run(['vm','image','show','--location',self.config.region,'--urn',self.config.azure_vm_image])
                if image.get('plan') or image.get('hyperVGeneration')!='V2':
                    raise ValueError('Use a Gen2 Ubuntu image without Marketplace purchase terms')
                plan['resolved_image']=self.config.azure_vm_image.rsplit(':',1)[0]+':'+image['name']
                plan['vm_capabilities']={key:capabilities[key] for key in ['vCPUs','MemoryGB']}
                protected_json(self.folder/'azure.json',plan)
                document=template(self.config,plan)
                protected_json(self.folder/'deployment-template.json',document)
                protected_json(record,{'deployment':plan['deployment'],'owner':plan['owner'],'recorded_before_submission':True})
                print('Creating Azure PostgreSQL VM, 64 GiB disk, VNet, Blob staging, Key Vault and identities; Azure charges apply.',flush=True)
                deployed=cli.deploy(plan['resource_group'],plan['deployment'],document)
            deadline=time.monotonic()+self.config.startup_timeout
            while deployed['properties']['provisioningState'] in ('Accepted','Running','Creating'):
                if time.monotonic()>deadline:raise TimeoutError('Azure deployment still running; keep the same state and use status/start')
                time.sleep(3)
                deployed=cli.run(['deployment','group','show','-g',plan['resource_group'],'-n',plan['deployment']])
            if deployed['properties']['provisioningState']!='Succeeded':
                raise RuntimeError('Azure PostgreSQL deployment did not succeed; inspect resource-group deployment operations')
            plan['outputs']=normalize_outputs({k:v['value'] for k,v in deployed['properties']['outputs'].items()})
            protected_json(self.folder/'azure.json',plan)
            self.config.instance_id=plan['outputs']['VmId'];self.config.host=plan['server_ip']
            instance=cli.run(['vm','get-instance-view','-g',plan['resource_group'],'-n','postgresql'])
            states=[v['code'] for v in instance.get('instanceView',{}).get('statuses',[])]
            if any(s in states for s in ['PowerState/deallocated','PowerState/stopped']):
                cli.run(['vm','start','-g',plan['resource_group'],'-n','postgresql'],json_output=False)
            operation=plan.get('bootstrap_operation','bootstrap')
            old=self.folder/'commands'/('sebs-'+hashlib.sha256(operation.encode()).hexdigest()[:24]+'.result.json')
            if old.exists() and (json.loads(old.read_text()).get('properties',{}).get('instanceView') or {}).get('executionState')!='Succeeded':
                # A new explicit start may retry known-failed, idempotent setup.
                # Unknown/in-progress outcomes keep their original operation.
                operation='bootstrap-'+uuid.uuid4().hex
                plan['bootstrap_operation']=operation
                protected_json(self.folder/'azure.json',plan)
            self.command(operation,bootstrap(self.config,plan),cli,timeout=self.config.startup_timeout)
            self.command('readiness-'+uuid.uuid4().hex,
                'set -eu\nfor i in $(seq 1 60); do\n'
                '  if docker exec sebs-postgresql pg_isready -U postgres -d sebs >/dev/null 2>&1; then echo POSTGRES_READY; exit 0; fi\n'
                '  sleep 2\ndone\nexit 1\n',cli,timeout=180)
            print('Azure PostgreSQL ready. No benchmark queries executed.',flush=True)

    def command(self, operation, script, cli, timeout=3600):
        plan,outputs=self.info();self.owned_group(cli,plan)
        name='sebs-'+hashlib.sha256(operation.encode()).hexdigest()[:24]
        path=self.folder/'commands'/(name+'.json')
        uri=outputs['VmId']+'/runCommands/'+name+'?api-version='+API
        digest=hashlib.sha256(script.encode()).hexdigest()
        if path.exists():
            saved=json.loads(path.read_text())
            if saved['script_sha256']!=digest:raise ValueError('Saved Azure operation differs; no command resubmitted')
            value=cli.run(['rest','--method','get','--uri',uri+'&$expand=instanceView'],missing_ok=True)
            if value is None:raise RuntimeError('Managed Run Command submission unknown; inspect the recorded operation before resubmitting')
        else:
            protected_json(path,{'operation':operation,'resource_uri':uri,'script_sha256':digest,'recorded_before_submission':True})
            value=cli.document(['rest','--method','put','--uri',uri],{'location':self.config.region,
                'properties':{'source':{'script':script},'timeoutInSeconds':timeout,'asyncExecution':True}})
        deadline=time.monotonic()+timeout+60
        while True:
            view=value.get('properties',{}).get('instanceView') or {}
            state=view.get('executionState')
            if not state and value.get('properties',{}).get('provisioningState')=='Failed':
                protected_json(path.with_suffix('.result.json'),value)
                raise RuntimeError('Azure SQL command provisioning failed; inspect '+str(path.with_suffix('.result.json')))
            if state in ('Succeeded','Failed','Canceled','TimedOut'):
                protected_json(path.with_suffix('.result.json'),value)
                if state!='Succeeded' or view.get('exitCode',0)!=0:
                    raise RuntimeError('Azure SQL operation failed; inspect '+str(path.with_suffix('.result.json')))
                return value
            if time.monotonic()>deadline:raise TimeoutError('Azure SQL operation still running; preserve its checkpoint and retry the same preparation')
            time.sleep(3)
            value=cli.run(['rest','--method','get','--uri',uri+'&$expand=instanceView'])

    def status(self):
        path=self.folder/'azure.json'
        if not path.exists():return {'provider':'azure','state':'not-created'}
        plan=self.plan()
        with self.control() as cli:
            group=self.owned_group(cli,plan,missing_ok=True)
            if group is None:
                return {'provider':'azure','state':'DELETE_COMPLETE' if (self.folder/'delete-request.json').exists() else 'absent'}
            deployment=cli.run(['deployment','group','show','-g',plan['resource_group'],'-n',plan['deployment']],missing_ok=True)
            operation=plan.get('bootstrap_operation','bootstrap')
            command='sebs-'+hashlib.sha256(operation.encode()).hexdigest()[:24]
            readiness=None
            if plan.get('outputs'):
                readiness=cli.run(['rest','--method','get','--uri',plan['outputs']['VmId']+'/runCommands/'+command+
                                   '?api-version='+API+'&$expand=instanceView'],missing_ok=True)
            return {'provider':'azure','resource_group':plan['resource_group'],'state':group['properties']['provisioningState'],
                'deployment_state':deployment['properties']['provisioningState'] if deployment else 'not-submitted',
                'bootstrap_state':((readiness or {}).get('properties',{}).get('instanceView') or {}).get('executionState','not-submitted'),
                'note':'Deployment success is not bootstrap readiness; start resumes the recorded readiness operation'}

    def envs(self, external=True):
        _,outputs=self.info()
        return {'SEBS_SQL_CONFIG':json.dumps({'type':'postgresql','provider':'azure',
            'secret_uri':outputs['SecretUri'],'identity_client_id':outputs['FunctionIdentityClientId']})}

    def function_options(self):
        return {'azure':{'hosting_plan':'flex-consumption','always_ready_instances':0}}

    def blob(self, cli, plan):
        from azure.storage.blob import BlobServiceClient
        keys=cli.run(['storage','account','keys','list','-g',plan['resource_group'],'-n',plan['storage_account']])
        key=keys[0]['value'];cli.secrets.append(key)
        return BlobServiceClient('https://'+plan['storage_account']+'.blob.core.windows.net',credential=key)

    def prepare_dataset(self, dataset, replace=False):
        from sebs.storage import postgresql_loader
        plan,_=self.info()
        with self.control() as cli:
            self.owned_group(cli,plan)
            blob=self.blob(cli,plan)
            remote=json.loads(json.dumps(dataset))
            for table,spec in remote['tables'].items():
                for item in spec['files']:
                    source=Path(item.pop('path'))
                    if postgresql_loader.file_sha256(source)!=item['sha256']:
                        raise ValueError('Input file changed before Azure upload')
                    item['key']='inputs/'+dataset['namespace']+'/'+table+'/'+item['sha256']+'.parquet'
                    with source.open('rb') as stream:blob.get_blob_client('sql',item['key']).upload_blob(stream,overwrite=True)
            remote['account_url']='https://'+plan['storage_account']+'.blob.core.windows.net'
            remote['report_key']='reports/'+dataset['namespace']+'/'+dataset['fingerprint']+'.json'
            raw=io.BytesIO()
            with gzip.GzipFile(fileobj=raw,mode='wb',mtime=0) as compressed,tarfile.open(fileobj=compressed,mode='w') as bundle:
                for name,value in [('loader.py',Path(postgresql_loader.__file__).read_bytes()),
                    ('dataset.json',json.dumps(remote,sort_keys=True).encode()),
                    ('runner.py',LOADER_RUNNER.replace('__REPLACE__',repr(replace)).encode())]:
                    member=tarfile.TarInfo(name);member.size=len(value);member.mode=0o600
                    bundle.addfile(member,io.BytesIO(value))
            payload=raw.getvalue();digest=hashlib.sha256(payload).hexdigest();key='loaders/'+digest+'.tar.gz'
            blob.get_blob_client('sql',key).upload_blob(payload,overwrite=True)
            script=loader_script(remote,digest,key)
            operation='load-'+dataset['namespace']+'-'+dataset['fingerprint']
            if replace:operation+='-'+uuid.uuid4().hex
            self.command(operation,script,cli)
            report=json.loads(blob.get_blob_client('sql',remote['report_key']).download_blob().readall())
            if report.get('fingerprint')!=dataset['fingerprint'] or report.get('namespace')!=dataset['namespace']:
                raise ValueError('Azure loader report identity differs')
            protected_json(self.folder/'datasets'/(dataset['namespace']+'.json'),report)
            return report

    def stop(self):
        plan,_=self.info()
        with self.control() as cli:
            self.owned_group(cli,plan)
            cli.run(['vm','deallocate','-g',plan['resource_group'],'-n','postgresql','--no-wait'],json_output=False)
        return {'state':'deallocating','note':'Database retained; disk, public IP, Key Vault and storage can remain billable'}

    def remove(self, delete_data=False, dry_run=False):
        plan=self.plan()
        if not delete_data and not dry_run:raise ValueError('Azure removal destroys the database; pass --delete-data')
        with self.control() as cli:
            if self.owned_group(cli,plan,missing_ok=True) is None:return {'state':'DELETE_COMPLETE'}
            registry=self.folder/'functions.json'
            entries=json.loads(registry.read_text()) if registry.exists() else {}
            if dry_run:return {'resource_group':plan['resource_group'],
                'action':'delete PostgreSQL VM, disks, network, staging storage, vault and identities',
                'retired_function_support_resources':[r for e in entries.values() for r in e.get('resource_ids',[])]}
            if registry.exists():
                for entry in entries.values():
                    app=cli.run(['rest','--method','get','--uri',entry['app_id']+'?api-version=2024-04-01'],missing_ok=True)
                    if app is not None:raise RuntimeError('Remove attached SQL Function Apps through SeBS first: '+entry['name'])
                # Recover partial creation that failed before native function
                # caching. Never delete a live app or an unowned support resource.
                for entry in entries.values():
                    for resource_id in entry.get('resource_ids',[]):
                        expected='/subscriptions/'+self.config.azure_subscription_id+'/resourceGroups/'+entry['resource_group']+'/providers/'
                        if not resource_id.startswith(expected):
                            raise ValueError('Function support resource is outside its recorded subscription/group')
                        version=next((v for kind,v in [('Microsoft.Insights/components','2020-02-02'),
                            ('Microsoft.OperationalInsights/workspaces','2023-09-01'),
                            ('Microsoft.Web/serverfarms','2024-04-01'),('Microsoft.Storage/storageAccounts','2023-05-01')]
                            if '/providers/'+kind+'/' in resource_id),None)
                        if version is None:raise ValueError('Unexpected function support resource type in registry')
                        uri=resource_id+'?api-version='+version
                        value=cli.run(['rest','--method','get','--uri',uri],missing_ok=True)
                        if value is not None:
                            if (value.get('tags') or {}).get('sebs.sql.owner')!=plan['owner']:
                                raise ValueError('Function support resource ownership changed; no deletion')
                            cli.run(['rest','--method','delete','--uri',uri],json_output=False)
            protected_json(self.folder/'delete-request.json',{'resource_group':plan['resource_group'],'owner':plan['owner']})
            cli.run(['group','delete','--name',plan['resource_group'],'--yes','--no-wait'],json_output=False)
        return {'state':'DELETE_IN_PROGRESS','note':'Poll storage status. Key Vault soft-deleted names may remain reserved; no purge is performed.'}


def loader_script(dataset, digest, key):
    return "set -eu\nexport HOME=/root\n/opt/sebs-postgres/venv/bin/python - <<'LOAD'\n"+f'''
import hashlib, subprocess, tarfile
from pathlib import Path
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
root=Path('/opt/sebs-postgres/datasets')/{dataset['namespace']!r}
root.mkdir(parents=True,exist_ok=True)
archive=root/'loader.tar.gz'
client=BlobServiceClient({dataset['account_url']!r},credential=ManagedIdentityCredential())
with archive.open('wb') as stream:client.get_blob_client('sql',{key!r}).download_blob().readinto(stream)
if hashlib.sha256(archive.read_bytes()).hexdigest()!={digest!r}:raise RuntimeError('Azure loader bundle hash differs')
with tarfile.open(archive) as bundle:
    if any(not m.isfile() or m.name not in ['loader.py','dataset.json','runner.py'] for m in bundle.getmembers()):
        raise RuntimeError('Unexpected Azure loader bundle member')
    bundle.extractall(root,filter='data')
subprocess.run(['/opt/sebs-postgres/venv/bin/python',str(root/'runner.py')],check=True)
LOAD
'''
