"""Offline remaining-query batching, failure preservation and no-replay checks."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.sql import postgres_azure_sf10_remaining as batch
from evaluation.sql.postgres_azure_baseline import save


class RemainingSF10Tests(unittest.TestCase):
    def test_only_remaining_queries_once_and_known_failure_does_not_disappear(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            folder=Path(temp)/'batch'
            def query(config,storage,cache,output,query,statement_timeout_ms):
                self.assertNotEqual(query,'q6');self.assertEqual(statement_timeout_ms,120000)
                if query=='q12':
                    save(output/'status.json',{'state':'failed'})
                    save(output/'receipt.json',{'state':'failed','exception':'QueryCanceled','invocation_id':query})
                else:
                    save(output/'status.json',{'state':'complete','valid':True})
                    save(output/'summary.json',{'state':'complete','valid':True,'sql_s':1,'function_s':2})
            with patch.object(batch,'start_query',side_effect=query) as submit:
                batch.run(Path('config'),Path('storage'),Path('cache'),folder)
                self.assertEqual(submit.call_count,4)
                self.assertEqual([call.kwargs['query'] for call in submit.call_args_list],list(batch.QUERIES))
                state=json.loads((folder/'status.json').read_text())
                self.assertEqual(state['completed_queries'],4)
                self.assertEqual(state['queries']['q12']['state'],'failed')
                with self.assertRaisesRegex(ValueError,'Recorded batch'):
                    batch.run(Path('config'),Path('storage'),Path('cache'),folder)
                self.assertEqual(submit.call_count,4)

    def test_unresolved_or_invalid_job_stops_before_next_query(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            folder=Path(temp)/'batch'
            def query(config,storage,cache,output,**kwargs):
                save(output/'status.json',{'state':'unresolved'})
            with patch.object(batch,'start_query',side_effect=query) as submit:
                with self.assertRaisesRegex(RuntimeError,'unknown q1'):
                    batch.run(Path('config'),Path('storage'),Path('cache'),folder)
                submit.assert_called_once()
                self.assertEqual(json.loads((folder/'status.json').read_text())['state'],'failed')
