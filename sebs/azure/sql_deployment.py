"""Flex Consumption deployment for native Azure SQL storage attachments only."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time

from sebs.azure.config import AzureResources
from sebs.azure.function import AzureFunction
from sebs.azure.triggers import HTTPTrigger
from sebs.faas.function import FunctionConfig
from sebs.storage.postgresql import protected_json

API='2024-04-01'


def names(function, storage_id):
    digest=hashlib.sha256((storage_id+function).encode()).hexdigest()[:12]
    return {'plan':'sebs-sql-fc-'+digest,'workspace':'sebs-sql-logs-'+digest,
            'storage':'sqlfn'+digest+storage_id[:6]}


def validate_package(package):
    cfg=FunctionConfig.from_benchmark(package)
    if package.language_name!='python' or str(package.language_version) not in ('3.11','3.12'):
        raise ValueError('Azure SQL Flex deployment currently supports Python 3.11/3.12')
    if cfg.memory not in (512,2048,4096):
        raise ValueError('Azure SQL Flex memory must be 512, 2048 or 4096 MiB')
    if not 1<=cfg.timeout<=3600:
        raise ValueError('Azure SQL function timeout must be between 1 and 3600 seconds')
    return cfg


def function_template(backend, app_name, group, package, account):
    cfg=validate_package(package)
    plan,outputs=backend.info();selected=names(app_name,backend.config.storage_id)
    tags={'sebs.sql.storage':backend.config.storage_id,'sebs.sql.owner':plan['owner']}
    prefix='/subscriptions/'+backend.config.azure_subscription_id+'/resourceGroups/'+group+'/providers/'
    plan_id=prefix+'Microsoft.Web/serverfarms/'+selected['plan']
    workspace=prefix+'Microsoft.OperationalInsights/workspaces/'+selected['workspace']
    storage_id=prefix+'Microsoft.Storage/storageAccounts/'+account
    connection="[concat('DefaultEndpointsProtocol=https;AccountName="+account+";AccountKey=',listKeys('"+storage_id+"','2023-05-01').keys[0].value,';EndpointSuffix=core.windows.net')]"
    appsettings={'AzureWebJobsStorage':connection,'DEPLOYMENT_STORAGE_CONNECTION_STRING':connection,
        'SEBS_SQL_CONFIG':backend.envs()['SEBS_SQL_CONFIG'],
        'AzureFunctionsJobHost__logging__applicationInsights__samplingSettings__isEnabled':'false',
        'AzureFunctionsJobHost__functionTimeout':str(timedelta(seconds=cfg.timeout)),
        'APPLICATIONINSIGHTS_CONNECTION_STRING':"[reference(resourceId('Microsoft.Insights/components','"+app_name+"'),'2020-02-02').ConnectionString]"}
    common={'location':backend.config.region,'tags':tags}
    resources=[{'type':'Microsoft.Web/serverfarms','apiVersion':API,'name':selected['plan'],**common,
        'kind':'functionapp','sku':{'tier':'FlexConsumption','name':'FC1'},'properties':{'reserved':True}},
        {'type':'Microsoft.OperationalInsights/workspaces','apiVersion':'2023-09-01','name':selected['workspace'],**common,
         'properties':{'sku':{'name':'PerGB2018'},'retentionInDays':30}},
        {'type':'Microsoft.Insights/components','apiVersion':'2020-02-02','name':app_name,**common,'kind':'web',
         'dependsOn':[workspace],'properties':{'Application_Type':'web','WorkspaceResourceId':workspace}},
        {'type':'Microsoft.Web/sites','apiVersion':API,'name':app_name,**common,'kind':'functionapp,linux',
         'dependsOn':[plan_id,prefix+'Microsoft.Insights/components/'+app_name],
         'identity':{'type':'UserAssigned','userAssignedIdentities':{outputs['FunctionIdentityId']:{}}},
         'properties':{'serverFarmId':plan_id,'httpsOnly':True,'virtualNetworkSubnetId':outputs['FunctionSubnetId'],
            'functionAppConfig':{'runtime':{'name':'python','version':str(package.language_version)},
                'deployment':{'storage':{'type':'blobContainer','value':'https://'+account+'.blob.core.windows.net/sebs-deploy',
                    'authentication':{'type':'StorageAccountConnectionString',
                                      'storageAccountConnectionStringName':'DEPLOYMENT_STORAGE_CONNECTION_STRING'}}},
                'scaleAndConcurrency':{'maximumInstanceCount':100,'instanceMemoryMB':cfg.memory,'alwaysReady':[],
                                      'triggers':{'http':{'perInstanceConcurrency':1}}}},
            'siteConfig':{'appSettings':[{'name':k,'value':v} for k,v in appsettings.items()]}}}]
    return {'$schema':'https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#',
            'contentVersion':'1.0.0.0','resources':resources}


def create(azure, package, name):
    cfg=validate_package(package)
    backend=azure._sql_backend(package)
    plan,outputs=backend.info()
    group=azure.config.resources.resource_group(azure.cli_instance)
    resource_id='/subscriptions/'+backend.config.azure_subscription_id+'/resourceGroups/'+group+'/providers/Microsoft.Web/sites/'+name
    selected=names(name,backend.config.storage_id)
    with backend.control(azure.cli_instance) as cli:
        backend.owned_group(cli,plan)
        existing=cli.run(['rest','--method','get','--uri',resource_id+'?api-version='+API],missing_ok=True)
        if existing is not None and (existing.get('tags') or {}).get('sebs.sql.owner')!=plan['owner']:
            raise ValueError('Existing Function App is not owned by this SQL storage deployment')
        prefix='/subscriptions/'+backend.config.azure_subscription_id+'/resourceGroups/'+group+'/providers/'
        registry_path=backend.folder/'functions.json'
        registry=json.loads(registry_path.read_text()) if registry_path.exists() else {}
        registry[name]={'name':name,'app_id':resource_id,'resource_group':group,'owner':plan['owner'],
            'storage_account':selected['storage'],'plan':selected['plan'],
            'resource_ids':[
                prefix+'Microsoft.Insights/components/'+name,
                prefix+'Microsoft.OperationalInsights/workspaces/'+selected['workspace'],
                prefix+'Microsoft.Web/serverfarms/'+selected['plan'],
                prefix+'Microsoft.Storage/storageAccounts/'+selected['storage']]}
        # Persist deterministic allocation identities before any function resource
        # is created, including failures before a cached Function object exists.
        protected_json(registry_path,registry)
        account=cli.run(['storage','account','show','-g',group,'-n',selected['storage']],missing_ok=True)
        if account is not None:
            if (account.get('tags') or {}).get('sebs.sql.owner')!=plan['owner']:
                raise ValueError('Existing function storage account has another owner')
        else:
            cli.run(['storage','account','create','-g',group,'-n',selected['storage'],'--location',backend.config.region,
                '--sku','Standard_LRS','--min-tls-version','TLS1_2','--allow-blob-public-access','false',
                '--tags','sebs.sql.owner='+plan['owner'],'sebs.sql.storage='+backend.config.storage_id])
        function_storage=AzureResources.Storage.from_allocation(selected['storage'],azure.cli_instance)
        if not any(v.account_name==function_storage.account_name for v in azure.config.resources.storage_accounts):
            azure.config.resources._storage_accounts.append(function_storage)
            azure.config.resources.update_cache(azure.cache_client)
        from azure.storage.blob import BlobServiceClient
        from azure.core.exceptions import ResourceExistsError
        service=BlobServiceClient.from_connection_string(function_storage.connection_string)
        try:service.create_container('sebs-deploy')
        except ResourceExistsError:pass
        cli.deploy(group,'sebs-sql-'+hashlib.sha256(name.encode()).hexdigest()[:12],
                   function_template(backend,name,group,package,selected['storage']))
    prefix='/subscriptions/'+backend.config.azure_subscription_id+'/resourceGroups/'+group+'/providers/'
    metadata={'storage_id':backend.config.storage_id,'owner':plan['owner'],'app_id':resource_id,
              'plan_id':prefix+'Microsoft.Web/serverfarms/'+selected['plan'],
              'workspace_id':prefix+'Microsoft.OperationalInsights/workspaces/'+selected['workspace'],
              'insights_id':prefix+'Microsoft.Insights/components/'+name}
    function=AzureFunction(name,package.benchmark,package.hash,function_storage,cfg,sql_resources=metadata)
    update(azure,function,package)
    azure.cache_client.add_function(deployment_name=azure.name(),language_name=package.language_name,
                                    code_package=package,function=function)
    return function


def update_envs(azure, function, package, extra=None):
    backend=azure._sql_backend(package);plan,_=backend.info()
    metadata=function.sql_resources
    if not metadata or metadata['storage_id']!=backend.config.storage_id or metadata['owner']!=plan['owner']:
        raise ValueError('Cached Azure SQL function belongs to another storage deployment')
    with backend.control(azure.cli_instance) as cli:
        uri=metadata['app_id']+'/config/appsettings?api-version='+API
        values=cli.run(['rest','--method','post','--uri',metadata['app_id']+'/config/appsettings/list?api-version='+API])['properties']
        values.update(backend.envs());values.update(extra or {})
        values['AzureFunctionsJobHost__functionTimeout']=str(timedelta(seconds=validate_package(package).timeout))
        cli.secrets.extend(v for v in values.values() if isinstance(v,str) and v)
        cli.document(['rest','--method','put','--uri',uri],{'properties':values})


def update_configuration(azure, function, package):
    cfg=validate_package(package);backend=azure._sql_backend(package)
    with backend.control(azure.cli_instance) as cli:
        uri=function.sql_resources['app_id']+'?api-version='+API
        app=cli.run(['rest','--method','get','--uri',uri])
        if (app.get('tags') or {}).get('sebs.sql.owner')!=function.sql_resources['owner']:
            raise ValueError('Function ownership changed')
        configuration=app['properties']['functionAppConfig']
        if configuration['scaleAndConcurrency'].get('alwaysReady'):
            raise ValueError('SQL comparison requires zero always-ready instances')
        configuration['scaleAndConcurrency']['instanceMemoryMB']=cfg.memory
        cli.document(['rest','--method','patch','--uri',uri],{'properties':{'functionAppConfig':configuration}})


def update(azure, function, package):
    validate_package(package)
    update_envs(azure,function,package)
    update_configuration(azure,function,package)
    backend=azure._sql_backend(package)
    archive=Path(package.code_location)/(package.benchmark+'.zip')
    if not archive.is_file():raise RuntimeError('Native Azure code package ZIP is missing')
    from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
    parts=dict(item.split('=',1) for item in function.function_storage.connection_string.split(';') if '=' in item)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    key='packages/'+digest+'.zip';account=function.function_storage.account_name
    blob=BlobServiceClient.from_connection_string(function.function_storage.connection_string)
    with archive.open('rb') as stream:blob.get_blob_client('sebs-deploy',key).upload_blob(stream,overwrite=True)
    sas=generate_blob_sas(account_name=account,container_name='sebs-deploy',blob_name=key,
        account_key=parts['AccountKey'],permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc)+timedelta(hours=1),protocol='https')
    url='https://'+account+'.blob.core.windows.net/sebs-deploy/'+key+'?'+sas
    metadata=function.sql_resources
    group=azure.config.resources.resource_group(azure.cli_instance)
    deploy={'$schema':'https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#',
        'contentVersion':'1.0.0.0','resources':[{'type':'Microsoft.Web/sites/extensions','apiVersion':'2022-09-01',
            'name':function.name+'/onedeploy','properties':{'packageUri':url,'remoteBuild':False}}]}
    with backend.control(azure.cli_instance) as cli:
        cli.secrets.extend([sas,url,parts['AccountKey']])
        cli.deploy(group,'sebs-publish-'+digest[:12],deploy)
        deadline=time.monotonic()+180
        while True:
            indexed=cli.run(['rest','--method','get','--uri',metadata['app_id']+'/functions?api-version='+API])
            if any(f['name'].split('/')[-1]=='handler' for f in indexed.get('value',[])):break
            if time.monotonic()>deadline:raise TimeoutError('SQL Function App deployed but handler indexing is not ready')
            time.sleep(3)
        app=cli.run(['rest','--method','get','--uri',metadata['app_id']+'?api-version='+API])
    endpoint='https://'+app['properties']['defaultHostName']+'/api/handler'
    triggers=function.triggers_all()
    for trigger in triggers:
        if isinstance(trigger,HTTPTrigger):trigger.url=endpoint;break
    else:
        trigger=HTTPTrigger(endpoint);trigger.logging_handlers=azure.logging_handlers;function.add_trigger(trigger)


def delete_resources(azure, function):
    metadata=getattr(function,'sql_resources',None)
    if not metadata:return
    from sebs.azure.control import AzureControl
    with AzureControl(azure.config.credentials.subscription_id,azure.cli_instance) as cli:
        for key in ['insights_id','workspace_id','plan_id']:
            uri=metadata[key]+'?api-version='+{'insights_id':'2020-02-02','workspace_id':'2023-09-01','plan_id':API}[key]
            value=cli.run(['rest','--method','get','--uri',uri],missing_ok=True)
            if value is None:continue
            if (value.get('tags') or {}).get('sebs.sql.owner')!=metadata['owner']:
                raise ValueError('Refusing to delete Azure function resource with changed ownership')
            cli.run(['rest','--method','delete','--uri',uri],json_output=False)


def delete(azure, function):
    from sebs.azure.control import AzureControl
    metadata=function.sql_resources
    with AzureControl(azure.config.credentials.subscription_id,azure.cli_instance) as cli:
        uri=metadata['app_id']+'?api-version='+API
        app=cli.run(['rest','--method','get','--uri',uri],missing_ok=True)
        if app is not None:
            if (app.get('tags') or {}).get('sebs.sql.owner')!=metadata['owner']:
                raise ValueError('Function App ownership changed; no deletion')
            cli.run(['rest','--method','delete','--uri',uri],json_output=False)
            deadline=time.monotonic()+180
            while cli.run(['rest','--method','get','--uri',uri],missing_ok=True) is not None:
                if time.monotonic()>deadline:raise TimeoutError('Function App deletion pending; retry native cleanup later')
                time.sleep(3)
        account=function.function_storage.account_name
        group=azure.config.resources.resource_group(azure.cli_instance)
        storage=cli.run(['storage','account','show','-g',group,'-n',account],missing_ok=True)
        if storage is not None:
            if (storage.get('tags') or {}).get('sebs.sql.owner')!=metadata['owner']:
                raise ValueError('Function storage account ownership changed; no deletion')
            cli.run(['storage','account','delete','-g',group,'-n',account,'--yes'],json_output=False)
    delete_resources(azure,function)
    azure.config.resources._storage_accounts=[v for v in azure.config.resources.storage_accounts
                                             if v.account_name!=function.function_storage.account_name]
    azure.config.resources.update_cache(azure.cache_client)
