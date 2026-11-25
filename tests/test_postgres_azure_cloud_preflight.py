"""Offline paired-driver source, quota, live-profile and no-write preflight tests."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from evaluation.sql import postgres_azure_cloud_preflight as preflight
from evaluation.sql.postgres_azure_baseline import ROOT, save


class AzurePostgresCloudPreflightTests(unittest.TestCase):
    def sku(self):
        return {'name':'Standard_D2s_v5','resourceType':'virtualMachines','family':'standardDSv5Family',
            'restrictions':[],'capabilities':[{'name':'vCPUs','value':'2'},{'name':'MemoryGB','value':'8'}]}

    def quotas(self,used='2'):
        return [{'name':{'value':key},'currentValue':used,'limit':'4'} for key in ('cores','standardDSv5Family')]

    def test_quotas_handle_strings_and_enforce_capacity_without_reserving_it(self):
        _,quotas=preflight.quota_report(self.sku(),self.quotas())
        self.assertTrue(all(item['headroom'] for item in quotas))
        _,quotas=preflight.quota_report(self.sku(),self.quotas('4'))
        self.assertFalse(any(item['headroom'] for item in quotas))
        with self.assertRaisesRegex(ValueError,'Missing regional'):
            preflight.quota_report(self.sku(),self.quotas()[:1])
        sku=self.sku();sku['restrictions']=[{'type':'Location'}]
        with self.assertRaisesRegex(ValueError,'unavailable'):preflight.quota_report(sku,self.quotas())

    def test_cached_package_requires_exact_source_members(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'repo';cache=Path(temp)/'cache'
            function={'benchmark':'720.sql-postgresql','name':'function','hash':'expected',
                'config':{'architecture':'x64','runtime':{'version':'3.11'},'memory':512}}
            save(cache/'720.sql-postgresql/config.json',{'azure':{'python':{'functions':{'function':function},
                'code_package':{'default':{'3.11':{'x64':{'hash':'expected','location':'package'}}}}}}})
            archive=cache/'package/720.sql-postgresql.zip';archive.parent.mkdir(parents=True)
            with ZipFile(archive,'w') as files:
                for member,source in preflight.SOURCES.items():
                    value=('# '+source+'\n').encode()
                    path=root/source;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(value)
                    files.writestr(member,value)
                files.writestr('.python_packages/lib/site-packages/psycopg-3.3.9.dist-info/METADATA','Version: 3.3.9')
            with patch.object(preflight.Benchmark,'hash_directory',return_value='expected'):
                _,_,digest,hashes=preflight.source_package(cache,root)
                self.assertEqual(digest,hashlib.sha256(archive.read_bytes()).hexdigest())
                self.assertEqual(len(hashes),4)
                (root/preflight.SOURCES['handler/queries.py']).write_text('changed')
                with self.assertRaisesRegex(ValueError,'runtime member differs'):
                    preflight.source_package(cache,root)

    def test_live_function_verifies_latest_publication_and_never_saves_sas(self):
        cfg=SimpleNamespace(storage_id='storage',region='swedencentral')
        plan={'owner':'owner','outputs':{'FunctionSubnetId':'/subnet','SecretUri':'https://vault/secrets/reader',
              'FunctionIdentityClientId':'identity'}}
        function={'name':'function','sql_resources':{'storage_id':'storage','owner':'owner',
            'app_id':'/subscriptions/sub/resourceGroups/group/providers/Microsoft.Web/sites/function'}}
        runtime={'type':'postgresql','provider':'azure','secret_uri':'https://vault/secrets/reader','identity_client_id':'identity'}
        app={'location':'Sweden Central','tags':{'sebs.sql.owner':'owner'},'properties':{'state':'Running',
            'defaultHostName':'function.azurewebsites.net','virtualNetworkSubnetId':'/SUBNET',
            'functionAppConfig':{'runtime':{'name':'python','version':'3.11'},
                'scaleAndConcurrency':{'instanceMemoryMB':512,'alwaysReady':[]}}}}
        digest='a'*64;publication='sebs-publish-'+digest[:12]
        cli=Mock();cli.secrets=[]
        cli.run.side_effect=[app,{'properties':{'SEBS_SQL_CONFIG':json.dumps(runtime),'OTHER_SECRET':'DO_NOT_SAVE'}},
            [{'name':publication,'properties':{'timestamp':'2026-11-23','provisioningState':'Succeeded'}}],
            {'resources':[{'name':'function/onedeploy','type':'Microsoft.Web/sites/extensions','properties':{
                'packageUri':'https://owned.blob.core.windows.net/sebs-deploy/packages/'+digest+'.zip?sig=DO_NOT_SAVE'}}]}]
        value=preflight.verify_live_function(cli,cfg,plan,function,digest)
        self.assertEqual(value['code_account'],'owned')
        self.assertNotIn('DO_NOT_SAVE',json.dumps(value))
        self.assertIn('DO_NOT_SAVE',cli.secrets)
        # A different newer archive is rejected before any comparison can start.
        cli.run.side_effect=[app,{'properties':{'SEBS_SQL_CONFIG':json.dumps(runtime)}},
            [{'name':'sebs-publish-newer','properties':{'timestamp':'2026-11-24','provisioningState':'Succeeded'}}],
            {'resources':[{'name':'function/onedeploy','type':'Microsoft.Web/sites/extensions','properties':{'packageUri':'other'}}]}]
        with self.assertRaisesRegex(ValueError,'Latest native publication'):
            preflight.verify_live_function(cli,cfg,plan,function,digest)

    def test_full_snapshot_only_reads_existing_resources(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            root=Path(temp);state=root/'storage'
            storage=json.loads((ROOT/'configs/storage-postgresql-azure.example.json').read_text())
            subscription='11111111-1111-1111-1111-111111111111'
            storage['sql']['postgresql'].update(state_dir=str(state),storage_id='a'*12,azure_subscription_id=subscription)
            save(root/'storage.json',storage)
            base=json.loads((ROOT/'configs/sql-postgresql-azure.example.json').read_text())
            base['deployment']['azure']['credentials']['subscription_id']=subscription
            save(root/'config.json',base)
            save(state/'datasets/sebs_720_fixture.json',{'namespace':'sebs_720_fixture','fingerprint':'f'*64,
                'metadata':{'scale_factor':1,'input_size':'small','workload':'TPC-H'}})
            archive=root/'archive.zip';archive.write_bytes(b'zip');digest=hashlib.sha256(b'zip').hexdigest()
            function={'name':'function','sql_resources':{'app_id':'/function'}}
            plan={'owner':'owner','resource_group':'group','server_ip':'10.73.1.4','server_subnet_prefix':'10.73.1.0/24',
                  'resolved_image':'Canonical:ubuntu:server:frozen'}
            outputs={'VmId':'/vm','FunctionIdentityId':'/identity','FunctionIdentityClientId':'client'}
            backend=Mock();backend.info.return_value=(plan,outputs)
            cli=Mock();cli.secrets=[]
            cli.run.side_effect=[{'tags':{'sebs.sql.owner':'owner'}},[{'value':'DO_NOT_SAVE'}],
                {'instanceView':{'statuses':[{'code':'PowerState/running'}]}},[self.sku()],self.quotas(),{'hyperVGeneration':'V2'}]
            backend.control.return_value.__enter__=Mock(return_value=cli)
            backend.control.return_value.__exit__=Mock(return_value=False)
            blob=Mock();blob.get_blob_client.return_value.download_blob.return_value.chunks.return_value=[b'zip']
            live={'endpoint':'https://function','code_account':'owned','code_group':'group','runtime_config':{}}
            with patch.object(preflight,'source_package',return_value=(function,archive,digest,{})), \
                 patch.object(preflight,'AzurePostgreSQL',return_value=backend), \
                 patch.object(preflight,'verify_live_function',return_value=live), \
                 patch.object(preflight,'can_assign_roles',return_value=True), \
                 patch('azure.storage.blob.BlobServiceClient',return_value=blob):
                report=preflight.preflight(root/'config.json',root/'storage.json',root/'cache',root/'comparison')
            self.assertTrue(report['ready_to_plan_driver'])
            self.assertEqual(report['input']['scale_factor'],1)
            self.assertNotIn('DO_NOT_SAVE',json.dumps(report))
            self.assertEqual(report['driver_python_expected'],'3.11')
            backend.command.assert_not_called()
            self.assertFalse(any(word in call.args[0] for call in cli.run.call_args_list
                                 for word in ('create','update','delete','start','set')))
