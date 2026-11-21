"""PostgreSQL benchmark and shared runtime checks, without network or databases."""
import datetime
import decimal
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import sys
import shutil
import ssl
import subprocess
import tempfile
from threading import Event, Barrier
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

from sebs.benchmark import Benchmark
from sebs.experiments.config import Config
from sebs.sebs_types import BenchmarkModule, Language

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks/700.analytics/720.sql-postgresql"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


pg_input = module("native_pg_input", BENCHMARK / "input.py")
with patch.dict(
    sys.modules,
    {"psycopg": types.SimpleNamespace(__version__="fixture", connect=Mock())},
):
    wrapper = module(
        "native_pg_wrapper", ROOT / "benchmarks/wrappers/common/python/sql.py"
    )
package = types.ModuleType("native_pg_function")
package.__path__ = [str(BENCHMARK / "python")]
sys.modules[package.__name__] = package
sys.modules[package.__name__ + ".sql"] = wrapper
function = module(package.__name__ + ".function", BENCHMARK / "python/function.py")


def event(policy="fresh"):
    return {
        "queries": ["q6"],
        "namespace": "sebs_720_fixture",
        "input_fingerprint": "a" * 64,
        "scale_factor": 0.01,
        "connection_policy": policy,
        "statement_timeout_ms": 1000,
    }


