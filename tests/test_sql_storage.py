"""SQL lifecycle/configuration/CLI checks without Docker or AWS operations."""
import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from click.testing import CliRunner

from sebs import SeBS
from sebs.cli import cli
from sebs.faas.config import Config
from sebs.storage.postgresql import build_dataset, PostgreSQL, protected_json
from sebs.storage.sql_config import PostgreSQLConfig
from sebs.storage.aws_postgresql import AWSPostgreSQL, template
from sebs.storage.aws_postgresql_bootstrap import user_data
from sebs.utils import LoggingHandlers


class StorageConfigurationTests(unittest.TestCase):
    def test_plain_configuration_contains_no_credentials(self):
        config = PostgreSQLConfig.deserialize({"state_dir": "results/fixture"})
        self.assertNotIn("password", json.dumps(config.serialize()))
        self.assertEqual(
            SeBS.get_sql_config_implementation("postgresql"), PostgreSQLConfig
        )
        self.assertEqual(SeBS.get_sql_implementation("postgresql"), PostgreSQL)

    def test_configuration_attaches_through_standard_factory_and_cache(self):
        section = {"type": "postgresql", "postgresql": {"state_dir": "results/fixture"}}
        cache = Mock()
        cache.get_config.return_value = {}
        config = Config.deserialize(
            {"name": "local", "local": {"storage": {"sql": section}}},
            cache,
            LoggingHandlers(),
        )
        self.assertEqual(config.resources.sql_storage_config.provider, "docker")
        serialized = config.resources.serialize()
        self.assertIn("sql", serialized)
        config.resources.update_cache(cache)
        self.assertTrue(
            any(
                call.kwargs.get("keys") == ["local", "resources", "storage", "sql"]
                for call in cache.update_config.call_args_list
            )
        )
        cache.get_config.return_value = {"resources": {"storage": {"sql": section}}}
        restored = Config.deserialize(
            {"name": "local", "local": {}}, cache, LoggingHandlers()
        )
        self.assertEqual(
            restored.resources.sql_storage_config.state_dir,
            config.resources.sql_storage_config.state_dir,
        )

    def test_invalid_settings_fail_before_allocation(self):
        for data in [
            {"provider": "other"},
            {"password": "must-not-be-configured-here"},
            {"mapped_port": 0},
            {"image": "postgres:16.16'; malicious"},
            {"provider": "aws"},
        ]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                PostgreSQLConfig.deserialize(data)

    def test_namespaces_separate_sizes_and_fingerprint_uses_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / "a", Path(directory) / "b"
            a.write_bytes(b"parquet-fixture")
            b.write_bytes(a.read_bytes())
            first = build_dataset(
                "a" * 12,
                "720.sql-postgresql",
                "test",
                {"lineitem": {"files": [a]}},
                {"scale_factor": 0.01},
            )
            same = build_dataset(
                "a" * 12,
                "720.sql-postgresql",
                "test",
                {"lineitem": {"files": [b]}},
                {"scale_factor": 0.01},
            )
            second = build_dataset(
                "a" * 12,
                "720.sql-postgresql",
                "small",
                {"lineitem": {"files": [a]}},
                {"scale_factor": 1},
            )
            self.assertEqual(first["fingerprint"], same["fingerprint"])
            self.assertNotEqual(first["namespace"], second["namespace"])
            a.write_bytes(b"changed")
            changed = build_dataset(
                "a" * 12,
                "720.sql-postgresql",
                "test",
                {"lineitem": {"files": [a]}},
                {"scale_factor": 0.01},
            )
            self.assertNotEqual(first["fingerprint"], changed["fingerprint"])

    def test_private_files_are_mode_600(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "private.json"
            protected_json(p, {"password": "fixture"})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)


class StorageCliTests(unittest.TestCase):
    def test_storage_start_sql_emits_attachment_through_standard_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            output = root / "storage.json"
            source.write_text(
                json.dumps(
                    {
                        "sql": {
                            "type": "postgresql",
                            "postgresql": {"state_dir": str(root / "state")},
                        }
                    }
                )
            )
            instance = Mock()
            instance.serialize.return_value = PostgreSQLConfig(
                state_dir=str(root / "state")
            ).serialize()
            implementation = Mock()
            implementation.deserialize.return_value = instance
            with patch.object(
                SeBS, "get_sql_implementation", return_value=implementation
            ), patch("docker.from_env") as docker:
                result = CliRunner().invoke(
                    cli,
                    [
                        "storage",
                        "start",
                        "sql",
                        str(source),
                        "--output-json",
                        str(output),
                    ],
                )
            self.assertEqual(result.exit_code, 0, result.output)
            instance.start.assert_called_once()
            docker.assert_not_called()
            self.assertIn("sql", json.loads(output.read_text()))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_status_stop_and_remove_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sql.json"
            path.write_text(
                json.dumps({"sql": {"type": "postgresql", "postgresql": {}}})
            )
            instance = Mock()
            instance.status.return_value = {"state": "running"}
            instance.stop.return_value = {"state": "stopped"}
            instance.remove.return_value = {"state": "removed"}
            implementation = Mock()
            implementation.deserialize.return_value = instance
            with patch.object(
                SeBS, "get_sql_implementation", return_value=implementation
            ):
                for args in [
                    ["status", "sql", str(path)],
                    ["stop", "sql", str(path)],
                    ["remove", "sql", str(path), "--dry-run"],
                ]:
                    result = CliRunner().invoke(cli, ["storage", *args])
                    self.assertEqual(result.exit_code, 0, result.output)
            instance.status.assert_called_once()
            instance.stop.assert_called_once()
            instance.remove.assert_called_once_with(False, True)


