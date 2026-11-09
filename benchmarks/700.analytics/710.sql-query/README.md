# 710.sql-query

Runs TPC-H queries with [DuckDB](https://duckdb.org) over Parquet files in object storage.
The function runs Q1 (scan and group-by), Q3 (three-table join), Q6 (scan and filter),
Q12 (two-table join), and Q21 (memory-heavy, with correlated subqueries over `lineitem`) by default.
Set `queries` in the input to select a subset.

## Input sizes

Data is generated with the DuckDB `tpch` extension on the machine running SeBS, and cached
in the system's temporary directory.

| Size    | Scale factor | Target file size |
|---------|--------------|------------------|
| `test`  | 0.01         | 512 KB           |
| `small` | 1            | 16 MB            |
| `large` | 10           | 64 MB            |

To use another scale factor, set `SEBS_SQL_SCALE_FACTOR` when deploying. It applies the file size
of `large`, and overrides the selected input size.

Results are validated against the official TPC-H answers shipped with DuckDB at scale factors
0.01 and 1. Other scale factors have no official answers, so they are compared against a query
executed on the machine running SeBS.

## Data access

`access_mode` in the input selects how the function reads data:

* `httpfs` (default) reads ranges of the files directly from storage, so column pruning reduces
  the data transferred. It is implemented for S3, S3-compatible storage, and Google Cloud Storage
  with an HMAC key (see [the GCP guide](../../../docs/sql-gcp-setup.md)).
* `download` fetches whole files to local disk with the SeBS storage wrapper, and works on all
  platforms. Lambda's default `/tmp` holds only 512 MB, and SeBS does not change it.

Each query uses a new DuckDB connection with the external file cache disabled. DuckDB caches remote
data per instance, so a connection kept across warm invocations would measure queries that never
read from storage. Warm-instance caching is a behavior that could be studied on its own.

## Measurements

For each query, `measurement.queries` reports times in microseconds (`connect_time`, `setup_time`,
`query_time`), and the `bytes_read`, `get_requests` and `head_requests` reported by DuckDB.
`setup_time` includes downloads in `download` mode.

## Parallel execution

`scripts/sql_parallel.py` coordinates a Starling-style execution over several function instances,
where workers exchange intermediate data through object storage:

* Q1 and Q6 aggregate groups of files, and then merge the partial aggregates.
* Q12 hash-partitions both tables on the join key, joins each partition, and then merges the results.

```bash
sebs local start 710.sql-query small deployment.json --config configs/example.json \
  --storage-configuration storage.json --deployments 4 --memory 1769 --architecture=arm64
python scripts/sql_parallel.py deployment.json --query q12 --baseline
```

The coordinator currently runs on the client and invokes local deployments.
Intermediate objects are not removed after the query.

## Request validation

Function URLs can be public, and the function puts values from the request into SQL statements and
storage requests. It therefore rejects requests with tables outside of a fixed list, unexpected
characters in object keys and prefixes, buckets not named `sebs-benchmarks-*`, or invalid partition
settings. This limits what a request can do, but it is not access control: anyone with the URL can
still read and write benchmark buckets and spend money. Remove public URLs after running experiments.

## Local resource limits

`sebs local start --memory` limits containers to the given memory without swap, with CPU
proportional to memory as on AWS Lambda (1 vCPU per 1769 MB, up to 6). The function configures
DuckDB from these limits. This approximates when a query runs out of memory, but local times do not
predict cloud performance: storage is on the same machine and has no network latency.

## AWS validation

Single-function execution was validated on AWS with Python 3.12/x64 at SF0.01 and SF1.
SF10 execution and controlled cold/warm measurements at SF1/SF10 also completed.
Each invocation runs all five queries; independent query-arrival workloads are not implemented.
Use `configs/sql-aws.example.json` as a template and follow [the AWS setup guide](../../../docs/sql-aws-setup.md).
The storage cache now tracks uploaded input sizes, so changing size no longer needs `--update-storage`.
This fix was tested with a stand-in for the provider, and has not run against S3 yet.
Parallel AWS execution and the EC2/Athena baselines are not implemented.

## One query per invocation

Copy `configs/sql-aws-single-query.example.json` to a local config and set the
execution role if required. Query selection is in the SeBS experiment config:

```json
"benchmark-inputs": {
  "710.sql-query": {"queries": ["q6"]}
}
```

Place this object inside `experiments`. Supported IDs: `q1`, `q3`, `q6`, `q12`,
`q21`. One ID means one complete query per invocation. Multiple IDs retain the
sequential bundle behavior; omitting the setting runs all five as before.
Empty lists, duplicates, unsupported IDs, and unknown parameters are rejected
before SQL dataset generation/upload. Selection is saved in experiment metadata.

```bash
cp configs/sql-aws-single-query.example.json configs/sql-aws-single-query.json
sebs benchmark invoke 710.sql-query small \
  --config configs/sql-aws-single-query.json --deployment aws \
  --memory 2048 --trigger library --repetitions 1 --validate \
  --cache cache-sql-aws --output-dir results/sql-aws-q6-check --verbose
```

This uses the same SF1 files. Query selection alone does not require reuploading;
add `--update-storage` if this size has not been uploaded or when changing sizes.
Change `queries` to another single ID and use a fresh output directory to validate
each query individually. `--validate` checks only the selected answers. After
validation, `sebs experiment invoke perf-cost` with this config measures the same
selected query; the example collects five cold and five warm samples at 2048 MB.
`perf-cost` does not independently validate answers.

This enables isolated-query measurements. For mixed arrivals, use the
[SQL workload experiment](../../../docs/sql-aws-workload.md). EC2/Athena adapters
are not implemented yet. Increasing `concurrent-invocations`
submits independent executions of the selected query; it does not split a query.

The AWS mixed-query smoke test completed ten individual requests with validated
answers and provider metrics for all ten. The example's two-slot client limit
allows overlapping invocations of the same deployed function; it does not split
queries or limit the function to two total requests. Measurements and limitations
are recorded in the [AWS handoff](../../../handoff/AWS_HANDOFF.md#individual-query-lambda-milestone).