class InputTests(unittest.TestCase):
    def test_input_generation_prepares_sql_storage_directly(self):
        storage = Mock()
        storage.prepare_dataset.return_value = {
            "namespace": "sebs_720_fixture",
            "input_fingerprint": "a" * 64,
        }
        ref = pg_input.reference()
        with patch.object(pg_input, "reference", return_value=ref), patch.object(
            ref, "_parquet_files", return_value={"lineitem": [Path("/fixture.parquet")]}
        ):
            result = pg_input.generate_input(
                None,
                "test",
                None,
                [],
                [],
                None,
                None,
                {"queries": ["q3"], "connection_policy": "reuse"},
                sql_storage=storage,
                replace_existing=True,
            )
        self.assertEqual(result["queries"], ["q3"])
        self.assertEqual(result["scale_factor"], 0.01)
        self.assertEqual(result["connection_policy"], "reuse")
        args = storage.prepare_dataset.call_args
        self.assertEqual(args.args[:2], ("720.sql-postgresql", "test"))
        self.assertEqual(
            args.args[2]["lineitem"]["primary_key"], ["l_orderkey", "l_linenumber"]
        )
        self.assertTrue(args.kwargs["replace"])

    def test_no_manifest_or_setup_script_required(self):
        with self.assertRaisesRegex(RuntimeError, "SQL storage"):
            pg_input.generate_input(None, "test", None, [], [], None, None)
        with self.assertRaises(ValueError):
            pg_input.generate_input(
                None,
                "test",
                None,
                [],
                [],
                None,
                None,
                {"manifest": "old.json"},
                sql_storage=Mock(),
            )

    def test_invalid_selection_fails_before_loading(self):
        for params in [
            {"queries": ["q6", "q1"]},
            {"queries": ["q0"]},
            {"connection_policy": "pool"},
            {"statement_timeout_ms": True},
            [],
            {"queries": [{}]},
        ]:
            storage = Mock()
            with self.subTest(params=params), self.assertRaises(ValueError):
                pg_input.generate_input(
                    None, "test", None, [], [], None, None, params, sql_storage=storage
                )
            storage.prepare_dataset.assert_not_called()

    def test_sql_text_matches_duckdb_without_parallel_stage_definitions(self):
        other = module(
            "duck_statements",
            ROOT / "benchmarks/700.analytics/710.sql-query/python/queries.py",
        )
        current = module("pg_statements", BENCHMARK / "python/queries.py")
        self.assertEqual(current.QUERIES, other.QUERIES)
        self.assertFalse(hasattr(current, "STAGES"))

    def test_framework_injects_storage_and_records_module_usage(self):
        benchmark = Benchmark.__new__(Benchmark)
        benchmark._benchmark = "720.sql-postgresql"
        benchmark._benchmark_data_path = None
        benchmark._experiment_config = Config()
        benchmark._cache_client = Mock()
        benchmark._benchmark_config = types.SimpleNamespace(modules=[BenchmarkModule.SQL])
        calls = []

        def generate(*args, sql_storage=None, replace_existing=False):
            calls.append((sql_storage, replace_existing))
            return {"queries": ["q6"]}

        benchmark._benchmark_input_module = types.SimpleNamespace(
            allocate_sql=lambda: {}, generate_input=generate
        )
        resources = Mock()
        benchmark.prepare_input(resources, "test", replace_existing=True)
        self.assertEqual(calls, [(resources.get_sql_storage.return_value, True)])
        self.assertTrue(benchmark.uses_sql)
        resources.get_storage.assert_not_called()
        resources.get_nosql_storage.assert_not_called()

    def test_cache_hash_tracks_sql_wrapper_only_for_sql_benchmarks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared = root/'shared.py'
            shared.write_text('version_one')
            language_dir = root/'benchmark'/'python'
            language_dir.mkdir(parents=True)
            (language_dir/'function.py').write_text('benchmark_source')
            config_path = language_dir.parent/'config.json'
            def resource(*parts):
                return shared if 'common' in parts else root/'missing'/'*.py'
            with patch('sebs.benchmark.get_resource_path',side_effect=resource):
                config_path.write_text(json.dumps({'modules':['sql']}))
                sql_one = Benchmark.hash_directory(str(language_dir),'aws',Language.PYTHON)
                config_path.write_text(json.dumps({'modules':[]}))
                plain_one = Benchmark.hash_directory(str(language_dir),'aws',Language.PYTHON)
                shared.write_text('version_two')
                plain_two = Benchmark.hash_directory(str(language_dir),'aws',Language.PYTHON)
                config_path.write_text(json.dumps({'modules':['sql']}))
                sql_two = Benchmark.hash_directory(str(language_dir),'aws',Language.PYTHON)
            self.assertEqual(plain_one,plain_two)
            self.assertNotEqual(sql_one,sql_two)

    def test_sql_wrapper_is_packaged_only_for_sql_module(self):
        from sebs.config import SeBSConfig

        benchmark = Benchmark.__new__(Benchmark)
        benchmark._language = Language.PYTHON
        benchmark._deployment_name = "aws"
        benchmark._system_config = SeBSConfig()
        for modules in [[], [BenchmarkModule.SQL]]:
            benchmark._benchmark_config = types.SimpleNamespace(modules=modules)
            with tempfile.TemporaryDirectory() as folder:
                benchmark.add_deployment_files(folder)
                self.assertEqual((Path(folder) / "sql.py").exists(), bool(modules))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.client = wrapper.sql()
        self.connection = MagicMock(
            closed=False, info=types.SimpleNamespace(server_version=160016)
        )
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = ["a" * 64]
        self.cursor.fetchall.return_value = [
            (decimal.Decimal("12.34"), datetime.date(1995, 1, 1))
        ]
        self.credentials = patch.object(
            self.client, "credentials", return_value=({"host": "fixture"}, True)
        )
        self.credentials.start()
        self.addCleanup(self.credentials.stop)
        self.driver = patch.object(
            wrapper.psycopg, "connect", return_value=self.connection
        )
        self.connect = self.driver.start()
        self.addCleanup(self.driver.stop)

    def query(self, policy="fresh"):
        return self.client.query(
            "SELECT approved_statement", "sebs_720_fixture", "a" * 64, policy=policy
        )

    def test_fresh_is_read_only_and_closed(self):
        result = self.query()
        self.assertEqual(result["rows"], [[12.34, "1995-01-01"]])
        self.assertFalse(result["connection_reused"])
        self.connection.close.assert_called_once()
        self.assertIn(
            "default_transaction_read_only=on", self.connect.call_args.kwargs["options"]
        )
        self.assertIsNone(self.connect.call_args.kwargs["prepare_threshold"])
        self.assertEqual(
            set(result["measurement"]),
            {
                "credential_time",
                "connect_time",
                "setup_time",
                "query_time",
                "handler_time",
            },
        )

    def test_reuse_is_explicit_and_per_environment(self):
        first = self.query("reuse")
        second = self.query("reuse")
        self.assertFalse(first["connection_reused"])
        self.assertTrue(second["connection_reused"])
        self.connect.assert_called_once()
        self.connection.close.assert_not_called()

    def test_closed_connection_replaced_before_execution(self):
        self.query("reuse")
        self.connection.closed = True
        self.query("reuse")
        self.assertEqual(self.connect.call_count, 2)
        self.assertEqual(
            sum(
                call.args[0] == "SELECT approved_statement"
                for call in self.cursor.execute.call_args_list
            ),
            2,
        )

    def test_dataset_mismatch_prevents_query(self):
        self.cursor.fetchone.return_value = ["b" * 64]
        with self.assertRaises(RuntimeError):
            self.query()
        self.assertFalse(
            any(
                call.args[0] == "SELECT approved_statement"
                for call in self.cursor.execute.call_args_list
            )
        )

    def test_failure_redacted_and_never_retried(self):
        self.cursor.fetchall.side_effect = RuntimeError("password=private-value")
        with self.assertRaises(RuntimeError) as caught:
            self.query("reuse")
        self.assertNotIn("private-value", str(caught.exception))
        self.assertIsNone(self.client._connection)
        self.assertIsNone(self.client._credential_cache)
        self.connect.assert_called_once()

    def test_namespace_injection_rejected_before_credentials(self):
        with self.assertRaises(ValueError):
            self.client.query("SELECT 1", "sebs_x;DROP SCHEMA public", "a" * 64)
        self.client.credentials.assert_not_called()

    def test_benchmark_selects_one_query_and_delegates_to_wrapper(self):
        client = Mock()
        client.query.return_value = {
            "rows": [[42]],
            "measurement": {"query_time": 1},
            "engine": "postgresql",
        }
        with patch.object(function, "client", client):
            result = function.handler(event())
        self.assertEqual(result["result"]["queries"], {"q6": [[42]]})
        self.assertEqual(client.query.call_args.args[0], function.QUERIES["q6"])
        for bad in [[], ["q6", "q1"], ["q999"], [{}]]:
            with self.subTest(bad=bad), patch.object(function, "client", client):
                with self.assertRaises(ValueError):
                    function.handler({**event(), "queries": bad})


