"""Offline tests for native Azure baseline sampling and replay guards."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.sql.postgres_azure_baseline import configuration, run, save, summarize, QUERIES


class AzureBaselineTests(unittest.TestCase):
    def test_configuration_selects_exact_query_and_policy_without_mutating_base(self):
        base={'experiments': {'benchmark-inputs': {'other': {'value': 1}}}}
        before=copy.deepcopy(base)
        value=configuration(base,'q21','reuse')
        self.assertEqual(base,before)
        self.assertEqual(value['experiments']['benchmark-inputs']['720.sql-postgresql'],
                         {'queries':['q21'],'connection_policy':'reuse','statement_timeout_ms':120000})
        self.assertEqual(value['experiments']['benchmark-inputs']['other'],{'value':1})
        with self.assertRaises(ValueError):configuration(base,'invalid','fresh')

    def test_summary_excludes_initial_and_separates_actual_cold_samples(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            folder=Path(temp)
            for policy in ('fresh','reuse'):
                for query in QUERIES:
                    values={}
                    for i in range(31):
                        values[str(i)]={'stats':{'failure':False,'cold_start':i<2},
                            'times':{'client_begin':f'{i:03}', 'benchmark':9000000 if i<2 else 1000000},
                            'provider_times':{'execution':0},
                            'output':{'result':{'connection_reused':policy=='reuse' and i>1,
                                'measurement':{query:{'query_time':500000,'connect_time':20000,'credential_time':0}}}}}
                    save(folder/policy/query/'results.json',{'_invocations':{'function':values}})
            summarize(folder)
            result=json.loads((folder/'summary.json').read_text())
            row=result['cells']['reuse/q6']
            self.assertEqual(row['all_attempts'],31)
            self.assertEqual(row['warm']['samples'],29)
            self.assertEqual(row['cold']['samples'],1)
            self.assertEqual(row['warm']['function_s'],1)
            self.assertEqual(row['warm']['connections_reused'],29)
            self.assertIsNone(row['warm']['provider_s'])
            path=folder/'fresh/q6/results.json'
            data=json.loads(path.read_text())
            data['_invocations']['function']['3']['stats']['failure']=True
            save(path,data)
            with self.assertRaisesRegex(ValueError,'Incomplete/failed'):summarize(folder)

    def test_recorded_run_never_resubmits(self):
        with tempfile.TemporaryDirectory() as temp,patch('subprocess.run') as command:
            folder=Path(temp);save(folder/'status.json',{'state':'failed'})
            with self.assertRaisesRegex(ValueError,'already recorded'):
                run(Path('absent'),Path('absent'),Path('absent'),folder)
            command.assert_not_called()
