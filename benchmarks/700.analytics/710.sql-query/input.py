# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
import csv
import io
import os
import shutil
import tempfile
from pathlib import Path

QUERIES = {"q1": 1, "q3": 3, "q6": 6, "q12": 12, "q21": 21}
TABLES = ["customer", "orders", "lineitem", "supplier", "nation"]

# Files are split, so that parallel queries can assign them to workers.
SIZES = {
    "test": {"scale_factor": 0.01, "file_size": "512KB", "row_group_size": 10000},
    "small": {"scale_factor": 1, "file_size": "16MB", "row_group_size": 122880},
    "large": {"scale_factor": 10, "file_size": "64MB", "row_group_size": 122880},
}

# Selects a scale factor beyond the fixed input sizes, generated with the file layout of large.
SCALE_FACTOR_OVERRIDE = "SEBS_SQL_SCALE_FACTOR"

# Relative tolerance of comparisons; reference answers have a limited number of decimals.
TOLERANCE = 1e-6
ABSOLUTE_TOLERANCE = 0.01


def buckets_count():
    return (1, 1)


def _cache_dir(scale_factor):
    # Generating and uploading data is expensive, and data for a scale factor never changes.
    path = Path(tempfile.gettempdir()) / "sebs-tpch" / f"sf{scale_factor}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _database(scale_factor):
    import duckdb

    path = _cache_dir(scale_factor)
    marker = path / "database.complete"
    database = path / "tpch.duckdb"
    # Without the marker, this is a leftover of an interrupted generation.
    if not marker.exists():
        database.unlink(missing_ok=True)
    con = duckdb.connect(str(database))
    con.execute("INSTALL tpch")
    con.execute("LOAD tpch")
    if not marker.exists():
        con.execute(f"CALL dbgen(sf={scale_factor})")
        marker.touch()
    return con


def _workload(size):
    """Return the name of the workload, used in paths, and its parameters."""
    if SCALE_FACTOR_OVERRIDE in os.environ:
        scale_factor = float(os.environ[SCALE_FACTOR_OVERRIDE])
        scale_factor = int(scale_factor) if scale_factor.is_integer() else scale_factor
        return f"sf{scale_factor}", {**SIZES["large"], "scale_factor": scale_factor}
    return size, SIZES[size]


def _parquet_files(name, params):
    path = _cache_dir(params["scale_factor"]) / name
    path.mkdir(exist_ok=True)
    con = None
    for table in TABLES:
        marker = path / f"{table}.complete"
        if marker.exists():
            continue
        con = con or _database(params["scale_factor"])
        shutil.rmtree(path / table, ignore_errors=True)
        con.execute(
            f"COPY {table} TO '{path / table}' (FORMAT parquet, "
            f"FILE_SIZE_BYTES '{params['file_size']}', "
            f"ROW_GROUP_SIZE {params['row_group_size']})"
        )
        marker.touch()
    if con is not None:
        con.close()
    return {table: sorted((path / table).iterdir()) for table in TABLES}


"""
    Generate TPC-H data as Parquet files, split to allow for parallel queries.

    :param data_dir: unused, data is generated
    :param size: workload size, selecting the scale factor
    :param benchmarks_bucket: bucket for input and output data
    :param input_paths: input prefixes for this benchmark
    :param output_paths: output prefixes for this benchmark
    :param upload_func: upload function taking three params(bucket_idx, key, filepath)
"""


def _selected_queries(parameters):
    """Validate query selection before generating or uploading any data."""
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise ValueError("SQL input parameters must be an object")
    unknown = set(parameters) - {"queries"}
    if unknown:
        raise ValueError(f"Unknown SQL input parameters: {sorted(unknown)}")
    selected = parameters.get("queries", list(QUERIES))
    if not isinstance(selected, list) or not selected:
        raise ValueError("queries must be a non-empty list of query IDs")
    if any(not isinstance(query, str) or query not in QUERIES for query in selected):
        raise ValueError(f"Supported query IDs: {', '.join(QUERIES)}")
    if len(set(selected)) != len(selected):
        raise ValueError("queries must not contain duplicate IDs")
    return list(selected)


def generate_input(
    data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func,
    parameters=None,
):
    selected_queries = _selected_queries(parameters)
    name, params = _workload(size)
    tables = {}
    for table, files in _parquet_files(name, params).items():
        tables[table] = []
        for file in files:
            key = os.path.join(name, table, file.name)
            upload_func(0, key, str(file))
            tables[table].append(os.path.join(input_paths[0], key))

    return {
        "bucket": {
            "bucket": benchmarks_bucket,
            "input": input_paths[0],
            "output": output_paths[0],
        },
        "scale_factor": params["scale_factor"],
        "access_mode": "httpfs",
        "queries": selected_queries,
        "tables": tables,
    }


def _expected_rows(scale_factor, query):
    con = _database(scale_factor)
    # Official answers are independent of this DuckDB run, but cover only some scale factors.
    answer = con.execute(
        "SELECT answer FROM tpch_answers() WHERE query_nr = ? AND scale_factor = ?",
        [QUERIES[query], scale_factor],
    ).fetchone()
    if answer is not None:
        con.close()
        return list(csv.reader(io.StringIO(answer[0]), delimiter="|"))[1:]

    sql = con.execute("SELECT query FROM tpch_queries() WHERE query_nr = ?", [QUERIES[query]])
    rows = con.execute(sql.fetchone()[0]).fetchall()
    con.close()
    return [[str(value) for value in row] for row in rows]


def _matches(value, expected):
    try:
        number = float(value)
        reference = float(expected)
    except (TypeError, ValueError):
        return str(value) == expected
    return abs(number - reference) <= max(ABSOLUTE_TOLERANCE, TOLERANCE * abs(reference))


def validate_output(data_dir, input_config, output, language, storage=None):
    results = output.get("result", {}).get("queries", {})
    for query in input_config["queries"]:
        if query not in results:
            return f"Missing the result of query {query}"
        rows = results[query]
        expected = _expected_rows(input_config["scale_factor"], query)
        if len(rows) != len(expected):
            return f"Query {query} returned {len(rows)} rows, expected {len(expected)}"
        for idx, (row, expected_row) in enumerate(zip(rows, expected)):
            if len(row) != len(expected_row):
                return (
                    f"Query {query} row {idx} has {len(row)} columns, "
                    f"expected {len(expected_row)}"
                )
            for value, expected_value in zip(row, expected_row):
                if not _matches(value, expected_value):
                    return f"Query {query} row {idx}: got {value}, expected {expected_value}"
    return None
