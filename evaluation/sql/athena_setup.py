"""Create Athena tables over the benchmark's Parquet files in S3, and a workgroup."""
import argparse
import json
import re
import tempfile
import time
from pathlib import Path

import boto3
import duckdb

from evaluation.sql.service import manifest_fingerprint

TYPES = {"BIGINT": "bigint", "INTEGER": "int", "DATE": "date", "VARCHAR": "string",
         "DOUBLE": "double"}
# safety limit, SF1 queries scan < 200 MB
SCANNED_BYTES_LIMIT = 10 * 2**30
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
LOCATION = re.compile(r"s3://[a-z0-9.-]+/[A-Za-z0-9._=/-]+/")


def table_location(bucket, keys):
    # Athena reads everything under the prefix
    prefixes = {key.rsplit("/", 1)[0] for key in keys}
    if len(prefixes) != 1:
        raise ValueError(f"Table files are not in a single folder: {sorted(prefixes)}")
    return f"s3://{bucket}/{prefixes.pop()}/"


def athena_type(parquet_type):
    match = re.fullmatch(r"DECIMAL\((\d+),(\d+)\)", parquet_type)
    if match:
        return f"decimal({match.group(1)},{match.group(2)})"
    if parquet_type not in TYPES:
        raise ValueError(f"Unsupported Parquet column type: {parquet_type}")
    return TYPES[parquet_type]


def parquet_columns(s3, bucket, key):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sample.parquet"
        s3.download_file(bucket, key, str(path))
        rows = duckdb.connect().execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
    return [(name, athena_type(column_type)) for name, column_type, *_ in rows]


def create_table_sql(database, table, columns, location):
    for name in [database, table, *(column for column, _ in columns)]:
        if not IDENTIFIER.fullmatch(name):
            raise ValueError(f"Invalid identifier: {name}")
    if not LOCATION.fullmatch(location):
        raise ValueError(f"Invalid table location: {location}")
    definitions = ",\n  ".join(f"`{name}` {column_type}" for name, column_type in columns)
    return (f"CREATE EXTERNAL TABLE `{database}`.`{table}` (\n  {definitions}\n)\n"
            f"STORED AS PARQUET\nLOCATION '{location}'")


def run_statement(athena, sql, workgroup):
    query_id = athena.start_query_execution(QueryString=sql, WorkGroup=workgroup)["QueryExecutionId"]
    while True:
        status = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]["Status"]
        if status["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.5)
    if status["State"] != "SUCCEEDED":
        raise RuntimeError(f"Athena statement failed: {status.get('StateChangeReason')}")


def ensure_workgroup(athena, name, output_location):
    configuration = {
        "ResultConfiguration": {"OutputLocation": output_location},
        "EnforceWorkGroupConfiguration": True,
        "PublishCloudWatchMetricsEnabled": False,
        "BytesScannedCutoffPerQuery": SCANNED_BYTES_LIMIT,
    }
    try:
        athena.get_work_group(WorkGroup=name)
    except athena.exceptions.InvalidRequestException:
        athena.create_work_group(Name=name, Configuration=configuration,
                                 Description="SeBS SQL benchmark comparison")
    else:
        athena.update_work_group(WorkGroup=name, State="ENABLED", ConfigurationUpdates={
            **{key: value for key, value in configuration.items() if key != "ResultConfiguration"},
            "ResultConfigurationUpdates": {"OutputLocation": output_location},
        })
    return athena.get_work_group(WorkGroup=name)["WorkGroup"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--trace", required=True, type=Path,
                        help="trace.json of a Lambda sql-workload run, which records the input files")
    parser.add_argument("--region", required=True)
    parser.add_argument("--workgroup", default="sebs-sql-comparison")
    parser.add_argument("--state", type=Path, default=Path("results/sql-athena/athena.json"))
    args = parser.parse_args()

    trace = json.loads(args.trace.read_text())
    input_config = trace["input"]
    bucket = input_config["bucket"]["bucket"]
    database = re.sub(r"[^a-z0-9_]", "_", f"sebs_sql_{bucket}")
    # in the benchmark bucket so the SeBS cleanup removes it
    output_location = f"s3://{bucket}/{input_config['bucket']['output']}/athena-results/"

    athena = boto3.client("athena", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)
    workgroup = ensure_workgroup(athena, args.workgroup, output_location)
    run_statement(athena, f"CREATE DATABASE IF NOT EXISTS `{database}`", args.workgroup)

    tables = {}
    for table, keys in input_config["tables"].items():
        location = table_location(bucket, keys)
        columns = parquet_columns(s3, bucket, keys[0])
        run_statement(athena, f"DROP TABLE IF EXISTS `{database}`.`{table}`", args.workgroup)
        run_statement(athena, create_table_sql(database, table, columns, location), args.workgroup)
        tables[table] = {"location": location, "files": len(keys), "columns": columns}
        print(f"{table}: {len(keys)} files, {len(columns)} columns", flush=True)

    state = {
        "region": args.region,
        "workgroup": args.workgroup,
        "database": database,
        "output_location": output_location,
        "engine_version": workgroup["Configuration"]["EngineVersion"]["EffectiveEngineVersion"],
        "scanned_bytes_limit": SCANNED_BYTES_LIMIT,
        "input_fingerprint": manifest_fingerprint(input_config),
        "scale_factor": input_config["scale_factor"],
        "tables": tables,
    }
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, indent=2) + "\n")
    print(f"Saved {args.state}", flush=True)


if __name__ == "__main__":
    main()
