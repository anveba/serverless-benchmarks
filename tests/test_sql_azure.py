"""Offline coverage of native Azure SQL input, credentials, paths and packaging."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import duckdb  # Load native module before temporary sys.modules patches.

from sebs.experiments.config import Config

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / 'benchmarks/700.analytics/710.sql-query'


def load_function():
    name = 'sql_azure_test_function'
    package = types.ModuleType(name)
    package.__path__ = [str(BENCHMARK / 'python')]
    storage = types.ModuleType(name + '.storage')
    storage.storage = Mock()
    with patch.dict(sys.modules, {name: package, name + '.storage': storage}):
        spec = importlib.util.spec_from_file_location(name + '.function', BENCHMARK / 'python/function.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class AzureSqlTests(unittest.TestCase):
    def test_template_selects_azure_http_and_explicit_native_access(self):
        config = json.loads((ROOT / 'configs/sql-azure-workload.example.json').read_text())
        experiment = Config.deserialize(config['experiments'])
        self.assertEqual(experiment.benchmark_input('710.sql-query'), {'access_mode': 'azure', 'queries': ['q6']})
        self.assertEqual(experiment.experiment_settings('sql-workload')['trigger'], 'http')
        self.assertEqual(config['deployment']['name'], 'azure')

    def test_native_blob_paths_do_not_download_whole_objects(self):
        function = load_function()
        connection = Mock()
        function.create_views(connection, 'sebs-benchmarks-abc',
            {'lineitem': ['in/small/lineitem/data_0.parquet']}, 'azure', '/tmp/unused')
        self.assertIn('az://sebs-benchmarks-abc/in/small/lineitem/data_0.parquet',
                      connection.execute.call_args.args[0])
        function.client.download.assert_not_called()

    def test_azure_uses_sebs_setting_and_does_not_create_s3_secret(self):
        function = load_function()
        connection = Mock()
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'STORAGE_CONNECTION_STRING': "AccountKey=abc'def"}), \
                 patch.object(function.duckdb, 'connect', return_value=connection):
                function.connect(directory, 'azure')
        statements = [c.args[0] for c in connection.execute.call_args_list]
        self.assertIn('LOAD azure', statements)
        self.assertIn("SET azure_transport_option_type='curl'", statements)
        transport = statements.index("SET azure_transport_option_type='curl'")
        secret = next(i for i, statement in enumerate(statements) if 'TYPE azure' in statement)
        self.assertLess(transport, secret)
        self.assertTrue(any("CONNECTION_STRING 'AccountKey=abc''def'" in s for s in statements))
        self.assertFalse(any('TYPE s3' in s for s in statements))

    def test_initialization_errors_do_not_expose_connection_string(self):
        function = load_function()
        connection = Mock()
        connection.execute.side_effect = RuntimeError('secret-that-must-not-escape')
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'STORAGE_CONNECTION_STRING': 'secret-that-must-not-escape'}), \
                 patch.object(function.duckdb, 'connect', return_value=connection):
                with self.assertRaisesRegex(RuntimeError, 'Azure storage initialization failed') as error:
                    function.connect(directory, 'azure')
        self.assertNotIn('secret-that-must-not-escape', str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        connection.close.assert_called_once()

    def test_missing_azure_setting_fails_explicitly(self):
        function = load_function()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'STORAGE_CONNECTION_STRING'):
                function.azure_secret()

    def test_unknown_mode_rejected_before_sql(self):
        function = load_function()
        with self.assertRaisesRegex(ValueError, 'access mode'):
            function.validate_event({'access_mode': 'typo'})

    def test_build_selects_only_provider_extension(self):
        script = BENCHMARK / 'python/package.sh'
        subprocess.run(['bash', '-n', str(script)], check=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'requirements.txt').write_text('duckdb==2.0.0\n')
            fake = path / 'python3'
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\ncat >/dev/null\n')
            fake.chmod(0o755)
            for provider, extension in [('AZURE', 'azure'), ('AWS', 'httpfs')]:
                capture = path / 'arguments'
                env = {**os.environ, 'PLATFORM': provider, 'TARGET_ARCHITECTURE': 'x64',
                       'PATH': str(path) + os.pathsep + os.environ['PATH'], 'CAPTURE': str(capture)}
                subprocess.run(['bash', str(script), str(path / 'package')], cwd=path, env=env, check=True)
                args = capture.read_text()
                self.assertIn(f'https://extensions.duckdb.org/v2.0.0/linux_amd64/{extension}.duckdb_extension.gz', args)
                self.assertIn(f'/{extension}.duckdb_extension', args)


if __name__ == '__main__':
    unittest.main()
