"""Compatibility preflight entry point points to the integrated SeBS backend."""
import unittest
from evaluation.sql.postgres_aws import preflight
from sebs.storage.aws_postgresql import preflight as native_preflight


class CompatibilityTests(unittest.TestCase):
    def test_no_evaluation_dependency_in_backend(self):
        self.assertIs(preflight, native_preflight)


if __name__ == "__main__":
    unittest.main()
