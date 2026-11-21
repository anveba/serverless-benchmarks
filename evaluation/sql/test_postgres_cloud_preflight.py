"""Offline checks for code identity, private network checks and quota estimates."""
import base64
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from zipfile import ZipFile

from evaluation.sql.postgres_cloud_preflight import SOURCES, standard_vcpu_use, verify_lambda, verify_package


class CloudPreflightTests(unittest.TestCase):
    def test_package_identity_checks_all_shared_benchmark_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "package.zip"
            with ZipFile(archive, "w") as package:
                for member, source in SOURCES.items():
                    path = root / source
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(member)
                    package.writestr(member, path.read_bytes())
            digest = base64.b64encode(hashlib.sha256(archive.read_bytes()).digest()).decode()
            self.assertEqual(verify_package(archive, digest, root), digest)
            with self.assertRaisesRegex(ValueError, "Cached package differs"):
                verify_package(archive, "wrong", root)
            (root / SOURCES["function/sql.py"]).write_text("changed")
            with self.assertRaisesRegex(ValueError, "source differs"):
                verify_package(archive, digest, root)

    def fixtures(self):
        config = SimpleNamespace(region="region", subnet_id="subnet")
        outputs = dict(LambdaGroupId="sg", LambdaRoleArn="role", SecretArn="secret")
        health = dict(Runtime="python3.12", Architectures=["x86_64"], MemorySize=512,
                      State="Active", LastUpdateStatus="Successful", Role="role",
                      VpcConfig=dict(SubnetIds=["subnet"], SecurityGroupIds=["sg"]),
                      Environment={"Variables": {"SEBS_SQL_CONFIG":
                          '{"type":"postgresql","region":"region","secret_arn":"secret"}'}})
        return health, outputs, config, {"settings": {"memory": 512}}

    def test_matching_lambda_verified_without_invocation(self):
        verify_lambda(*self.fixtures())

    def test_mismatching_memory_or_storage_rejected(self):
        values = self.fixtures()
        values[0]["MemorySize"] = 1024
        with self.assertRaisesRegex(ValueError, "memory differs"):
            verify_lambda(*values)
        values = self.fixtures()
        values[0]["Environment"]["Variables"]["SEBS_SQL_CONFIG"] = "{}"
        with self.assertRaisesRegex(ValueError, "SQL storage differs"):
            verify_lambda(*values)

    def test_quota_estimate_excludes_spot_and_nonstandard_families(self):
        types = {"m7i-flex.large": {"VCpuInfo": {"DefaultVCpus": 2}},
                 "p5.48xlarge": {"VCpuInfo": {"DefaultVCpus": 192}}}
        instances = [{"InstanceType": "m7i-flex.large"},
                     {"InstanceType": "m7i-flex.large", "InstanceLifecycle": "spot"},
                     {"InstanceType": "p5.48xlarge"}]
        self.assertEqual(standard_vcpu_use(instances, types), 2)


if __name__ == "__main__":
    unittest.main()
