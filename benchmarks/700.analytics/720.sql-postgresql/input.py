# Copyright 2020-2026 ETH Zurich and the SeBS authors. All rights reserved.
"""TPC-H input preparation; SeBS SQL storage owns loading and connection settings."""
import importlib.util
from pathlib import Path

PRIMARY_KEYS = {
    "customer": ["c_custkey"],
    "orders": ["o_orderkey"],
    "lineitem": ["l_orderkey", "l_linenumber"],
    "supplier": ["s_suppkey"],
    "nation": ["n_nationkey"],
}


def reference():
    path = Path(__file__).resolve().parent.parent / "710.sql-query" / "input.py"
    spec = importlib.util.spec_from_file_location("sebs_postgres_reference", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def allocate_sql():
    """Declare SQL storage usage, analogous to allocate_nosql in 130.crud-api."""
    return PRIMARY_KEYS


def generate_input(
    data_dir,
    size,
    bucket,
    input_paths,
    output_paths,
    upload,
    nosql,
    parameters=None,
    sql_storage=None,
    replace_existing=False,
):
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict) or set(parameters) - {
        "queries",
        "connection_policy",
        "statement_timeout_ms",
    }:
        raise ValueError(
            "Supported PostgreSQL parameters are queries, connection_policy and statement_timeout_ms"
        )
    selected = parameters.get("queries", ["q6"])
    ref = reference()
    if (
        not isinstance(selected, list)
        or len(selected) != 1
        or not isinstance(selected[0], str)
        or selected[0] not in ref.QUERIES
    ):
        raise ValueError("Select exactly one supported query per invocation")
    policy = parameters.get("connection_policy", "fresh")
    if policy not in ("fresh", "reuse"):
        raise ValueError("connection_policy must be fresh or reuse")
    timeout = parameters.get("statement_timeout_ms", 120000)
    if type(timeout) is not int or not 1 <= timeout <= 240000:
        raise ValueError("statement_timeout_ms must be between 1 and 240000")
    if sql_storage is None:
        raise RuntimeError("SQL storage is not attached; use --storage-configuration")
    name, settings = ref._workload(size)
    files = ref._parquet_files(name, settings)
    tables = {
        table: {"files": paths, "primary_key": PRIMARY_KEYS[table]}
        for table, paths in files.items()
    }
    prepared = sql_storage.prepare_dataset(
        "720.sql-postgresql",
        name,
        tables,
        {
            "scale_factor": settings["scale_factor"],
            "input_size": size,
            "workload": "TPC-H",
        },
        replace=replace_existing,
    )
    return {
        **prepared,
        "queries": selected,
        "scale_factor": settings["scale_factor"],
        "connection_policy": policy,
        "statement_timeout_ms": timeout,
    }


def validate_output(data_dir, input_config, output, language, storage=None):
    return reference().validate_output(
        data_dir, input_config, output, language, storage
    )
