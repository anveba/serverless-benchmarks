"""Offline Azure diagnostic transport, identity checks and EXPLAIN-only safety."""
import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from evaluation.sql import postgres_azure_diagnose as diagnostics
from evaluation.sql.postgres_azure_baseline import ROOT, save


class AzurePostgresDiagnoseTests(unittest.TestCase):
    def test_remote_program_only_plans_benchmark_sql_and_keeps_credentials_local(self):
        payload={'namespace':'sebs_720_fixture','fingerprint':'f'*64,'queries':{'q6':'SELECT 42'},
            'account_url':'https://owned.blob.core.windows.net','report_key':'reports/test.json'}
        shell=diagnostics.script(payload)
        program=shell.split("<<'SEBS_AZURE_DIAGNOSTICS'\n",1)[1].rsplit('\nSEBS_AZURE_DIAGNOSTICS',1)[0]
        ast.parse(program)
        self.assertIn('EXPLAIN (FORMAT JSON)',program)
        self.assertNotIn('EXPLAIN ANALYZE',program)
        self.assertIn('SET TRANSACTION READ ONLY',program)
        self.assertIn('ManagedIdentityCredential()',program)
        self.assertNotIn('import boto3',program)
        self.assertNotIn('put_object(',program)
        with self.assertRaises(ValueError):diagnostics.script({**payload,'namespace':'public;DROP TABLE x'})

    def test_snapshot_uses_existing_vm_and_dataset_without_provisioning(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            root=Path(temp);state=root/'state'
            config=json.loads((ROOT/'configs/storage-postgresql-azure.example.json').read_text())
            config['sql']['postgresql'].update(state_dir=str(state),azure_subscription_id='11111111-1111-1111-1111-111111111111')
            save(root/'storage.json',config)
            save(state/'datasets/sebs_720_fixture.json',{'namespace':'sebs_720_fixture','fingerprint':'f'*64,'metadata':{'scale_factor':10}})
            report={'namespace':'sebs_720_fixture','fingerprint':'f'*64,
                'benchmark_queries_executed':0,'plans_execute_queries':False,
                'plans':{'q6':[{'Plan':{'Node Type':'Seq Scan','Relation Name':'lineitem'}}]}}
            for key in ('disk','memory','vmstat','container_stats','cgroups'):report[key]={'stdout':'snapshot'}
            for key in ('container','settings','database_bytes','database_counters','tables'):report[key]={}
            backend=Mock();backend.info.return_value=({'storage_account':'owned','resource_group':'group'},{'VmId':'/vm'})
            cli=Mock();cli.run.side_effect=[{'hardwareProfile':{'vmSize':'Standard_D2s_v5'},
                'storageProfile':{'osDisk':{'managedDisk':{'id':'/disk'},'caching':'ReadWrite'}}},
                {'sku':{'name':'StandardSSD_LRS'},'diskSizeGb':64}]
            backend.control.return_value.__enter__=Mock(return_value=cli)
            backend.control.return_value.__exit__=Mock(return_value=False)
            backend.blob.return_value.get_blob_client.return_value.download_blob.return_value.readall.return_value=json.dumps(report).encode()
            with patch.object(diagnostics,'AzurePostgreSQL',return_value=backend):
                diagnostics.diagnose(root/'storage.json',root/'reports','test')
            backend.owned_group.assert_called_once()
            backend.command.assert_called_once()
            actual=json.loads((root/'reports/test.json').read_text())
            self.assertEqual(actual['azure_resources']['disk']['sku']['name'],'StandardSSD_LRS')
            self.assertEqual(actual['benchmark_queries_executed'],0)
            self.assertEqual(cli.run.call_count,2)
            self.assertTrue(all(call.args[0][1]=='show' for call in cli.run.call_args_list))
