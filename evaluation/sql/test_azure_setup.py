"""Offline checks of Azure setup scripts, file identity and the Synapse adapter."""
import ast
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from evaluation.sql.azure_configure import INSTALL_PYTHON, launch_script, verify_blob_snapshot
from evaluation.sql.azure_remote_setup import setup_synapse
from evaluation.sql.service import queries
from evaluation.sql.synapse_client import (COLLATION, SynapseClient, odbc_value,
    query_sql, table_pattern, view_sql)


class AzureSetupTests(unittest.TestCase):
    def test_only_limit_syntax_changes_for_synapse(self):
        for query in ['q1', 'q6', 'q12']:
            self.assertEqual(query_sql(query), queries.QUERIES[query].strip())
        for query, limit in [('q3', 10), ('q21', 100)]:
            sql = query_sql(query)
            self.assertTrue(sql.startswith(f'SELECT TOP ({limit})'))
            self.assertNotIn('LIMIT', sql)
            original = queries.QUERIES[query].strip()
            self.assertEqual(sql.replace(f'SELECT TOP ({limit})', 'SELECT', 1),
                             original[:original.rfind('LIMIT')].rstrip())
        with self.assertRaises(KeyError):
            query_sql('arbitrary_sql')

    def test_folder_wildcards_require_exact_snapshot(self):
        container = Mock()
        container.list_blobs.return_value = [Mock(name='unused')]
        blob1, blob2 = Mock(), Mock()
        blob1.name, blob2.name = 'input/lineitem/a.parquet', 'input/lineitem/extra.parquet'
        container.list_blobs.return_value = [blob1]
        verify_blob_snapshot(container, {'lineitem': [blob1.name]})
        container.list_blobs.return_value = [blob1, blob2]
        with self.assertRaisesRegex(ValueError, 'differs'):
            verify_blob_snapshot(container, {'lineitem': [blob1.name]})
        container.list_blobs.return_value = []
        with self.assertRaises(ValueError):
            verify_blob_snapshot(container, {'lineitem': [blob1.name]})

    def test_view_paths_are_scoped_and_quoted(self):
        keys = ['input/lineitem/a.parquet', 'input/lineitem/b.parquet']
        self.assertEqual(table_pattern(keys), 'input/lineitem/*.parquet')
        sql = view_sql('lineitem', keys)
        self.assertIn("DATA_SOURCE = 'sebs_input'", sql)
        self.assertIn("FORMAT = 'PARQUET'", sql)
        self.assertNotIn('SECRET', sql)
        for bad in [[keys[0], 'other/b.parquet'], ["input/a';DROP.parquet"],
                    ['../lineitem/a.parquet'], [keys[0], keys[0]]]:
            with self.assertRaises(ValueError):
                table_pattern(bad)

    def test_remote_installer_and_embedded_python_parse(self):
        script = launch_script({'role': 'driver', 'sha256': 'a'*64,
                                'bundle_url': 'https://fixture.invalid/source?sig=fixture',
                                'private': {'sql_password': 'fixture-secret'}})
        result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        embedded = script.split("<<'SEBS_WRITE_SETUP'\n")[1].split('\nSEBS_WRITE_SETUP')[0]
        ast.parse(embedded)
        ast.parse(INSTALL_PYTHON)
        self.assertNotIn('fixture-secret', script)
        self.assertIn('checksum mismatch', INSTALL_PYTHON)
        self.assertIn("filter='data'", INSTALL_PYTHON)
        self.assertIn('CONFIGURE_ALREADY_RUNNING', script)
        self.assertNotIn('azure_replay', INSTALL_PYTHON)

    def test_sql_connection_value_escapes_closing_brace(self):
        self.assertEqual(odbc_value('pass;word}'), '{pass;word}}}')

    def test_setup_creates_metadata_views_without_analytical_queries(self):
        master, database = Mock(), Mock()
        master.cursor.return_value.fetchone.return_value = None
        database.cursor.return_value.fetchone.side_effect = [(COLLATION,), ('existing-key',), None, None]
        state = {'sql_database': 'sebs_sql', 'input_container_url': 'https://fixture/container'}
        private = {'sql_password': 'fixture-password', 'input_sas': 'fixture-sas'}
        with patch('evaluation.sql.azure_remote_setup.connect', side_effect=[master, database]):
            setup_synapse(state, private, {'input': {'tables': {'lineitem': ['in/lineitem/a.parquet']}}})
        statements = [call.args[0] for call in database.cursor.return_value.execute.call_args_list]
        self.assertTrue(any(sql.startswith('CREATE OR ALTER VIEW dbo.[lineitem]') for sql in statements))
        self.assertFalse(any('sum(l_extendedprice' in sql or 'l_shipdate >=' in sql for sql in statements))
        master.close.assert_called_once()
        database.close.assert_called_once()

    def test_synapse_failures_remain_failures_and_redact_password(self):
        client = SynapseClient({'sql_endpoint': 'fixture-ondemand.sql.azuresynapse.net',
                                'sql_database': 'sebs_sql'}, {'sql_password': 'fixture-secret'})
        with patch('evaluation.sql.synapse_client.connect', side_effect=RuntimeError('bad fixture-secret')):
            result = client.invoke('q6')
        self.assertTrue(result.stats.failure)
        self.assertIsNone(result.stats.cold_start)
        self.assertNotIn('fixture-secret', json.dumps(result.output))
        self.assertIsNone(result.output['provider_metrics']['billed_bytes'])

    def test_synapse_success_uses_fresh_connection_and_preserves_rows(self):
        client = SynapseClient({'sql_endpoint': 'fixture-ondemand.sql.azuresynapse.net',
                                'sql_database': 'sebs_sql'}, {})
        con = Mock()
        con.cursor.return_value.fetchall.return_value = [(42,)]
        with patch('evaluation.sql.synapse_client.connect', return_value=con) as connect:
            result = client.invoke('q6')
        connect.assert_called_once()
        con.close.assert_called_once()
        self.assertFalse(result.stats.failure)
        self.assertEqual(result.output['result'], {'queries': {'q6': [[42]]}})
        self.assertIn('/* sebs_request=', con.cursor.return_value.execute.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