class CredentialsTests(unittest.TestCase):
    def test_aws_secret_cached_and_tls_verified(self):
        client = wrapper.sql()
        secret = {
            "host": "10.0.0.1",
            "port": 5432,
            "dbname": "sebs",
            "user": "sebs_reader",
            "password": "private-value",
            "ca_pem": "fixture-certificate",
        }
        aws = Mock()
        aws.get_secret_value.return_value = {"SecretString": json.dumps(secret)}
        with tempfile.TemporaryDirectory() as folder, patch.object(
            wrapper, "Path", side_effect=lambda _: Path(folder)
        ), patch("boto3.client", return_value=aws), patch.dict(
            os.environ,
            {
                "SEBS_SQL_CONFIG": json.dumps(
                    {
                        "type": "postgresql",
                        "secret_arn": "arn:fixture",
                        "region": "us-east-1",
                    }
                )
            },
        ):
            first, hit = client.credentials()
            second, again = client.credentials()
            self.assertFalse(hit)
            self.assertTrue(again)
            self.assertEqual(first["sslmode"], "verify-full")
            self.assertEqual(Path(first["sslrootcert"]).stat().st_mode & 0o777, 0o600)
            aws.get_secret_value.assert_called_once_with(SecretId="arn:fixture")

    def test_docker_settings_need_no_cloud_sdk_call(self):
        config = {
            "type": "postgresql",
            "host": "172.17.0.2",
            "port": 5432,
            "dbname": "sebs",
            "user": "sebs_reader",
            "password": "local-private",
            "sslmode": "disable",
        }
        with patch.dict(os.environ, {"SEBS_SQL_CONFIG": json.dumps(config)}), patch(
            "boto3.client"
        ) as aws:
            settings, hit = wrapper.sql().credentials()
        self.assertEqual(settings["host"], "172.17.0.2")
        aws.assert_not_called()


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is needed for real CA-reader checks")
class AtomicCertificateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certificates = tempfile.TemporaryDirectory(prefix="sebs-ca-fixture-")
        root = Path(cls.certificates.name)
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(root / "key.pem"), "-out", str(root / "ca.pem"),
            "-subj", "/CN=SeBS disposable test CA", "-addext", "basicConstraints=critical,CA:TRUE",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.pem = (root / "ca.pem").read_text()

    @classmethod
    def tearDownClass(cls):
        cls.certificates.cleanup()

    def verify_ca(self, path):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=str(path))

    def test_previous_ca_remains_readable_while_replacement_waits_to_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "ca.crt"
            target.write_text(self.pem)
            waiting, publish = Event(), Event()
            replace = os.replace
            def delayed_replace(source, destination):
                waiting.set()
                if not publish.wait(timeout=5):
                    raise TimeoutError("Test publication did not resume")
                replace(source, destination)
            with patch.object(wrapper.os, "replace", side_effect=delayed_replace):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(wrapper._publish_ca, target, self.pem + "\n")
                    try:
                        self.assertTrue(waiting.wait(timeout=5))
                        self.assertEqual(target.read_text(), self.pem)
                        self.verify_ca(target)
                    finally:
                        publish.set()
                    future.result(timeout=5)
            self.assertEqual(target.read_text(), self.pem + "\n")
            self.verify_ca(target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_failed_publication_keeps_good_ca_and_removes_partial_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "ca.crt"
            target.write_text(self.pem)
            with patch.object(wrapper.os, "replace", side_effect=PermissionError("fixture")):
                with self.assertRaises(PermissionError):
                    wrapper._publish_ca(target, self.pem + "\n")
            self.assertEqual(target.read_text(), self.pem)
            self.verify_ca(target)
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_eight_writers_do_not_expose_partial_pem_to_real_ssl_readers(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "ca.crt"
            target.write_text(self.pem)
            barrier = Barrier(9)
            def writer():
                barrier.wait(timeout=5)
                for _ in range(10):
                    wrapper._publish_ca(target, self.pem)
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(writer) for _ in range(8)]
                barrier.wait(timeout=5)
                for _ in range(80):
                    self.verify_ca(target)
                for future in futures:
                    future.result(timeout=5)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(folder).iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
