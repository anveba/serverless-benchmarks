# 720.sql-postgresql — external SQL database

**Category:** Analytics. **Language:** Python. **Validated deployments:** local
Docker and AWS Lambda with self-hosted PostgreSQL on EC2. All five queries passed
SF1 validation and measured workload replay. SF10 includes separate timeout and
index-tuning observations; it is not a replicated SF10 performance study.

A function executes one complete TPC-H query on external PostgreSQL and returns
its result. The database stores native tables and performs the SQL computation.
This exercises database connections, network/protocol overhead and remote SQL
execution. `710.sql-query` instead runs DuckDB inside the function over Parquet.

[Setup, normal SeBS commands, measurements and cleanup](../../../docs/sql-postgresql-aws.md).

## SeBS integration

- `modules: ["sql"]` opts into the shared PostgreSQL runtime wrapper and driver.
- Start storage with `sebs storage start sql` and pass its generated JSON through
  `--storage-configuration`, just like self-hosted NoSQL storage.
- `allocate_sql()` declares the storage requirement. Normal input preparation
  generates data and asks the backend to prepare its tables before invocation.
- The storage backend owns configuration/caching, readiness, transport/loading,
  runtime settings, stop/restart and removal. AWS details stay out of the function.
- Normal SeBS build/cache/deployment, output validation, perf-cost and sql-workload
  collection apply. Local SQL-only runs need no object-storage service.

## Workload

Q1, Q3, Q6, Q12 and Q21 are the same complete statements as in `710.sql-query`.
They cover grouped aggregation, joins/top-N, filtering/aggregation, conditional
aggregation, and correlated EXISTS/NOT EXISTS. There are no parallel query stages,
intermediate partitions, Lambda coordinator or result-merging functions.
PostgreSQL can use its normal internal CPU parallelism.

`test` = SF0.01, `small` = SF1, `large` = SF10. Loading uses the exact generated
Parquet contents, preserving decimal/date values, adds primary keys and runs
ANALYZE. Preparation is outside invocation timing. Schemas are isolated by storage,
benchmark and input identity; each invocation checks its dataset fingerprint.
The existing TPC-H reference answers validate results after workload replay.

Default: one Q6, fresh connection, statement timeout 120 seconds, function timeout
300 seconds and function memory 512 MiB. Input parameters may select exactly one
query, `connection_policy=fresh|reuse`, and `statement_timeout_ms` from 1 to 240000.
A reused connection belongs to one warm environment, not a shared pool. A failed
query is never retried. Database/OS caches remain enabled in both modes.

## Measurements and costs

The response reports microseconds for credential lookup, connection establishment
(or reuse), dataset/setup checks, executing SQL plus fetching results, and total
handler work. It includes engine/driver versions and observed credential-cache
and connection-reuse flags. SQL execution time includes network/protocol time;
it is not isolated database CPU time. SeBS supplies client/provider durations,
cold-start labels and native AWS Lambda billing.

Compare full architecture costs: function invocations plus PostgreSQL provisioning,
idle time, disk and applicable endpoint/secret/storage/network costs. PostgreSQL on
EC2 remains provisioned between requests. Resource allocations, row storage/indexes,
Parquet scans and driver placement must be explicit in comparisons.

Recorded AWS results and limitations: [PostgreSQL handoff](../../../handoff/POSTGRESQL_HANDOFF.md).
Native benchmark setup requires no evaluation helper. The optional cloud driver,
resource diagnostics, tuning and cost/reporting tools are documented separately in
the [evaluation guide](../../../docs/sql-postgresql-evaluation.md).
