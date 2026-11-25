"""Azure PostgreSQL/Flex integration checks with mocked control planes only."""
import ast
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sebs.azure.azure import Azure
from sebs.azure.control import AzureControl
from sebs.azure.function import AzureFunction
from sebs.azure.config import AzureCredentials, AzureResources
from sebs.faas.function import FunctionConfig, Runtime
from sebs.azure.sql_deployment import function_template, validate_package, update_envs
from sebs.storage.azure_postgresql import AzurePostgreSQL, loader_script, LOADER_RUNNER, can_assign_roles
from sebs.storage.azure_postgresql_bootstrap import bootstrap
from sebs.storage.azure_postgresql_template import template, SECRET_READER
from sebs.storage.sql_config import PostgreSQLConfig
from sebs.storage.postgresql import PostgreSQL, protected_json
from sebs.utils import LoggingBase
from sebs.sebs_types import Language
from sebs.utils import LoggingHandlers

SUBSCRIPTION='11111111-2222-4333-8444-555555555555'
ROOT=Path(__file__).resolve().parents[1]


def config(directory):
    return PostgreSQLConfig.deserialize({'provider':'azure','state_dir':str(directory),'storage_id':'a'*12,
        'azure_subscription_id':SUBSCRIPTION,'region':'swedencentral','memory_mb':6144})


