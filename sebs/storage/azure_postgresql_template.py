"""Owned PostgreSQL VM, private network and identities; no function or query here."""
from sebs.storage.sql_config import PostgreSQLConfig

SECRET_READER = '4633458b-17de-408a-b874-0445c86b69e6'
SECRET_OFFICER = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
BLOB_CONTRIBUTOR = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'


def rid(kind, name):
    return "[resourceId('"+kind+"', '"+name+"')]"


def template(config: PostgreSQLConfig, plan):
    resources=[]
    tags={'sebs.sql.storage':config.storage_id,'sebs.sql.owner':plan['owner']}
    def add(resource_type,name,api,properties,depends=(),**extra):
        value={'type':resource_type,'apiVersion':api,'name':name,'location':config.region,
               'properties':properties,'tags':tags,**extra}
        if depends:value['dependsOn']=list(depends)
        resources.append(value)
        return rid(resource_type,name)
    identity=add('Microsoft.ManagedIdentity/userAssignedIdentities',plan['function_identity'],'2023-01-31',{})
    vault=add('Microsoft.KeyVault/vaults',plan['vault'],'2023-07-01',{
        'tenantId':'[subscription().tenantId]','sku':{'family':'A','name':'standard'},
        'enableRbacAuthorization':True,'enableSoftDelete':True,'softDeleteRetentionInDays':7,
        # Omit purge protection: Azure rejects explicit false and protection is irreversible.
        'publicNetworkAccess':'Enabled','accessPolicies':[]})
    storage=add('Microsoft.Storage/storageAccounts',plan['storage_account'],'2023-05-01',{
        'supportsHttpsTrafficOnly':True,'minimumTlsVersion':'TLS1_2','allowBlobPublicAccess':False},
        sku={'name':'Standard_LRS'},kind='StorageV2')
    # Container has no location/tags in its resource schema.
    resources.append({'type':'Microsoft.Storage/storageAccounts/blobServices/containers','apiVersion':'2023-05-01',
        'name':plan['storage_account']+'/default/sql','dependsOn':[storage],'properties':{'publicAccess':'None'}})
    nsg=add('Microsoft.Network/networkSecurityGroups','postgresql','2023-11-01',{'securityRules':[
        {'name':'FunctionsPostgreSQL','properties':{'priority':100,'protocol':'Tcp','access':'Allow','direction':'Inbound',
            'sourceAddressPrefix':plan['function_subnet_prefix'],'sourcePortRange':'*','destinationAddressPrefix':'*','destinationPortRange':'5432'}},
        {'name':'PrivateDriverPostgreSQL','properties':{'priority':110,'protocol':'Tcp','access':'Allow','direction':'Inbound',
            'sourceAddressPrefix':plan['server_subnet_prefix'],'sourcePortRange':'*','destinationAddressPrefix':'*','destinationPortRange':'5432'}},
        {'name':'DenyOtherInbound','properties':{'priority':1000,'protocol':'*','access':'Deny','direction':'Inbound',
            'sourceAddressPrefix':'*','sourcePortRange':'*','destinationAddressPrefix':'*','destinationPortRange':'*'}}]})
    vnet=add('Microsoft.Network/virtualNetworks','sql','2023-11-01',{
        'addressSpace':{'addressPrefixes':[config.azure_vnet_cidr]},'subnets':[
            {'name':'server','properties':{'addressPrefix':plan['server_subnet_prefix'],
                'serviceEndpoints':[{'service':'Microsoft.Storage'},{'service':'Microsoft.KeyVault'}]}},
            {'name':'functions','properties':{'addressPrefix':plan['function_subnet_prefix'],
                'delegations':[{'name':'flex','properties':{'serviceName':'Microsoft.App/environments'}}],
                'serviceEndpoints':[{'service':'Microsoft.Storage'},{'service':'Microsoft.KeyVault'}]}}]})
    public=add('Microsoft.Network/publicIPAddresses','postgresql-egress','2023-11-01',{
        'publicIPAllocationMethod':'Static'},sku={'name':'Standard'})
    nic=add('Microsoft.Network/networkInterfaces','postgresql','2023-11-01',{
        'networkSecurityGroup':{'id':nsg},'ipConfigurations':[{'name':'private','properties':{
            'privateIPAllocationMethod':'Static','privateIPAddress':plan['server_ip'],
            'subnet':{'id':"[resourceId('Microsoft.Network/virtualNetworks/subnets','sql','server')]"},
            'publicIPAddress':{'id':public,'deleteOption':'Delete'}}}]},[nsg,vnet,public])
    publisher,offer,sku,version=plan.get('resolved_image',config.azure_vm_image).split(':')
    vm=add('Microsoft.Compute/virtualMachines','postgresql','2024-03-01',{
        'hardwareProfile':{'vmSize':config.azure_vm_size},
        'storageProfile':{'imageReference':{'publisher':publisher,'offer':offer,'sku':sku,'version':version},
            'osDisk':{'createOption':'FromImage','diskSizeGB':64,'deleteOption':'Delete',
                      'managedDisk':{'storageAccountType':config.azure_disk_sku}}},
        'osProfile':{'computerName':'sebs-postgres','adminUsername':'azureuser',
            'linuxConfiguration':{'disablePasswordAuthentication':True,'provisionVMAgent':True,
                'ssh':{'publicKeys':[{'path':'/home/azureuser/.ssh/authorized_keys','keyData':plan['ssh_public_key']}]}}},
        'networkProfile':{'networkInterfaces':[{'id':nic,'properties':{'primary':True,'deleteOption':'Delete'}}]},
        'securityProfile':{'securityType':'TrustedLaunch','uefiSettings':{'secureBootEnabled':True,'vTpmEnabled':True}}},
        [nic],identity={'type':'SystemAssigned'})
    for suffix,scope,role,principal,depends in [
        ('vm-secret',vault,SECRET_OFFICER,"[reference(resourceId('Microsoft.Compute/virtualMachines','postgresql'),'2024-03-01','Full').identity.principalId]",[vm,vault]),
        ('vm-blob',storage,BLOB_CONTRIBUTOR,"[reference(resourceId('Microsoft.Compute/virtualMachines','postgresql'),'2024-03-01','Full').identity.principalId]",[vm,storage]),
        ('function-secret',vault,SECRET_READER,"[reference(resourceId('Microsoft.ManagedIdentity/userAssignedIdentities','"+plan['function_identity']+"'),'2023-01-31').principalId]",[identity,vault])]:
        resources.append({'type':'Microsoft.Authorization/roleAssignments','apiVersion':'2022-04-01',
            'name':"[guid(resourceGroup().id,'"+suffix+"')]",'scope':scope,'dependsOn':depends,
            'properties':{'principalId':principal,'principalType':'ServicePrincipal',
                'roleDefinitionId':"[subscriptionResourceId('Microsoft.Authorization/roleDefinitions','"+role+"')]"}})
    values={'VmId':vm,'FunctionIdentityId':identity,
        'FunctionIdentityClientId':"[reference(resourceId('Microsoft.ManagedIdentity/userAssignedIdentities','"+plan['function_identity']+"'),'2023-01-31').clientId]",
        'FunctionSubnetId':"[resourceId('Microsoft.Network/virtualNetworks/subnets','sql','functions')]",
        'SecretUri':'https://'+plan['vault']+'.vault.azure.net/secrets/sebs-reader',
        'StorageAccount':plan['storage_account'],'ServerPrivateIp':plan['server_ip']}
    return {'$schema':'https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#',
        'contentVersion':'1.0.0.0','resources':resources,
        'outputs':{k:{'type':'string','value':v} for k,v in values.items()}}
