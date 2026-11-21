"""Aggregate export must exclude runtime identities and reject incomplete input."""
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.sql.postgres_export_results import export, QUERIES, RUNS


class ExportTests(unittest.TestCase):
    def data(self,path):
        path=str(path)
        private='PRIVATE_RUNTIME_IDENTIFIER'
        if path.endswith('verification.json'):
            return {'all_checked_resources_removed':True,'findings':{'resource':'absent'},'checked_at':'2026-11-21T17:13:15Z'}
        if path.endswith('archive-complete.json'):return {'reports':1611,'identity':private}
        if 'baseline-' in path:
            return {'_invocations':{'private-function':{str(i):{'request_id':private,'stats':{'failure':False,'cold_start':False},
                'times':{'client_begin':f'{i:02}'},'provider_times':{'execution':1000},
                'output':{'result':{'measurement':{q:{'query_time':100,'connect_time':10} for q in QUERIES}}}} for i in range(31)}}}
        if path.endswith('comparison-summary.json'):
            value={key:1 for key in ['mean_s','median_s','p95_s','mean_queue_s','mean_run_seconds']}
            value.update(requests=300,valid=300,failures=0,per_query={},cold_starts=1,billed_gb_seconds=10,native_billing_complete=True)
            return {'allocation':{'instance_id':private},'backends':{'postgresql':copy.deepcopy(value),'lambda':value}}
        if path.endswith('summary.json'):
            item={key:1 for key in ['samples','attempts','all_attempts_gb_seconds','aws_execution_s',
                'aws_initialization_ms','query_fetch_s','credentials_ms','connect_ms']}
            return {'queries':{q:{p:copy.deepcopy(item) for p in ['cold','warm']} for q in QUERIES},'all_attempts_gb_seconds':54.633}
        if path.endswith('cost-report.json'):
            return {'snapshot':private,'basis':'List prices','rates':{},'exclusions':[],
                'workloads':[],'campaign':{'actual_invoice_complete':False},'known_lambda':{
                    'requests_with_native_billing':1611,'gb_seconds':4185.5975,'coverage_complete':False,
                    'modeled_subtotal_usd':.07,'note':'Known saved usage only','request_ids_without_saved_billing':[private]}}
        raise AssertionError(path)

    def test_export_keeps_aggregates_and_omits_private_runtime_fields(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            output=Path(temp)/'summary.json'
            with patch('evaluation.sql.postgres_export_results.read',side_effect=self.data):
                report=export(Path('results'),Path('snapshot'),output)
            self.assertNotIn('PRIVATE_RUNTIME_IDENTIFIER',output.read_text())
            self.assertNotIn('private-function',output.read_text())
            self.assertEqual(set(report['workloads']),set(RUNS))
            self.assertEqual(report['baseline_fresh_reuse']['fresh']['q6']['retained_samples'],30)
            self.assertFalse(report['cost_model']['known_lambda']['coverage_complete'])

    def test_incomplete_cleanup_refuses_export(self):
        with patch('evaluation.sql.postgres_export_results.read',return_value={'all_checked_resources_removed':False}):
            with self.assertRaisesRegex(ValueError,'cleanup verification'):
                export(Path('results'),Path('snapshot'),Path('unused'))


if __name__=='__main__':unittest.main()
