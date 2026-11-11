# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
"""Offline coverage of query selection through SeBS input preparation."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sebs.benchmark import Benchmark
from sebs.experiments.config import Config

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "benchmarks/700.analytics/710.sql-query/input.py"
spec = importlib.util.spec_from_file_location("sql_input_test", INPUT)
sql_input = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sql_input)


def configuration(parameters=None):
    data = json.loads((ROOT / "configs/sql-aws.example.json").read_text())["experiments"]
    if parameters is not None:
        data["benchmark-inputs"] = {"710.sql-query": parameters}
    return Config.deserialize(data)


class QuerySelectionTests(unittest.TestCase):
    def generate(self, parameters=None):
        upload = Mock()
        with patch.object(sql_input, "_parquet_files", return_value={
            "lineitem": [Path("/fixture/data_0.parquet")]
        }):
            result = sql_input.generate_input(
                None, "small", "bucket", ["input"], ["output"], upload, None,
                parameters=parameters,
            )
        return result, upload

    def test_default_preserves_all_queries(self):
        result, _ = self.generate()
        self.assertEqual(result["queries"], ["q1", "q3", "q6", "q12", "q21"])

    def test_one_query_keeps_dataset_and_selects_only_requested_answer(self):
        result, upload = self.generate({"queries": ["q6"]})
        self.assertEqual(result["queries"], ["q6"])
        self.assertEqual(result["scale_factor"], 1)
        self.assertEqual(result["tables"]["lineitem"], ["input/small/lineitem/data_0.parquet"])
        upload.assert_called_once_with(0, "small/lineitem/data_0.parquet", "/fixture/data_0.parquet")
        with patch.object(sql_input, "_expected_rows", return_value=[["42"]]) as expected:
            output = {"result": {"queries": {"q6": [[42]]}}}
            self.assertIsNone(sql_input.validate_output(None, result, output, "python"))
            expected.assert_called_once_with(1, "q6")
            self.assertIn("Missing", sql_input.validate_output(None, result, {}, "python"))

    def test_azure_access_is_explicit_and_preserved_in_payload(self):
        result, _ = self.generate({"access_mode": "azure", "queries": ["q6"]})
        self.assertEqual(result["access_mode"], "azure")
        for value in ["typo", None, []]:
            with self.subTest(value=value), patch.object(sql_input, "_parquet_files") as generate:
                with self.assertRaisesRegex(ValueError, "access_mode"):
                    self.generate({"access_mode": value})
                generate.assert_not_called()

    def test_each_supported_query_can_be_selected(self):
        for query in sql_input.QUERIES:
            with self.subTest(query=query):
                result, _ = self.generate({"queries": [query]})
                self.assertEqual(result["queries"], [query])

    def test_invalid_selection_fails_before_data_work(self):
        for parameters in [
            {"queries": []}, {"queries": "q6"}, {"queries": ["q2"]},
            {"queries": ["q6", "q6"]}, {"queries": [None]},
            {"query": "q6"}, {"queries": ["q6"], "typo": True},
        ]:
            with self.subTest(parameters=parameters):
                with patch.object(sql_input, "_parquet_files") as generate:
                    with self.assertRaises(ValueError):
                        sql_input.generate_input(
                            None, "small", "bucket", ["input"], ["output"], Mock(), None,
                            parameters=parameters,
                        )
                    generate.assert_not_called()

    def test_config_records_selection_and_returns_copy(self):
        config = configuration({"queries": ["q6"]})
        selected = config.benchmark_input("710.sql-query")
        selected["queries"].append("q1")
        saved = config.serialize()
        self.assertEqual(saved["benchmark-inputs"]["710.sql-query"]["queries"], ["q6"])
        restored = Config.deserialize(saved)
        self.assertEqual(restored.benchmark_input("710.sql-query"), {"queries": ["q6"]})
        self.assertEqual(restored.benchmark_input("unconfigured"), {})
        self.assertNotIn("benchmark-inputs", configuration().serialize())

    def test_invalid_config_parameters_fail(self):
        original = json.loads((ROOT / "configs/sql-aws.example.json").read_text())["experiments"]
        for value in [[], None, {"710.sql-query": []}]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Config.deserialize({**original, "benchmark-inputs": value})

    def test_framework_passes_parameters_and_preserves_legacy_generators(self):
        for configured in [False, True]:
            with self.subTest(configured=configured):
                benchmark = Benchmark.__new__(Benchmark)
                benchmark._benchmark = "710.sql-query"
                benchmark._benchmark_data_path = None
                benchmark._experiment_config = configuration({"queries": ["q6"]} if configured else None)
                benchmark._cache_client = Mock()
                received = []
                if configured:
                    def generator(*args, parameters=None):
                        received.append(parameters)
                        return {"queries": parameters["queries"]}
                else:
                    def generator(a, b, c, d, e, f, g):
                        received.append("legacy")
                        return {"queries": ["q1"]}
                benchmark._benchmark_input_module = SimpleNamespace(generate_input=generator)
                result = benchmark.prepare_input(Mock(), "small")
                self.assertEqual(received, [{"queries": ["q6"]}] if configured else ["legacy"])
                self.assertEqual(result["queries"], ["q6"] if configured else ["q1"])

    def test_unsupported_generator_rejects_config_before_storage(self):
        benchmark = Benchmark.__new__(Benchmark)
        benchmark._benchmark = "710.sql-query"
        benchmark._experiment_config = configuration({"queries": ["q6"]})
        benchmark._benchmark_input_module = SimpleNamespace(generate_input=lambda *args: {})
        resources = Mock()
        with self.assertRaisesRegex(ValueError, "does not support"):
            benchmark.prepare_input(resources, "small")
        resources.get_storage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
