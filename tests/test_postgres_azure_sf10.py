"""SF10 single-attempt safety and outcome classification, entirely offline."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evaluation.sql.postgres_azure_sf10 import outcome, run
from evaluation.sql.postgres_azure_baseline import ROOT, save


class AzurePostgresSF10Tests(unittest.TestCase):
    def invocation(self,query,failed=False):
        return {'request_id':'id-'+query,'stats':{'failure':failed,'cold_start':False},
            'times':{'client_begin':'2026-11-22','benchmark':2000000},
            'output':{'result':{'measurement':{query:{'query_time':1000000}}}}}

    def test_unknown_failure_invalid_and_valid_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)
            self.assertEqual(outcome(path)['outcome'],'unknown')
            save(path/'experiments.json',{'_invocations':{'f':{'id':self.invocation('q6',True)}}})
            self.assertEqual(outcome(path)['outcome'],'execution_failure')
            save(path/'experiments.json',{'_invocations':{'f':{'id':self.invocation('q6')}}})
            (path/'out.log').write_text('output validation failed')
            self.assertEqual(outcome(path)['outcome'],'invalid')
            (path/'out.log').write_text('output validation passed')
            self.assertEqual(outcome(path)['sql_s'],1)

    def test_unknown_http_outcome_stops_batch_and_prevents_resubmission(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            root=Path(temp);output=root/'run'
            base=json.loads((ROOT/'configs/sql-postgresql-azure.example.json').read_text())
            save(root/'config.json',base)
            save(root/'storage.json',{'sql':{'postgresql':{'provider':'azure', 'region':'swedencentral',
                'azure_subscription_id':base['deployment']['azure']['credentials']['subscription_id']}}})
            def command(args,**kwargs):
                if args[5]=='process':return SimpleNamespace(returncode=0)
                self.assertEqual(args[5],'invoke')
                self.assertEqual(args[7],'large')
                path=Path(args[args.index('--output-dir')+1])
                query=json.loads(Path(args[args.index('--config')+1]).read_text())['experiments']['benchmark-inputs']['720.sql-postgresql']['queries'][0]
                if query=='q1':return SimpleNamespace(returncode=1)
                save(path/'experiments.json',{'_invocations':{'f':{'id':self.invocation(query)}}})
                (path/'out.log').write_text('output validation passed')
                return SimpleNamespace(returncode=0)
            with patch('evaluation.sql.postgres_azure_sf10.subprocess.run',side_effect=command) as invoke:
                with self.assertRaisesRegex(RuntimeError,'needs inspection'):
                    run(root/'config.json',root/'storage.json',root/'cache',output)
                self.assertEqual(invoke.call_count,3)
                state=json.loads((output/'status.json').read_text())
                self.assertEqual(state['completed_queries'],1)
                self.assertEqual(state['queries']['q1']['outcome'],'unknown')
                with self.assertRaisesRegex(ValueError,'Recorded/nonempty'):
                    run(root/'config.json',root/'storage.json',root/'cache',output)
                self.assertEqual(invoke.call_count,3)