class AzureStorageTests(unittest.TestCase):
    def backend(self, directory):
        backend=AzurePostgreSQL(config(directory))
        with patch('cryptography.hazmat.primitives.asymmetric.rsa.generate_private_key') as generate:
            generate.return_value.public_key.return_value.public_bytes.return_value=b'ssh-rsa fixture'
            plan=backend.plan(create=True)
        plan['outputs']={'VmId':'/subscriptions/'+SUBSCRIPTION+'/resourceGroups/group/providers/Microsoft.Compute/virtualMachines/postgresql',
            'SecretUri':'https://test-vault.vault.azure.net/secrets/sebs-reader','FunctionIdentityClientId':SUBSCRIPTION,
            'FunctionIdentityId':'/identity','FunctionSubnetId':'/subnet'}
        protected_json(backend.folder/'azure.json',plan)
        return backend,plan

    def test_azure_config_roundtrip_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=config(temp)
            self.assertEqual(PostgreSQLConfig.deserialize(cfg.serialize()),cfg)
            self.assertNotIn('password',json.dumps(cfg.serialize()))
            for extra in [{'region':'us-east-1'},{'azure_subscription_id':'bad'},
                {'azure_vnet_cidr':'127.0.0.0/16'},{'azure_vnet_cidr':'2001:db8::/16'},
                {'azure_vnet_cidr':'10.1.0.0/24'},{'azure_vm_size':'x; command'}]:
                with self.subTest(extra=extra),self.assertRaises(ValueError):
                    PostgreSQLConfig.deserialize({**cfg.serialize(),**extra})

    def test_provider_factory_never_opens_local_docker(self):
        with tempfile.TemporaryDirectory() as temp,patch('docker.from_env') as docker:
            value=PostgreSQL.deserialize(config(temp))
            self.assertIsInstance(value.backend,AzurePostgreSQL)
            docker.assert_not_called()

    def test_arm_template_private_sql_scoped_roles_and_no_function_compute(self):
        with tempfile.TemporaryDirectory() as temp:
            backend,plan=self.backend(temp)
            data=template(backend.config,plan)
            self.assertNotIn('adminPassword',json.dumps(data))
            kinds=[r['type'] for r in data['resources']]
            vault=next(r for r in data['resources'] if r['type']=='Microsoft.KeyVault/vaults')
            self.assertNotIn('enablePurgeProtection',vault['properties'])
            self.assertTrue(vault['properties']['enableSoftDelete'])
            self.assertNotIn('Microsoft.Web/sites',kinds)
            self.assertEqual(kinds.count('Microsoft.Compute/virtualMachines'),1)
            vnet=next(r for r in data['resources'] if r['type']=='Microsoft.Network/virtualNetworks')
            self.assertEqual(vnet['properties']['subnets'][1]['properties']['delegations'][0]['properties']['serviceName'],
                             'Microsoft.App/environments')
            nsg=next(r for r in data['resources'] if r['type']=='Microsoft.Network/networkSecurityGroups')
            for rule in nsg['properties']['securityRules']:
                props=rule['properties']
                if props['access']=='Allow':
                    self.assertEqual(props['destinationPortRange'],'5432')
                    self.assertNotIn(props['sourceAddressPrefix'],['*','Internet','0.0.0.0/0'])
            roles=[r for r in data['resources'] if r['type']=='Microsoft.Authorization/roleAssignments']
            self.assertEqual(len(roles),3)
            self.assertTrue(any(SECRET_READER in r['properties']['roleDefinitionId'] for r in roles))

    def test_bootstrap_and_loader_compile_without_embedded_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            backend,plan=self.backend(temp)
            script=bootstrap(backend.config,plan)
            program=script.split("<<'BOOTSTRAP'\n")[1].split('\nBOOTSTRAP')[0]
            tree=ast.parse(program)
            values=[n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)]
            self.assertIn('local all all trust\nhostssl all all 0.0.0.0/0 scram-sha-256\n',values)
            self.assertIn('POSTGRES_DB=sebs\nPOSTGRES_USER=postgres\nPOSTGRES_PASSWORD=',values)
            self.assertNotIn('AZURE_SECRET_PASSWORD',script)
            self.assertIn('ManagedIdentityCredential()',script)
            ast.parse(LOADER_RUNNER.replace('__REPLACE__','False'))
            generated=loader_script({'namespace':'sebs_720_fixture','account_url':'https://storage.blob.core.windows.net'},'a'*64,'loaders/x')
            ast.parse(generated.split("<<'LOAD'\n")[1].split('\nLOAD')[0])
            self.assertIn("m.name not in ['loader.py','dataset.json','runner.py']",generated)

    def test_command_checkpoints_unknown_submission_and_never_repeats_put(self):
        with tempfile.TemporaryDirectory() as temp:
            backend,plan=self.backend(temp)
            cli=Mock();backend.owned_group=Mock()
            cli.document.side_effect=RuntimeError('network timeout')
            with self.assertRaises(RuntimeError):backend.command('operation','echo ready',cli,timeout=1)
            cli.run.return_value=None
            with self.assertRaisesRegex(RuntimeError,'submission unknown'):
                backend.command('operation','echo ready',cli,timeout=1)
            self.assertEqual(cli.document.call_count,1)
            with self.assertRaisesRegex(ValueError,'differs'):
                backend.command('operation','different',cli,timeout=1)

    def test_command_records_terminal_failure_without_retrying(self):
        with tempfile.TemporaryDirectory() as temp:
            backend,_=self.backend(temp)
            cli=Mock();backend.owned_group=Mock()
            cli.document.return_value={'properties':{'instanceView':{'executionState':'Failed','exitCode':1}}}
            with self.assertRaisesRegex(RuntimeError,'operation failed'):
                backend.command('operation','exit 1',cli,timeout=1)
            self.assertEqual(cli.document.call_count,1)
            self.assertEqual(len(list((backend.folder/'commands').glob('*.result.json'))),1)

    def test_changed_owner_and_attached_function_block_group_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            backend,plan=self.backend(temp)
            cli=Mock();cli.run.return_value={'tags':{'sebs.sql.owner':'other','sebs.sql.storage':backend.config.storage_id}}
            with self.assertRaisesRegex(ValueError,'ownership differs'):backend.owned_group(cli,plan)
            backend.control=Mock();backend.control.return_value.__enter__=Mock(return_value=cli)
            backend.control.return_value.__exit__=Mock(return_value=False)
            backend.owned_group=Mock(return_value={})
            protected_json(backend.folder/'functions.json',{'app':{'name':'app','app_id':'/app'}})
            cli.run.return_value={'name':'app'}
            with self.assertRaisesRegex(RuntimeError,'Remove attached SQL Function'):
                backend.remove(delete_data=True)
            self.assertFalse((backend.folder/'delete-request.json').exists())
            self.assertFalse(any(c.args[0][:2]==['group','delete'] for c in cli.run.call_args_list))

    def test_saved_camel_case_outputs_are_normalized_on_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            backend,plan=self.backend(temp)
            original=plan['outputs']
            plan['outputs']={key[0].lower()+key[1:]:value for key,value in original.items()}
            protected_json(backend.folder/'azure.json',plan)
            self.assertEqual(backend.info()[1],original)
            self.assertEqual(json.loads(backend.envs()['SEBS_SQL_CONFIG'])['secret_uri'],original['SecretUri'])

    def test_runtime_env_contains_only_identity_references(self):
        with tempfile.TemporaryDirectory() as temp:
            backend,_=self.backend(temp)
            data=json.loads(backend.envs()['SEBS_SQL_CONFIG'])
            self.assertEqual(data['provider'],'azure')
            self.assertNotIn('password',data)
            self.assertNotIn('host',data)

    def test_start_freezes_image_and_resumes_existing_deployment(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            backend,plan=self.backend(temp)
            cli=Mock()
            response={'properties':{'provisioningState':'Succeeded',
                'outputs':{k[0].lower()+k[1:]:{'value':v} for k,v in plan['outputs'].items()}}}
            quota_usage={'name':{'value':'cores'},'currentValue':'3','limit':'4'}
            def run(args,**kwargs):
                if args[:2]==['group','show']:
                    return {'tags':{'sebs.sql.owner':plan['owner'],'sebs.sql.storage':backend.config.storage_id}}
                if args[:2]==['provider','show']:return {'registrationState':'Registered'}
                if args[:2]==['vm','list-skus']:
                    return [{'name':backend.config.azure_vm_size,'resourceType':'virtualMachines','restrictions':[],
                             'capabilities':[{'name':'vCPUs','value':'2'},{'name':'MemoryGB','value':'8'}]}]
                if args[:2]==['vm','list-usage']:
                    return [quota_usage]
                if args[:2]==['account','list-locations']:return [{'name':'swedencentral'}]
                if args[:3]==['rest','--method','get']:
                    if 'Microsoft.Authorization/permissions' in args[-1]:return {'value':[{'actions':['*'],'notActions':[]}]}
                    return {'value':[{'name':'Sweden Central'}]}
                if args[:3]==['vm','image','show']:return {'name':'24.04.test','hyperVGeneration':'V2'}
                if args[:2]==['vm','get-instance-view']:return {'instanceView':{'statuses':[{'code':'PowerState/running'}]}}
                if args[:3]==['deployment','group','show']:return response
                raise AssertionError(args)
            cli.run.side_effect=run;cli.deploy.return_value=response
            backend.control=Mock();backend.control.return_value.__enter__=Mock(return_value=cli)
            backend.control.return_value.__exit__=Mock(return_value=False)
            backend.command=Mock()
            with self.assertRaisesRegex(ValueError,'Insufficient Azure VM vCPU quota'):
                backend.start()
            cli.deploy.assert_not_called()
            quota_usage['currentValue']='0'
            backend.start();backend.start()
            self.assertEqual(cli.deploy.call_count,1)
            saved=json.loads((backend.folder/'azure.json').read_text())
            self.assertTrue(saved['resolved_image'].endswith(':24.04.test'))
            self.assertTrue((backend.folder/'deployment-request.json').exists())
            self.assertEqual(backend.command.call_count,4)
            previous=cli.run.side_effect
            def failed_deployment(args,**kwargs):
                if args[:3]==['deployment','group','show']:
                    return {'properties':{'provisioningState':'Failed'}}
                return previous(args,**kwargs)
            cli.run.side_effect=failed_deployment
            backend.start()
            self.assertEqual(cli.deploy.call_count,2)
            self.assertEqual(len(list((backend.folder/'deployment-retries').glob('*.json'))),1)
            def unknown_deployment(args,**kwargs):
                if args[:3]==['deployment','group','show']:return None
                return previous(args,**kwargs)
            cli.run.side_effect=unknown_deployment
            with self.assertRaisesRegex(RuntimeError,'submission outcome is unknown'):backend.start()
            self.assertEqual(cli.deploy.call_count,2)

    def test_blob_loader_bundle_is_deterministic_and_strips_host_paths(self):
        from sebs.storage.postgresql import build_dataset
        with tempfile.TemporaryDirectory() as temp:
            backend,plan=self.backend(temp)
            source=Path(temp)/'input.parquet';source.write_bytes(b'fixture')
            dataset=build_dataset(backend.config.storage_id,'720.sql-postgresql','test',
                {'lineitem':{'files':[source],'primary_key':['key']}},{'scale_factor':.01})
            uploaded={}
            report={'namespace':dataset['namespace'],'fingerprint':dataset['fingerprint']}
            def client(container,key):
                def upload(value,overwrite):uploaded[key]=value.read() if hasattr(value,'read') else value
                return SimpleNamespace(upload_blob=upload,
                    download_blob=lambda:SimpleNamespace(readall=lambda:json.dumps(report).encode()))
            blob=SimpleNamespace(get_blob_client=client)
            cli=Mock();backend.control=Mock()
            backend.control.return_value.__enter__=Mock(return_value=cli)
            backend.control.return_value.__exit__=Mock(return_value=False)
            backend.owned_group=Mock();backend.blob=Mock(return_value=blob);backend.command=Mock()
            backend.prepare_dataset(dataset);backend.prepare_dataset(dataset)
            loaders=[key for key in uploaded if key.startswith('loaders/')]
            self.assertEqual(len(loaders),1)
            import tarfile
            with tarfile.open(fileobj=io.BytesIO(uploaded[loaders[0]])) as archive:
                remote=json.loads(archive.extractfile('dataset.json').read())
                self.assertNotIn('path',remote['tables']['lineitem']['files'][0])
                self.assertEqual(remote['fingerprint'],dataset['fingerprint'])
                self.assertEqual(set(archive.getnames()),{'loader.py','runner.py','dataset.json'})
            self.assertEqual(backend.command.call_args_list[0].args[:2],backend.command.call_args_list[1].args[:2])
            source.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'changed before Azure upload'):backend.prepare_dataset(dataset)

    def test_effective_rbac_requires_assignment_permission_not_just_contributor(self):
        cli=Mock()
        contributor={'actions':['*'],'notActions':['Microsoft.Authorization/*/Write','Microsoft.Authorization/*/Delete']}
        cli.run.return_value={'value':[contributor]}
        self.assertFalse(can_assign_roles(cli,'/owned-group'))
        cli.run.return_value={'value':[contributor,{'actions':['Microsoft.Authorization/roleAssignments/write']}]}
        self.assertTrue(can_assign_roles(cli,'/owned-group'))


