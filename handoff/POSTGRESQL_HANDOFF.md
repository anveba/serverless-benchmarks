# PostgreSQL benchmark integration handoff — 2026-11-16

## Status

`720.sql-postgresql` is integrated with SeBS storage, input preparation, module
packaging, function deployment, validation, workload replay and cleanup. The
previous standalone AWS evaluation setup is no longer required to run it.

Verified: **173 offline tests and 7 real local Docker integration checks pass**.
The latter include a real SeBS function build, deployment, cold/warm HTTP
invocations, answer validation, cached function reuse and cleanup. Temporary test
containers and database volumes were removed. Local tests use SF0.01; their
latencies are functional test observations, not performance results.

AWS provisioning, data loading, Lambda deployment, private PostgreSQL access and
Q6/SF0.01 answer validation and a ten-request SF0.01 workload have succeeded in
the user's account. All five query types passed twice. These are functional
checks, not a comparative workload study. The user ran the cloud
commands; the agent ran no AWS resource mutations or Git commands.

Full reproduction and cleanup: [SQL PostgreSQL guide](../docs/sql-postgresql-aws.md).
Commit paths: [POSTGRESQL_COMMIT_FILES.txt](POSTGRESQL_COMMIT_FILES.txt).

## Architecture and NoSQL parity

The function selects one approved SQL statement, submits it to PostgreSQL, fetches
rows and returns results. PostgreSQL stays running outside the function. This is
the external-database pattern used by `130.crud-api`, not in-function DuckDB.

| Concern | Integration |
|---|---|
| Storage lifecycle | `sebs storage start/status/stop/remove sql` |
| Storage attachment | Existing `--storage-configuration` option and deployment cache |
| Benchmark declaration | `modules: ["sql"]` and `allocate_sql()` table/key metadata |
| Input preparation | Generate deterministic TPC-H Parquet, load through SQL backend before timing |
| Runtime access | Shared `sql` wrapper; no AWS SDK/IAM/provisioning code in benchmark handler |
| Function lifecycle | Normal SeBS build images, package/function cache, platform deployment and cleanup |
| Validation | Existing TPC-H reference answers, outside timed workload replay |
| Experiments | Existing benchmark invoke, perf-cost and sql-workload paths |

Storage is reusable independently of this benchmark. Docker and AWS EC2 are
provider implementations of the same PostgreSQL storage interface. The generic
loader knows table files/keys and metadata; TPC-H query text, scale mapping and
primary-key declarations stay in the benchmark input/code.

The query set remains Q1/Q3/Q6/Q12/Q21, with the same SQL text as `710.sql-query`.
Each invocation executes one complete query. No query splitting, shuffle,
coordinator or multi-Lambda aggregation is included. PostgreSQL may use ordinary
internal CPU parallelism.

## Important behavior

- Normal input preparation generates and loads data automatically. No local
  manifest, manual loading command, layer installation or hand-edited Lambda
  networking/role is required.
- Each storage/benchmark/input identity has a distinct schema. File-content hashes
  and metadata define a fingerprint; invocations reject a mismatching dataset.
- Loading is transactional and protected by a namespace advisory lock. It preserves
  decimal/date types, verifies hashes/row counts, adds primary keys and runs ANALYZE.
- `--update-storage` permits replacing changed contents of that owned input schema;
  other input sizes remain intact.
- `fresh` opens/closes each database connection; `reuse` keeps one per warm
  execution environment. Neither is a shared connection pool. Failed queries are
  never replayed; AWS SDK invocation retries are disabled.
- Credentials are cached per runtime configuration. AWS secret rotation needs a
  runtime restart/redeployment before measurement. Prepared-statement auto-caching
  is disabled; database and OS caches stay enabled.
- Docker credentials are kept in mode-0600 private state. Public attachment/cache
  configuration contains state references, not passwords. AWS uses Secrets Manager
  and verified TLS to a private PostgreSQL endpoint.
- Docker SQL function containers use the same bridge as the database on WSL and
  native Linux. Ordinary SQL-only local deployments need no object-storage service.
- AWS EC2 bootstrap gets a public IPv4 for outbound access, but no public SQL/SSH
  ingress. Lambda has a private subnet/security group and a read-only database login.
- The AWS storage stack owns a staging bucket, server, disk, endpoint, secret and
  roles. Remote loading uses S3/SSM; the measured function does not perform loading.
  Database-role S3 writes are limited to report objects.

## Framework fixes made during integration

1. SQL storage configuration participates in resource serialization and caching.
   Existing non-SQL benchmark module behavior remains intact.
2. Shared SQL wrapper code is included in SQL package hashes and copied only for
   the SQL module; changing it does not invalidate unrelated Python package hashes.
3. AWS creation/cached updates receive the backend's execution role, VPC and runtime
   credential reference through SeBS. The backend uses SeBS's configured AWS session.
4. Local SQL connection settings are reconciled when reusing cached functions.
   Normal SeBS cleanup now removes local function containers and handles Docker
   auto-removal without a second conflicting deletion.
5. Local initialization and deployment serialization support an absent object
   store, which is needed for SQL-only runs.
6. The local Python runner's end timestamp now follows `function.handler`.
   SeBS mounts the current runner source with Python function containers so the
   fix also applies to previously downloaded run images. A warm-invocation test
   checks that function duration includes the SQL handler interval.

## Configuration and resource defaults

- PostgreSQL 16.16/bookworm, pinned by image digest; Psycopg 3.3.9.
- Runtime requires Python 3.10+; tested local function: 3.11/x64. AWS example: 3.12/x64.
- Docker storage default: 2 CPUs, 4096 MiB, host port 9013 bound to loopback.
- AWS example: `m7i-flex.large`, 2 CPUs/6144 MiB container, 64 GiB gp3 disk.
  Flex CPU availability can vary. Availability/quotas are checked in the account.