class LocalCleanupTests(unittest.TestCase):
    def test_function_cleanup_honors_docker_auto_removal(self):
        from sebs.local.local import Local

        for auto in [True, False]:
            with self.subTest(auto_remove=auto):
                deployment = Local.__new__(Local)
                deployment._docker_client = Mock()
                deployment._config = SimpleNamespace(
                    resources=SimpleNamespace(allocated_ports={9000})
                )
                container = deployment._docker_client.containers.get.return_value
                container.attrs = {"HostConfig": {"AutoRemove": auto}}
                deployment.delete_function(
                    "owned", {"instance_id": "owned-id", "port": 9000}
                )
                container.stop.assert_called_once_with(timeout=0)
                if auto:
                    container.remove.assert_not_called()
                else:
                    container.remove.assert_called_once_with(force=True)
                self.assertEqual(deployment.config.resources.allocated_ports, set())


class AWSStorageTests(unittest.TestCase):
    def config(self):
        return PostgreSQLConfig(
            provider="aws", subnet_id="subnet-fixture", ami_id="ami-fixture"
        )

    def test_template_is_workload_independent_and_private(self):
        document = template(self.config())
        resources = document["Resources"]
        self.assertIn("StagingBucket", resources)
        self.assertNotIn("710.sql-query", json.dumps(document))
        self.assertNotIn("720.sql-postgresql", json.dumps(document))
        rule = resources["DatabaseGroup"]["Properties"]["SecurityGroupIngress"][0]
        self.assertEqual(rule["FromPort"], 5432)
        self.assertNotIn("CidrIp", rule)
        self.assertEqual(rule["SourceSecurityGroupId"], {"Ref": "FunctionGroup"})
        self.assertTrue(resources["SecretsEndpoint"]["Properties"]["PrivateDnsEnabled"])
        permissions = resources["DatabaseRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        puts = [item for item in permissions if "s3:PutObject" in item["Action"]]
        self.assertEqual(len(puts), 1)
        self.assertIn("/reports/*", json.dumps(puts[0]["Resource"]))

        for configured in [self.config(), PostgreSQLConfig(cpus=1, memory_mb=2048)]:
            script = user_data(configured.image, configured.cpus, configured.memory_mb)
            python = script.split("<<'BOOTSTRAP'\n", 1)[1].rsplit("\nBOOTSTRAP", 1)[0]
            ast.parse(python)
            self.assertLess(len(script.encode()), 16384)
            self.assertIn("sslmode='verify-full'", python)
            self.assertNotIn("set -x", script)

    def test_unknown_ssm_submission_is_never_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = AWSPostgreSQL.__new__(AWSPostgreSQL)
            storage.folder = Path(directory)
            storage.ssm = Mock()
            storage.info = Mock(return_value=({}, {}, {"InstanceId": "i-fixture"}))
            protected_json(
                storage.folder / "commands" / "load.json",
                {"instance_id": "i-fixture", "command_id": None},
            )
            with self.assertRaisesRegex(RuntimeError, "unknown"):
                storage.command("load", "script")
            storage.ssm.send_command.assert_not_called()

    def test_aws_remove_requires_explicit_data_deletion_and_honors_dry_run(self):
        storage = AWSPostgreSQL.__new__(AWSPostgreSQL)
        storage.info = Mock(return_value=({}, {"StackId": "owned"}, {}))
        self.assertEqual(storage.remove(dry_run=True)["stack"], "owned")
        with self.assertRaises(ValueError):
            storage.remove()
        storage.info.assert_called_once()

    def test_aws_remove_refuses_attached_functions(self):
        storage = AWSPostgreSQL.__new__(AWSPostgreSQL)
        storage.config = self.config()
        storage.info = Mock(
            return_value=({}, {"StackId": "owned"}, {"LambdaRoleArn": "owned-role"})
        )
        client = Mock()
        client.get_paginator.return_value.paginate.return_value = [
            {"Functions": [{"Role": "owned-role", "FunctionName": "owned-function"}]}
        ]
        storage.session = SimpleNamespace(client=Mock(return_value=client))
        with self.assertRaisesRegex(RuntimeError, "owned-function"):
            storage.remove(delete_data=True)
        client.delete_stack.assert_not_called()


if __name__ == "__main__":
    unittest.main()