class FlexTests(unittest.TestCase):
    backend = AzureStorageTests.backend
    def package(self):
        return SimpleNamespace(language_name='python',language_version='3.11',uses_sql=True)

    def test_flex_plan_private_network_identity_memory_and_zero_always_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            backend,_=self.backend(temp)
            with patch('sebs.azure.sql_deployment.FunctionConfig.from_benchmark',return_value=SimpleNamespace(memory=512,timeout=300)):
                data=function_template(backend,'function','group',self.package(),'storage')
            app=next(r for r in data['resources'] if r['type']=='Microsoft.Web/sites')
            cfg=app['properties']['functionAppConfig']
            self.assertEqual(cfg['scaleAndConcurrency']['alwaysReady'],[])
            self.assertEqual(cfg['scaleAndConcurrency']['instanceMemoryMB'],512)
            self.assertEqual(cfg['scaleAndConcurrency']['triggers']['http']['perInstanceConcurrency'],1)
            self.assertEqual(app['properties']['virtualNetworkSubnetId'],'/subnet')
            self.assertEqual(app['identity']['type'],'UserAssigned')
            settings={v['name']:v['value'] for v in app['properties']['siteConfig']['appSettings']}
            self.assertNotIn('WEBSITE_RUN_FROM_PACKAGE',settings)
            self.assertEqual(settings['AzureFunctionsJobHost__logging__applicationInsights__samplingSettings__isEnabled'],'false')
            self.assertEqual(next(r['sku']['name'] for r in data['resources'] if r['type']=='Microsoft.Web/serverfarms'),'FC1')

    def test_memory_validation_does_not_silently_use_legacy_1536(self):
        with patch('sebs.azure.sql_deployment.FunctionConfig.from_benchmark',return_value=SimpleNamespace(memory=1536,timeout=300)):
            with self.assertRaisesRegex(ValueError,'512, 2048 or 4096'):validate_package(self.package())

    def test_native_adapter_routes_only_azure_sql_to_new_provider(self):
        deployment=Azure.__new__(Azure);LoggingBase.__init__(deployment)
        deployment._system_resources=Mock()
        backend=Mock()
        deployment._system_resources.get_sql_storage.return_value=SimpleNamespace(config=SimpleNamespace(provider='azure'),backend=backend)
        package=self.package();variant=SimpleNamespace(is_container=False)
        with patch('sebs.azure.sql_deployment.create',return_value='created') as create:
            self.assertEqual(deployment.create_function(package,'function',variant,None),'created')
            create.assert_called_once_with(deployment,package,'function')
        self.assertIsNone(deployment._sql_backend(SimpleNamespace(uses_sql=False)))
        deployment._system_resources.get_sql_storage.return_value.config.provider='docker'
        self.assertIsNone(deployment._sql_backend(package))

    def test_python_package_keeps_dependencies_at_root_and_does_not_move_handler_into_itself(self):
        deployment=Azure.__new__(Azure);LoggingBase.__init__(deployment)
        with tempfile.TemporaryDirectory() as temp,patch('sebs.azure.azure.execute'):
            folder=Path(temp)
            (folder/'function').mkdir();(folder/'function/sql.py').write_text('# wrapper')
            (folder/'handler.py').write_text('def main(request): pass')
            (folder/'.python_packages').mkdir();(folder/'requirements.txt').write_text('psycopg')
            deployment.package_code(temp,Language.PYTHON,'3.11','x64','720.sql-postgresql',False)
            self.assertTrue((folder/'handler/function/sql.py').exists())
            self.assertTrue((folder/'.python_packages').exists())
            self.assertTrue((folder/'handler/handler.py').exists())
            self.assertEqual(json.loads((folder/'host.json').read_text())['version'],'2.0')

    def test_onedeploy_uses_native_zip_and_actual_flex_hostname(self):
        from sebs.azure.sql_deployment import update
        with tempfile.TemporaryDirectory() as temp:
            backend,_=self.backend(temp)
            package=self.package();package.code_location=temp;package.benchmark='720.sql-postgresql'
            (Path(temp)/'720.sql-postgresql.zip').write_bytes(b'fixture')
            cli=Mock();cli.secrets=[]
            cli.run.side_effect=[{'value':[{'name':'function/handler'}]},
                {'properties':{'defaultHostName':'function-random.swedencentral.azurewebsites.net'}}]
            backend.control=Mock();backend.control.return_value.__enter__=Mock(return_value=cli)
            backend.control.return_value.__exit__=Mock(return_value=False)
            function=SimpleNamespace(name='function',sql_resources={'app_id':'/app'},
                function_storage=SimpleNamespace(account_name='storage',connection_string='AccountName=storage;AccountKey=private-key'),
                triggers_all=lambda:[],add_trigger=Mock())
            azure=SimpleNamespace(_sql_backend=lambda package:backend,cli_instance=object(),logging_handlers=LoggingHandlers(),
                config=SimpleNamespace(resources=SimpleNamespace(resource_group=lambda cli:'group')))
            with patch('sebs.azure.sql_deployment.FunctionConfig.from_benchmark',return_value=SimpleNamespace(memory=512,timeout=300)), \
                 patch('sebs.azure.sql_deployment.update_envs'),patch('sebs.azure.sql_deployment.update_configuration'), \
                 patch('azure.storage.blob.BlobServiceClient'),patch('azure.storage.blob.generate_blob_sas',return_value='private-sas'):
                update(azure,function,package)
            payload=cli.deploy.call_args.args[2]
            self.assertTrue(payload['resources'][0]['properties']['packageUri'].endswith('?private-sas'))
            self.assertFalse(payload['resources'][0]['properties']['remoteBuild'])
            self.assertIn('private-sas',cli.secrets)
            self.assertEqual(function.add_trigger.call_args.args[0].url,
                'https://function-random.swedencentral.azurewebsites.net/api/handler')

    def test_cache_roundtrip_preserves_sql_ownership_and_legacy_shape(self):
        cfg=FunctionConfig(timeout=300,memory=512,runtime=Runtime(Language.PYTHON,'3.11'))
        storage=AzureResources.Storage.from_cache('storage','connection-fixture')
        metadata={'owner':'owner','storage_id':'a'*12,'plan_id':'/plan'}
        function=AzureFunction('function','720.sql-postgresql','hash',storage,cfg,sql_resources=metadata)
        normalize=lambda value:json.loads(json.dumps(value,default=lambda obj:obj.serialize()))
        self.assertEqual(AzureFunction.deserialize(normalize(function.serialize())).sql_resources,metadata)
        legacy=AzureFunction('legacy','010.sleep','hash',storage,cfg)
        self.assertNotIn('sql_resources',legacy.serialize())
        self.assertIsNone(AzureFunction.deserialize(normalize(legacy.serialize())).sql_resources)

    def test_create_records_support_resources_before_allocating_and_caches_function(self):
        from sebs.azure.sql_deployment import create
        with tempfile.TemporaryDirectory() as temp:
            backend,plan=self.backend(temp)
            cli=Mock()
            registry=backend.folder/'functions.json'
            def run(args,**kwargs):
                if args[:3]==['rest','--method','get']:return None
                if args[:3]==['storage','account','show']:return None
                if args[:3]==['storage','account','create']:
                    self.assertTrue(registry.exists());return {}
                raise AssertionError(args)
            cli.run.side_effect=run;backend.owned_group=Mock()
            backend.control=Mock();backend.control.return_value.__enter__=Mock(return_value=cli)
            backend.control.return_value.__exit__=Mock(return_value=False)
            package=self.package();package.benchmark='720.sql-postgresql';package.hash='code-hash'
            resources=SimpleNamespace(resource_group=lambda cli:'group',storage_accounts=[],_storage_accounts=[],update_cache=Mock())
            azure=SimpleNamespace(_sql_backend=lambda package:backend,cli_instance=object(),cache_client=Mock(),
                config=SimpleNamespace(resources=resources),name=lambda:'azure')
            storage=SimpleNamespace(account_name='storage',connection_string='fixture')
            with patch('sebs.azure.sql_deployment.FunctionConfig.from_benchmark',return_value=SimpleNamespace(memory=512,timeout=300)), \
                 patch('sebs.azure.sql_deployment.AzureResources.Storage.from_allocation',return_value=storage), \
                 patch('azure.storage.blob.BlobServiceClient'),patch('sebs.azure.sql_deployment.update'):
                function=create(azure,package,'function')
            self.assertEqual(function.sql_resources['owner'],plan['owner'])
            self.assertEqual(len(json.loads(registry.read_text())['function']['resource_ids']),4)
            azure.cache_client.add_function.assert_called_once()