- Shared buffers: min(1 GiB, one quarter of configured container memory);
  work_mem 32 MiB, max_connections 32, max_parallel_workers_per_gather 1.
- Function default: 512 MiB, 300-second timeout; SQL default statement limit 120 s,
  configurable up to 240 s. Function and database allocations are independent.
- AWS bootstrap TLS certificates expire after 30 days; renew/recreate before expiry.
- `test` SF0.01, `small` SF1, `large` SF10; the existing
  `SEBS_SQL_SCALE_FACTOR` override also applies.

## First AWS smoke observation and metrics-collection fix

One cold Q6 at SF0.01 validated successfully. Saved native AWS measurements:

| Measurement | Value |
|---|---:|
| AWS execution | 2.229150 s |
| AWS initialization | 74.420 ms |
| Function execution | 2.221325 s |
| Client invocation | 2.806783 s |
| Billed duration | 2304 ms |
| Allocated / peak function memory | 512 / 107 MB |
| Lambda billed GB-seconds | 1.152 |
| SQL execution plus result fetch | 8.590802 ms |
| Database connect | 150.653754 ms |
| Credential lookup | 1069.914676 ms |

The first `benchmark process` failed when its redundant CloudWatch Logs query's
saved time window was rejected with `MalformedQueryException`. Complete native
REPORT timing/billing was already in `experiments.json`, returned by SDK invocation.
The AWS collector now reuses complete saved reports and queries only missing ones.
The fallback pads the time range, can adjust for an exact new log group's creation
time, polls Scheduled/Running states correctly and tolerates duplicate REPORTs.
Authorization/retention failures are not silently hidden. Nine offline regression
tests cover these paths. The fix was verified on the actual saved smoke record,
without a CloudWatch call, SQL rerun or measurement-file edits.

The user reran smoke processing successfully, then invoked and processed the
five-query workload. All ten answers validated (each query twice):

| SF0.01 workload mean | Cold (2 invocations) | Warm (8 invocations) |
|---|---:|---:|
| AWS execution | 2.282240 s | 0.049920 s |
| Function execution | 2.278328 s | 0.047744 s |
| AWS initialization | 102.080 ms | not reported |
| SDK client timer (wall clock) | 2.768970 s | 0.616045 s |

One warm Q12 recorded 2.211525 s in the SDK wall-clock timer but only
0.408460 s in the workload's monotonic service timer. The roughly 1.8-second
wall-clock discontinuity inflated the client mean. Keep the original files; do
not substitute corrected measurements. The SDK trigger now measures elapsed
latency with `perf_counter_ns`, retaining wall timestamps for cloud log lookup.
An offline regression checks forward and backward clock adjustments. The
workload's monotonic warm service latencies were 0.374–0.429 s after rounding.
AWS execution/billing measurements are unaffected. This small run does not
isolate query type, database cache effects or connection policy.

Next run the same ten-request workload at SF1 (`small`), then connection-policy
and larger workload experiments. There are no PostgreSQL comparative cost/latency
tables yet.

## Next live step

From the active checkout, with AWS credentials set:

```bash
sebs storage preflight sql --region us-east-1
```

For a fresh deployment, copy/fill `configs/storage-postgresql-aws.example.json`
and follow the guide. For the existing deployment, proceed to SF1 using the same
SQL storage attachment and cache, with a fresh workload output directory. Retain
state and cache until cleanup.
There are no PostgreSQL cloud latency/cost tables to report yet.

A fair comparison must include database uptime/idle costs, disk, IPv4, private
endpoint, secrets/staging/log costs as applicable, plus Lambda. PostgreSQL native
row storage/indexes differ from DuckDB Parquet scans. Keep engine allocations,
connection/cache policy and driver location explicit. First terminal-driven runs
are smoke tests; cloud-driver performance comparisons follow live validation.

## Cleanup contract

`storage stop sql` retains the database. Docker's remove-containers option may
remove the stopped container while retaining its named volume/private state.
AWS stop stops EC2 only; EBS/endpoint/secret costs remain.

Use the dedicated SeBS cache to preview/remove function resources, then
`sebs storage remove sql STORAGE.json --delete-data --dry-run` and the real removal.
AWS removal refuses functions still using its role and draining Lambda interfaces;
keep the role intact and retry after those interfaces are gone. Confirm
`DELETE_COMPLETE`. Local results, ordinary SeBS S3 buckets and CloudWatch logs are
separate from the storage stack and must be retained/cleaned intentionally.

## Verification commands

The main offline checks are documented in the guide. The comprehensive recorded
run included SQL, existing AWS/Azure/GCP SQL adapters/evaluation, credential/resource
regressions and the AWS library-trigger tests: 173 passing tests.

```bash
python -B -m unittest tests.test_sql_storage tests.test_sql_postgresql \
  tests.test_aws_function_options tests.test_sql_query tests.test_sql_workload -q

SEBS_TEST_POSTGRES_DOCKER=1 python -B -m unittest \
  evaluation.sql.test_postgres_integration -v
```

The opt-in integration run has seven checks: five query answers, data reuse/input
isolation, read-only/mismatch handling, actual SeBS function build/invocation/cache
and warm timing, connection reuse, stop/restart, and actual storage CLI lifecycle.
Other FaaS adapters have SQL environment wiring, but no Azure/GCP/OpenWhisk live
PostgreSQL deployment has been validated. The current cloud deployment target is AWS.
