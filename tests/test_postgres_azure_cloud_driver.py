"""Offline Azure driver isolation, artifact, bootstrap and launch-checkpoint tests."""
import ast
import base64
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import ModuleType,SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from zipfile import ZipFile

from evaluation.sql import postgres_azure_cloud_driver as driver
from evaluation.sql import postgres_azure_driver_ready as readiness
from evaluation.sql.postgres_azure_baseline import ROOT,save
from sebs.storage.sql_config import sql_config


class AzurePostgresDriverTests(unittest.TestCase):
    def profile(self,folder):
        config=json.loads((ROOT/'configs/storage-postgresql-azure.example.json').read_text())
        config['sql']['postgresql'].update(azure_subscription_id='11111111-1111-1111-1111-111111111111',
            storage_id='a'*12,state_dir=str(folder/'sql'))
        cfg=sql_config(config['sql'])
        sql_plan={'owner':'owner','resource_group':'group','storage_account':'owned','ssh_public_key':'ssh-rsa public'}
        plan={'owner':'owner','sql_storage_id':cfg.storage_id,'subscription':cfg.azure_subscription_id,'region':cfg.region,
            'server_private_ip':'10.73.1.4','function_identity_id':'/reader-identity','function_identity_client_id':'client',
            'secret_uri':'https://vault/secrets/reader','driver_candidate':{'vm_size':'Standard_D2s_v5',
                'image':'Canonical:ubuntu-24_04-lts:server:frozen'},'staging_account':'owned'}
        return config,cfg,sql_plan,plan

    def test_template_owns_only_driver_and_keeps_database_private(self):
        with tempfile.TemporaryDirectory() as temp:
            _,cfg,sql_plan,plan=self.profile(Path(temp))
            template=driver.template(cfg,sql_plan,plan,{'key':'code','sha256':'f'*64})
            resources=template['resources']
            vms=[value for value in resources if value['type']=='Microsoft.Compute/virtualMachines']
            self.assertEqual(len(vms),1);self.assertEqual(vms[0]['name'],'postgresql-driver')
            self.assertEqual(vms[0]['properties']['storageProfile']['osDisk']['diskSizeGB'],32)
            self.assertIn('/reader-identity',vms[0]['identity']['userAssignedIdentities'])
            self.assertEqual(vms[0]['tags']['sebs.sql.role'],'comparison-driver')
            nsg=next(value for value in resources if value['type']=='Microsoft.Network/networkSecurityGroups')
            self.assertTrue(all(rule['properties']['access']=='Deny' for rule in nsg['properties']['securityRules']))
            self.assertEqual(len([value for value in resources if value['type']=='Microsoft.Authorization/roleAssignments']),1)
            self.assertNotIn('adminPassword',json.dumps(template))
            self.assertNotIn('Microsoft.KeyVault/vaults',json.dumps(template))
            bootstrap=base64.b64decode(vms[0]['properties']['osProfile']['customData']).decode()
            self.assertIn('driver-ready.json',bootstrap)
            nic=next(value for value in resources if value['type']=='Microsoft.Network/networkInterfaces')
            self.assertEqual(nic['properties']['ipConfigurations'][0]['properties']['subnet']['id'],
                '/subscriptions/'+cfg.azure_subscription_id+'/resourceGroups/group/providers/Microsoft.Network/virtualNetworks/sql/subnets/server')

    def test_bootstrap_programs_compile_and_use_real_newlines(self):
        shell=driver.bootstrap({'staging_account':'owned'},{'key':'code','sha256':'f'*64})
        for marker in ('DOWNLOAD_DRIVER','DRIVER_IMAGE','RECORD_IMAGE'):
            program=shell.split("<<'"+marker+"'\n",1)[1].split('\n'+marker,1)[0]
            tree=ast.parse(program)
            if marker=='DOWNLOAD_DRIVER':
                self.assertIn("extractall(repo,filter='data')",program)
                line=next(value for value in ast.walk(tree) if isinstance(value,ast.Call)
                    and isinstance(value.func,ast.Attribute) and value.func.attr=='write_text')
                self.assertEqual(line.args[0].right.value,'\n')
            if marker=='DRIVER_IMAGE':
                self.assertIn("image['RepoDigests'][0]",program)
        self.assertNotIn('AZURE_SECRET_PASSWORD',shell)
        self.assertNotIn('function.handler(',shell)
        self.assertTrue(all(not line.endswith('\\\\') for line in shell.splitlines()))

    def test_source_bundle_excludes_private_files_and_contains_verbatim_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'repo';root.mkdir()
            for name in ('pyproject.toml','README.md','LICENSE.md','configs/systems.json','configs/python.json',
                'configs/nodejs.json','configs/java.json','configs/cpp.json','sebs/public.py','evaluation/sql/public.py'):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('public')
            for name in ('results/secret.json','cache/secret.json','configs/sql-private.json','.aws/credentials','evaluation/private.json'):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('DO_NOT_BUNDLE')
            archive=Path(temp)/'native.zip'
            sources={}
            with ZipFile(archive,'w') as files:
                for member in ('handler/sql.py','handler/function.py','handler/queries.py','handler/handler.py'):
                    value=('# '+member).encode();files.writestr(member,value);sources[member]=hashlib.sha256(value).hexdigest()
            plan={'native_package':str(archive),'native_package_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
                  'source_sha256':sources}
            output=Path(temp)/'bundle.tar.gz';driver.bundle(output,plan,root)
            with tarfile.open(output,'r:gz') as files:
                names=files.getnames()
                self.assertNotIn('configs/sql-private.json',names)
                self.assertNotIn('evaluation/private.json',names)
                self.assertFalse(any(name.startswith(('results/','cache/','.aws/')) for name in names))
                self.assertEqual(files.extractfile('driver_function/sql.py').read(),b'# handler/sql.py')
                self.assertIn('comparison/plan.json',names)

    def test_driver_commands_target_driver_not_database_vm(self):
        with tempfile.TemporaryDirectory() as temp:
            _,cfg,sql_plan,_=self.profile(Path(temp))
            target=driver.DriverCommands(cfg,Path(temp)/'driver',sql_plan,'/driver-vm')
            self.assertEqual(target.info()[1]['VmId'],'/driver-vm')
            self.assertEqual(target.folder,Path(temp)/'driver/driver-commands')
            self.assertEqual(cfg.state_dir,str(Path(temp)/'sql'))

    def test_launch_checkpoints_before_arm_and_never_retries_unknown_submission(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            root=Path(temp);config,cfg,sql_plan,plan=self.profile(root)
            save(root/'storage.json',config)
            folder=root/'driver';plan.update(ready_to_plan_driver=True,native_package_sha256='digest')
            save(folder/'preflight.json',plan)
            backend=Mock();backend.info.return_value=(sql_plan,{'SecretUri':'https://vault/secrets/reader'})
            cli=Mock();cli.run.return_value=None
            backend.control.return_value.__enter__=Mock(return_value=cli)
            backend.control.return_value.__exit__=Mock(return_value=False)
            def submit(*args):
                self.assertTrue((folder/'driver-request.json').exists())
                raise TimeoutError('Unknown HTTP submission outcome')
            cli.deploy.side_effect=submit
            def bundle(path,plan):path.write_bytes(b'code');return 'digest'
            with patch.object(driver,'AzurePostgreSQL',return_value=backend), \
                 patch.object(driver,'source_package',return_value=(None,None,'digest',{})), \
                 patch.object(driver,'bundle',side_effect=bundle):
                with self.assertRaises(TimeoutError):driver.launch(root/'storage.json',root/'cache',folder)
                with self.assertRaisesRegex(RuntimeError,'submission outcome unknown'):
                    driver.launch(root/'storage.json',root/'cache',folder)
                cli.deploy.assert_called_once()
                backend.blob.return_value.get_blob_client.return_value.upload_blob.assert_called_once()

    def test_readiness_verifies_sources_reader_tls_and_metadata_only(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{},clear=False):
            root=Path(temp);sources={}
            for name in ('function.py','queries.py','sql.py','handler.py'):
                content=('# '+name).encode();path=root/'driver_function'/name
                path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
                sources['handler/'+name]=hashlib.sha256(content).hexdigest()
            plan={'source_sha256':sources,'secret_uri':'https://vault/secrets/reader',
                'function_identity_client_id':'reader-id','server_private_ip':'10.73.1.4',
                'namespace':'sebs_720_fixture','input_fingerprint':'f'*64}
            save(root/'comparison/plan.json',plan)
            sql_module=ModuleType('driver_function.sql');client=Mock()
            client.credentials.return_value=({'sslmode':'verify-full','host':'10.73.1.4','user':'sebs_reader',
                'port':5432,'password':'DO_NOT_PRINT'},False)
            sql_module.sql=Mock(return_value=client)
            package=ModuleType('driver_function');package.__path__=[]
            con=Mock();con.__enter__=Mock(return_value=con);con.__exit__=Mock(return_value=False)
            con.execute.side_effect=[SimpleNamespace(fetchone=lambda:('on',)),SimpleNamespace(fetchone=lambda:('f'*64,))]
            connection=Mock();connection.__enter__=Mock();connection.__exit__=Mock(return_value=False)
            psycopg=ModuleType('psycopg');psycopg.__version__='3.3.9';psycopg.connect=Mock(return_value=con)
            psycopg.sql=SimpleNamespace(SQL=lambda value:value,Identifier=lambda value:value)
            with patch.dict(sys.modules,{'driver_function':package,'driver_function.sql':sql_module,'psycopg':psycopg}), \
                 patch.object(readiness.sys,'version_info',(3,11,18)), \
                 patch('socket.create_connection',return_value=connection):
                report=readiness.check(root)
            self.assertEqual(report['benchmark_queries_submitted'],0)
            self.assertTrue(report['tls_verified']);self.assertTrue(report['sf1_fingerprint_matches'])
            self.assertEqual(con.execute.call_count,2)
            self.assertIn('default_transaction_read_only=on',psycopg.connect.call_args.kwargs['options'])
            self.assertNotIn('DO_NOT_PRINT',json.dumps(report))

    def test_failed_retry_corrects_only_subnet_and_reuses_frozen_artifact(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            root=Path(temp);_,cfg,sql_plan,plan=self.profile(root)
            original=driver.template(cfg,sql_plan,plan,{'key':'frozen-code','sha256':'f'*64})
            nic=next(value for value in original['resources'] if value['type']=='Microsoft.Network/networkInterfaces')
            nic['properties']['ipConfigurations'][0]['properties']['subnet']['id']=nic['properties']['ipConfigurations'][0]['properties']['subnet']['id'].replace('/virtualNetworks/sql/subnets/server','/virtualNetworks/subnets/sql/server')
            save(root/'driver-template.json',original)
            cli=Mock();cli.run.return_value={'tags':{'sebs.sql.owner':'owner','sebs.sql.storage':cfg.storage_id,'sebs.sql.role':'comparison-driver'}}
            cli.deploy.return_value={'properties':{'provisioningState':'Succeeded'}}
            driver.retry_failed_deployment(cli,cfg,sql_plan,root)
            document=cli.deploy.call_args.args[2]
            restored=json.loads(json.dumps(document))
            restored_nic=next(value for value in restored['resources'] if value['type']=='Microsoft.Network/networkInterfaces')
            restored_nic['properties']['ipConfigurations'][0]['properties']['subnet']['id']=nic['properties']['ipConfigurations'][0]['properties']['subnet']['id']
            self.assertEqual(restored,original)
            self.assertEqual(len(list((root/'driver-retries').glob('*.before.json'))),1)
            self.assertEqual(cli.run.call_count,4)
            cli.deploy.assert_called_once()
            # Existing resources without the driver role/owner cannot be adopted.
            cli.deploy.reset_mock();cli.run.return_value={'tags':{'sebs.sql.owner':'other'}}
            with self.assertRaisesRegex(ValueError,'ownership differs'):
                driver.retry_failed_deployment(cli,cfg,sql_plan,root)
            cli.deploy.assert_not_called()