class IdentityRuntimeTests(unittest.TestCase):
    def wrapper(self):
        spec=importlib.util.spec_from_file_location('azure_pg_runtime',ROOT/'benchmarks/wrappers/common/python/sql.py')
        module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'psycopg':SimpleNamespace(connect=Mock())}):spec.loader.exec_module(module)
        return module

    def test_worker_identity_uses_key_vault_and_tls_credentials_are_cached(self):
        wrapper=self.wrapper()
        config={'type':'postgresql','provider':'azure','secret_uri':'https://vault-name.vault.azure.net/secrets/sebs-reader',
                'identity_client_id':SUBSCRIPTION}
        reader={'host':'10.73.1.4','port':5432,'dbname':'sebs','user':'sebs_reader','password':'private-password','ca_pem':'fixture-ca'}
        token=Mock();token.__enter__=Mock(return_value=token);token.__exit__=Mock(return_value=False)
        token.read.return_value=json.dumps({'access_token':'private-token'}).encode()
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'value':json.dumps(reader)}).encode()
        with patch.dict(os.environ,{'SEBS_SQL_CONFIG':json.dumps(config),'IDENTITY_ENDPOINT':'http://localhost:8081/msi/token','IDENTITY_HEADER':'private-header'},clear=True), \
             patch('urllib.request.urlopen',side_effect=[token,response]) as request,patch.object(wrapper,'_publish_ca') as publish:
            client=wrapper.sql();settings,hit=client.credentials()
            self.assertFalse(hit);self.assertEqual(settings['sslmode'],'verify-full')
            self.assertEqual(client.credentials()[1],True)
            self.assertEqual(request.call_count,2)
            self.assertIn('client_id='+SUBSCRIPTION,request.call_args_list[0].args[0].full_url)
            self.assertEqual(request.call_args_list[0].args[0].get_header('X-identity-header'),'private-header')
            self.assertEqual(request.call_args_list[1].args[0].get_header('Authorization'),'Bearer private-token')
            publish.assert_called_once()

    def test_untrusted_secret_endpoint_and_identity_endpoint_rejected(self):
        wrapper=self.wrapper()
        with patch('urllib.request.urlopen') as request:
            with self.assertRaises(ValueError):wrapper._azure_secret({'secret_uri':'https://evil.example/secrets/reader'})
            with patch.dict(os.environ,{'IDENTITY_ENDPOINT':'https://evil.example/token'},clear=True):
                with self.assertRaises(ValueError):wrapper._azure_secret({'secret_uri':'https://vault-name.vault.azure.net/secrets/sebs-reader'})
            request.assert_not_called()


class ControlTests(unittest.TestCase):
    def test_secret_bearing_command_failure_never_prints_argv_or_stdout(self):
        cli=Mock();cli.docker_instance.exec_run.return_value=(1,(b'private-stdout',b'failure private-password'))
        control=AzureControl(SUBSCRIPTION,cli);control.secrets.append('private-password')
        with self.assertRaises(RuntimeError) as raised:
            control.run(['login','--password=private-password'])
        self.assertNotIn('private-password',str(raised.exception))
        self.assertNotIn('private-stdout',str(raised.exception))

    def test_explicit_public_subscription_is_used_without_credentials_in_config(self):
        cache=Mock();cache.get_config.return_value={}
        env={'AZURE_SECRET_APPLICATION_ID':'app','AZURE_SECRET_TENANT':'tenant','AZURE_SECRET_PASSWORD':'private'}
        with patch.dict(os.environ,env,clear=True):
            credentials=AzureCredentials.deserialize({'credentials':{'subscription_id':SUBSCRIPTION}},cache,LoggingHandlers())
        self.assertEqual(credentials.subscription_id,SUBSCRIPTION)
        self.assertNotIn('private',json.dumps(credentials.serialize()))


if __name__=='__main__':unittest.main()
