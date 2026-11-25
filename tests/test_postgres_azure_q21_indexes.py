"""Offline fixed-profile Azure index maintenance and replay-safety checks."""
import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from evaluation.sql import postgres_azure_q21_indexes as indexes
from evaluation.sql.postgres_azure_baseline import ROOT, save


class AzureQ21IndexTests(unittest.TestCase):
    def report(self):
        return {'action':'prepare','namespace':'sebs_720_fixture','fingerprint':'f'*64,
            'steps':[],'before_table_bytes':{},'after_table_bytes':{},'visibility':[],
            'elapsed_seconds':1,'benchmark_queries_executed':0,
            'estimated_plan':[{'Plan':{'Node Type':'Index Only Scan','Index Name':'owned'}}]}

    def test_remote_profile_preserves_guards_and_valid_owner_json(self):
        payload={'namespace':'sebs_720_fixture','fingerprint':'f'*64,'action':'prepare',
            'indexes':indexes.INDEXES,'query':'SELECT 42','account_url':'https://owned.blob.core.windows.net',
            'report_key':'reports/test.json'}
        shell=indexes.script(payload)
        program=shell.split("<<'SEBS_AZURE_Q21_INDEXES'\n",1)[1].rsplit('\nSEBS_AZURE_Q21_INDEXES',1)[0]
        tree=ast.parse(program)
        self.assertNotIn('boto3.client',program)
        self.assertIn('ManagedIdentityCredential()',program)
        self.assertIn('A benchmark query is active',program)
        self.assertIn('Existing index is not owned',program)
        self.assertIn('at least 8 GiB free',program)
        self.assertIn('VACUUM (ANALYZE)',program)
        self.assertIn('EXPLAIN (FORMAT JSON)',program)
        self.assertNotIn('EXPLAIN ANALYZE',program)
        persist=next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='persist_owner')
        self.assertEqual(persist.body[0].value.args[0].right.value,'\n')
        with self.assertRaises(ValueError):indexes.script({**payload,'indexes':[]})

    def test_existing_owned_storage_and_report_prevent_repeat_maintenance(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            root=Path(temp);state=root/'storage';folder=root/'treatment'
            config=json.loads((ROOT/'configs/storage-postgresql-azure.example.json').read_text())
            config['sql']['postgresql'].update(state_dir=str(state),azure_subscription_id='11111111-1111-1111-1111-111111111111')
            save(root/'storage.json',config)
            save(state/'datasets/sebs_720_fixture.json',{'namespace':'sebs_720_fixture','fingerprint':'f'*64,'metadata':{'scale_factor':10}})
            backend=Mock();backend.info.return_value=({'storage_account':'owned'}, {})
            backend.control.return_value.__enter__=Mock(return_value=Mock())
            backend.control.return_value.__exit__=Mock(return_value=False)
            backend.blob.return_value.get_blob_client.return_value.download_blob.return_value.readall.return_value=json.dumps(self.report()).encode()
            with patch.object(indexes,'AzurePostgreSQL',return_value=backend) as constructor:
                indexes.apply('prepare',root/'storage.json',folder)
                backend.owned_group.assert_called_once()
                backend.command.assert_called_once()
                self.assertEqual(json.loads((folder/'prepare-plan.json').read_text())['indexes'],indexes.INDEXES)
                indexes.apply('prepare',root/'storage.json',folder)
                constructor.assert_called_once();backend.command.assert_called_once()
                save(folder/'remove-report.json',{'action':'remove'})
                with self.assertRaisesRegex(ValueError,'already removed'):
                    indexes.apply('prepare',root/'storage.json',folder)
                backend.command.assert_called_once()
