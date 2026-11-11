import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import duckdb

PACKAGE = Path(__file__).resolve().parent.parent / "benchmarks/700.analytics/710.sql-query/python"


def load_function():
    # the function imports its storage wrapper relative to the deployed package
    package = types.ModuleType("sqlfunction")
    package.__path__ = [str(PACKAGE)]
    storage = types.ModuleType("sqlfunction.storage")
    storage.storage = types.SimpleNamespace(get_instance=lambda: None)
    sys.modules.update({"sqlfunction": package, "sqlfunction.storage": storage})
    spec = importlib.util.spec_from_file_location("sqlfunction.function", PACKAGE / "function.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


function = load_function()


class StorageAccessTests(unittest.TestCase):
    def access(self, env):
        with patch.dict(os.environ, env, clear=True):
            return function.storage_access()

    def test_aws(self):
        scheme, secret = self.access({"AWS_ACCESS_KEY_ID": "id", "AWS_SECRET_ACCESS_KEY": "key",
                                      "AWS_SESSION_TOKEN": "token", "AWS_REGION": "eu-north-1"})
        self.assertEqual(scheme, "s3")
        self.assertIn("SESSION_TOKEN 'token'", secret)

    def test_gcp_uses_hmac_key(self):
        scheme, secret = self.access({"GCS_HMAC_KEY_ID": "GOOG1ID", "GCS_HMAC_SECRET": "a/b+c"})
        self.assertEqual(scheme, "gs")
        con = duckdb.connect()
        con.execute("LOAD httpfs")
        con.execute(f"CREATE SECRET storage ({secret})")
        self.assertEqual(con.execute("SELECT type FROM duckdb_secrets()").fetchone()[0], "gcs")

    def test_httpfs_connection_and_views_use_the_same_provider_scheme(self):
        environments = [
            ({'AWS_ACCESS_KEY_ID': 'id', 'AWS_SECRET_ACCESS_KEY': 'key',
              'AWS_SESSION_TOKEN': 'token', 'AWS_REGION': 'eu-north-1'}, 's3', 's3'),
            ({'GCS_HMAC_KEY_ID': 'GOOG1ID', 'GCS_HMAC_SECRET': 'a/b+c'}, 'gs', 'gcs'),
            ({'MINIO_ADDRESS': 'host:9011', 'MINIO_ACCESS_KEY': 'a',
              'MINIO_SECRET_KEY': 'b'}, 's3', 's3'),
        ]
        for environment, scheme, secret_type in environments:
            with self.subTest(scheme=scheme, environment=list(environment)), \
                 tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, environment, clear=True):
                connection = Mock()
                with patch.object(function.duckdb, 'connect', return_value=connection):
                    self.assertIs(function.connect(directory, 'httpfs'), connection)
                function.create_views(connection, 'sebs-benchmarks-fixture',
                    {'lineitem': ['input/small/lineitem/data_0.parquet']}, 'httpfs', directory)
                statements = [call.args[0] for call in connection.execute.call_args_list]
                self.assertIn('LOAD httpfs', statements)
                self.assertTrue(any('CREATE SECRET storage (TYPE ' + secret_type + ',' in sql
                                    for sql in statements))
                self.assertIn(scheme + '://sebs-benchmarks-fixture/input/small/lineitem/data_0.parquet',
                              statements[-1])
                self.assertFalse(any('TYPE azure' in sql for sql in statements))

    def test_local_storage(self):
        scheme, secret = self.access({"MINIO_ADDRESS": "host:9011", "MINIO_ACCESS_KEY": "a",
                                      "MINIO_SECRET_KEY": "b"})
        self.assertEqual(scheme, "s3")
        self.assertIn("ENDPOINT 'host:9011'", secret)

    def test_unconfigured_platform(self):
        with self.assertRaisesRegex(RuntimeError, "download mode"):
            self.access({})

    def test_gcp_bucket_names_accepted(self):
        self.assertTrue(function.BUCKET_PATTERN.fullmatch("sebs-benchmarks-1a2b3c4d"))
        self.assertTrue(function.BUCKET_PATTERN.fullmatch("sebs-benchmarks-1a2b_3c4d"))
        self.assertFalse(function.BUCKET_PATTERN.fullmatch("other-bucket"))


if __name__ == "__main__":
    unittest.main()
