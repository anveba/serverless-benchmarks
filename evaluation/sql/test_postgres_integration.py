"""Opt-in local integration of the actual SeBS storage, input and runtime paths.

SEBS_TEST_POSTGRES_DOCKER=1 enables disposable local Docker resources. AWS is
never contacted. All created containers and the owned volume are removed.
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(
    os.environ.get("SEBS_TEST_POSTGRES_DOCKER") == "1",
    "Opt-in local Docker integration",
)
class NativePostgresIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import docker
        from sebs.storage.postgresql import PostgreSQL
        from sebs.storage.sql_config import PostgreSQLConfig

        (ROOT / "results").mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="sql-native-check-", dir=ROOT / "results"
        )
        cls.folder = Path(cls.temporary.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = PostgreSQLConfig(
            state_dir=str(cls.folder / "storage"),
            mapped_port=port,
            cpus=1,
            memory_mb=1024,
            startup_timeout=90,
        )
        cls.storage = PostgreSQL(docker.from_env(), None, config)
        try:
            cls.storage.start()
            cls.input = load_module(
                "native_pg_integration_input",
                ROOT / "benchmarks/700.analytics/720.sql-postgresql/input.py",
            )
            cls.event = cls.input.generate_input(
                None, "test", None, [], [], None, None, sql_storage=cls.storage
            )
            package = types.ModuleType("native_pg_live")
            package.__path__ = [
                str(ROOT / "benchmarks/700.analytics/720.sql-postgresql/python")
            ]
            sys.modules[package.__name__] = package
            cls.wrapper = load_module(
                package.__name__ + ".sql",
                ROOT / "benchmarks/wrappers/common/python/sql.py",
            )
            sys.modules[package.__name__ + ".sql"] = cls.wrapper
            cls.function = load_module(
                package.__name__ + ".function",
                Path(package.__path__[0]) / "function.py",
            )
            # Publish the same runtime configuration used by a local SeBS function.
            cls.env = patch.dict(os.environ, cls.storage.envs(external=False))
            cls.env.start()
        except BaseException:
            if cls.storage.config.instance_id:
                cls.storage.remove(delete_data=True)
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.function.client.close()
        cls.env.stop()
        cls.storage.remove(delete_data=True)
        cls.temporary.cleanup()

    def tearDown(self):
        self.function.client.close()

    def test_five_queries_validate_with_original_tpch_answers(self):
        for query in self.function.QUERIES:
            with self.subTest(query=query):
                event = {**self.event, "queries": [query]}
                result = self.function.handler(event)
                self.assertIsNone(
                    self.input.validate_output(None, event, result, "python")
                )

    def test_reused_connection_stays_open_in_one_runtime(self):
        event = {**self.event, "connection_policy": "reuse"}
        first = self.function.handler(event)
        connection = self.function.client._connection
        second = self.function.handler(event)
        self.assertFalse(first["connection_reused"])
        self.assertTrue(second["connection_reused"])
        self.assertIs(self.function.client._connection, connection)

    def test_reader_cannot_modify_data_and_mismatch_is_rejected(self):
        import psycopg

        settings, _ = self.function.client.credentials()
        with psycopg.connect(**settings, autocommit=True) as con:
            with self.assertRaises(psycopg.Error):
                con.execute("DELETE FROM " + self.event["namespace"] + ".lineitem")
        with self.assertRaises(RuntimeError):
            self.function.handler({**self.event, "input_fingerprint": "f" * 64})

    def test_preparation_reuses_dataset_and_sizes_do_not_overwrite(self):
        again = self.input.generate_input(
            None, "test", None, [], [], None, None, sql_storage=self.storage
        )
        self.assertEqual(again, self.event)
        ref = self.input.reference()
        name, settings = ref._workload("test")
        tables = {
            table: {"files": files, "primary_key": self.input.PRIMARY_KEYS[table]}
            for table, files in ref._parquet_files(name, settings).items()
        }
        other = self.storage.prepare_dataset(
            "720.sql-postgresql", "other-fixture", tables, {"scale_factor": 0.01}
        )
        self.assertNotEqual(other["namespace"], self.event["namespace"])
        self.assertIsNone(
            self.input.validate_output(
                None, self.event, self.function.handler(self.event), "python"
            )
        )

    def test_stop_start_retains_data_and_credentials(self):
        before = self.storage.config.instance_id
        self.storage.stop()
        self.storage.start()
        self.assertEqual(self.storage.config.instance_id, before)
        self.assertIsNone(
            self.input.validate_output(
                None, self.event, self.function.handler(self.event), "python"
            )
        )

    def test_storage_cli_manages_a_real_persistent_database(self):
        from click.testing import CliRunner
        from sebs.cli import cli
        from sebs.storage.postgresql import PostgreSQL
        from sebs.storage.sql_config import PostgreSQLConfig

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        source = self.folder / "cli-config.json"
        output = self.folder / "cli-storage.json"
        config = {
            "provider": "docker",
            "state_dir": str(self.folder / "cli-state"),
            "mapped_port": port,
            "cpus": 1,
            "memory_mb": 1024,
        }
        source.write_text(
            json.dumps({"sql": {"type": "postgresql", "postgresql": config}})
        )
        try:
            for args in [
                ["start", "sql", str(source), "--output-json", str(output)],
                ["status", "sql", str(output)],
                ["stop", "sql", str(output)],
                ["start", "sql", str(output), "--output-json", str(output)],
                ["remove", "sql", str(output), "--delete-data", "--dry-run"],
            ]:
                result = CliRunner().invoke(cli, ["storage", *args])
                self.assertEqual(result.exit_code, 0, result.output)
        finally:
            if output.exists():
                attached = json.loads(output.read_text())["sql"]["postgresql"]
                backend = PostgreSQL.deserialize(PostgreSQLConfig.deserialize(attached))
                backend.remove(delete_data=True)

    def test_real_sebs_function_build_deployment_and_invocation(self):
        """Exercise actual SeBS packaging and its local HTTP function wrapper."""
        from sebs.sebs import SeBS
        from sebs.faas.function import Trigger

        base = json.loads(
            (ROOT / "configs/sql-postgresql-local.example.json").read_text()
        )
        base["deployment"]["local"]["storage"] = {
            "sql": {"type": "postgresql", "postgresql": self.storage.serialize()}
        }
        # No patching of storage, loading, packaging, deployment or execution.
        with SeBS(
            str(self.folder / "cache"),
            str(self.folder / "function-build"),
            verbose=True,
        ) as sebs:
            deployment = sebs.get_deployment(base)
            try:
                experiment = sebs.get_experiment_config(base["experiments"])
                benchmark = sebs.get_benchmark(
                    "720.sql-postgresql", deployment, experiment
                )
                event = benchmark.prepare_input(deployment.system_resources, "test")
                function = deployment.get_function(benchmark)
                triggers = function.triggers(Trigger.TriggerType.HTTP)
                trigger = (
                    triggers[0]
                    if triggers
                    else deployment.create_trigger(function, Trigger.TriggerType.HTTP)
                )
                result = trigger.sync_invoke(event)
                self.assertFalse(result.stats.failure)
                self.assertIsNone(
                    benchmark.validate_output(event, result.output["result"])
                )
                warm = trigger.sync_invoke(event)
                self.assertFalse(warm.stats.failure)
                handler_us = warm.output['result']['measurement']['q6']['handler_time']
                self.assertGreaterEqual(warm.times.benchmark,handler_us-100)
                cached = deployment.get_function(benchmark)
                self.assertEqual(cached.container.id, function.container.id)
            finally:
                deployment.cleanup_functions(dry_run=False)
                deployment.shutdown()


if __name__ == "__main__":
    unittest.main()
