# PostgreSQL SQL benchmark in SeBS

`720.sql-postgresql` sends one complete TPC-H query per function invocation to a
separate PostgreSQL server. It uses SeBS storage management, input preparation,
function packaging/deployment, validation, workload replay and provider metrics.
`710.sql-query` continues to execute DuckDB inside the function over object files.

PostgreSQL is a reusable SQL backend, analogous to self-hosted ScyllaDB for NoSQL.
The benchmark declares `modules: ["sql"]` and an `allocate_sql` input hook. SeBS
attaches the storage supplied through `--storage-configuration`; input generation
prepares its tables before deploying/invoking the function. There is no separate
dataset manifest, manual loading command or evaluation script to run.

The implemented storage providers are Docker and AWS EC2. Local Docker and AWS
Lambda have been exercised end to end. All five query
types passed at SF1; SF10 includes recorded timeout and separate tuning results.
Other FaaS adapters can inject SQL settings, but their network/deployment paths
have not been validated for this benchmark.

Handoff/status: [POSTGRESQL_HANDOFF.md](../handoff/POSTGRESQL_HANDOFF.md).

## Prerequisites

Run commands from the repository root, in the checkout containing this benchmark.
Use the existing [SeBS AWS setup](sql-aws-setup.md) for Docker and AWS credentials.
For a new environment (Python 3.10+ and a working Docker installation):

```bash
python3 -m venv python-venv
source python-venv/bin/activate
python -m pip install -e '.[sql]'
sebs --help
unset SEBS_SQL_SCALE_FACTOR
docker info --format 'Docker: {{.ServerVersion}} | {{.OSType}}/{{.Architecture}}'
```

The runtime driver requires Python 3.10 or later.

The `sql` extra installs the host-side PostgreSQL driver for local data loading.
SeBS's SQL module separately adds the pinned driver to function build packages.
DuckDB 2.0.0 generates the TPC-H dataset during preparation; it does not execute
SQL inside the PostgreSQL benchmark's function.

**WSL only:** if PATH selects a broken Docker Desktop shim, use the working Docker
CLI/integration described in the base setup guide. SQL function and database
containers use the same Docker bridge, including on WSL. The remaining commands
apply to Linux generally; AWS steps require an AWS account.

## Local deployment

Start a PostgreSQL container through SeBS:

```bash
sebs storage start sql configs/storage-postgresql.json \
  --output-json results/postgresql-storage.json

sebs storage status sql results/postgresql-storage.json
```

The default is PostgreSQL 16.16/bookworm, pinned by image digest, 2 CPUs, 4096 MiB,
32 connections, work_mem 32 MiB and at most one parallel worker per gather. The
host-side port is 9013, bound to loopback. A named Docker volume holds the database.
Read-only application credentials and admin credentials are generated; private
state is stored with mode 0600 under `results/postgresql-storage/private.json`.
The generated attachment and cache contain state references, not passwords.
Keep this state directory: it is needed to reconnect to the retained volume.

Run a tiny Q6 through the normal benchmark path:

```bash
sebs benchmark invoke 720.sql-postgresql test \
  --config configs/sql-postgresql-local.example.json \
  --deployment local \
  --architecture x64 \
  --language python --language-version 3.11 \
  --trigger http --repetitions 1 --validate \
  --storage-configuration results/postgresql-storage.json \
  --cache cache-postgresql-local \
  --output-dir results/postgresql-local-smoke \
  --verbose
```

`test` generates SF0.01 (unless `SEBS_SQL_SCALE_FACTOR` overrides it). `small` generates SF1; `large` generates SF10. The setup
loads the same generated Parquet contents as `710.sql-query` into native
PostgreSQL heap tables, preserving decimals/dates, adding declared primary keys,
running ANALYZE, and checking row counts and hashes. This occurs before invocation
timing. The function uses the shared SQL wrapper and a read-only login.

Each storage/benchmark/input combination has its own schema. `test` and `small`
coexist; changing size does not overwrite another input. Repeating the same input
reuses its verified fingerprint. If contents change within the same input identity,
pass `--update-storage` to replace only its owned schema. Every invocation checks
that its dataset fingerprint matches PostgreSQL.

## AWS deployment

Set AWS credentials in the terminal used for these commands. Temporary credentials
also require `AWS_SESSION_TOKEN`. Use the same account/region throughout; keep a
separate SeBS cache for this experiment. The orchestration identity needs EC2,
network, IAM/PassRole, CloudFormation, SSM, Secrets Manager, S3 and Lambda deployment
permissions. The storage backend creates its own scoped EC2/Lambda runtime roles.
Never put AWS access keys or database passwords in tracked configuration.

First perform read-only account/subnet/image checks:

```bash
sebs storage preflight sql --region us-east-1
```

Copy the example and fill `subnet_id` and `ami_id` from that output:

```bash
cp configs/storage-postgresql-aws.example.json configs/storage-postgresql-aws.json
```

Choose an x64 instance type available to your account with enough resources for
the configured PostgreSQL container plus at least 1 GiB host memory. Defaults are
`m7i-flex.large` (2 vCPU, 8 GiB) and a PostgreSQL container capped at 2 CPUs/6144 MiB.
Flex CPU availability can vary. Quotas/free-trial restrictions still apply.
The subnet needs an active Internet Gateway default route for EC2 bootstrap;
VPC DNS support/hostnames must be enabled. The backend refuses an existing private-
DNS Secrets Manager endpoint conflict instead of modifying another deployment.
Use a separate suitable test VPC if necessary.

Start PostgreSQL storage:

```bash
sebs storage start sql configs/storage-postgresql-aws.json \
  --output-json results/postgresql-aws-storage.json

sebs storage status sql results/postgresql-aws-storage.json
```

This creates **billable resources**: one EC2 instance, a 64 GiB gp3 disk, public IPv4
for outbound bootstrap/SSM, an S3 staging bucket, a Secrets Manager secret and one
interface endpoint. PostgreSQL port 5432 is private and allowed only from the
Lambda security group. No SSH ingress, NAT gateway or RDS instance is created.
TLS verifies the server certificate and private-IP hostname. Bootstrap certificates
last 30 days; recreate/renew the experiment before expiry.

The start command waits for CloudFormation, SSM and PostgreSQL readiness. The
attachment is written before the wait, and requests/state are checkpointed under
`results/postgresql-aws-storage/`. If the wait times out, inspect `storage status`
and retry **start with the same configuration/state directory**; do not launch a
second stack. On a failed/rolled-back stack, inspect its CloudFormation events
before removing it and creating a fresh experiment state directory.

Run one Q6 and validate it:

```bash
sebs benchmark invoke 720.sql-postgresql test \
  --config configs/sql-postgresql-aws.example.json \
  --deployment aws \
  --architecture x64 \
  --language python --language-version 3.12 \
  --trigger library --repetitions 1 --validate \
  --storage-configuration results/postgresql-aws-storage.json \
  --cache cache-postgresql-aws \
  --output-dir results/postgresql-aws-smoke \
  --verbose
```

SeBS generates the input, transports it through the storage backend, waits for the
atomic PostgreSQL load, then builds/deploys Lambda normally. The SQL backend supplies
its VPC, read-only credential reference and execution role automatically. You do
not hand-edit Lambda settings or install a layer. The function is 512 MiB; the
separate database allocation is independent. SDK invocation uses no public URL.

Process AWS timing/billing through the existing command. Complete native reports
already returned by SDK invocation are reused; CloudWatch is queried only for
missing reports. New-log-group time boundaries are handled by the collector.
Processing does not resubmit SQL queries:

```bash
sebs benchmark process \
  --config configs/sql-postgresql-aws.example.json --deployment aws \
  --cache cache-postgresql-aws --output-dir results/postgresql-aws-smoke

sebs benchmark statistics results/postgresql-aws-smoke/results.json
```

A timeout has an unknown server-side outcome; inspect logs before repeating SQL.
The runtime does not replay a failed query. A tiny smoke invocation is a functional
check, not a performance comparison.

## Query workload and connections

The queries are exactly Q1, Q3, Q6, Q12 and Q21 from `710.sql-query`; a test checks
that their SQL text stays equal. They cover filtering, grouping/aggregation,
joins/top-N, conditional aggregation and correlated EXISTS/NOT EXISTS. There is
no query splitting, Lambda coordinator, shuffle or distributed aggregation.
PostgreSQL may use its ordinary internal CPU workers.

Both example configs contain Q6/Q1/Q12/Q3/Q21 repeated twice, spaced by two seconds,
with at most two in flight. Each request is a complete query in a separate
invocation. For AWS:

```bash
sebs experiment invoke sql-workload \
  --config configs/sql-postgresql-aws.example.json --deployment aws \
  --storage-configuration results/postgresql-aws-storage.json \
  --cache cache-postgresql-aws --output-dir results/postgresql-aws-workload \
  --verbose

sebs experiment process sql-workload \
  --config configs/sql-postgresql-aws.example.json --deployment aws \
  --cache cache-postgresql-aws --output-dir results/postgresql-aws-workload

sebs benchmark statistics \
  results/postgresql-aws-workload/sql-workload/invocations-processed.json
```

For a local workload, use the local example, `--deployment local`, the local storage
attachment/cache, and a fresh output directory. The example selects HTTP locally.
Dataset validation runs after timed replay. Existing perf-cost experiments can
also use this benchmark; they measure the function client together with its
remote SQL request, not PostgreSQL in isolation.

Parameters live in `experiments.benchmark-inputs.720.sql-postgresql`:

| Parameter | Default | Meaning |
|---|---|---|
| `queries` | `["q6"]` | Exactly one approved query per ordinary invocation |
| `connection_policy` | `fresh` | `fresh` closes each connection; `reuse` keeps one per warm environment |
| `statement_timeout_ms` | `120000` | PostgreSQL statement limit, from 1 to 240000 ms |

Run connection policies into different output directories and report observed
`connection_reused` flags. Warm Lambda does not imply an existing database
connection or a cold database. Credentials are cached per runtime configuration;
after rotating the secret, restart/redeploy the function environments. There is
no global pool and prepared-statement auto-caching is disabled.

Each result includes per-query microseconds for credential lookup, connection,
dataset/setup check, SQL execution plus fetching rows, and total handler work.
`query_time` includes protocol/network time. SeBS adds client/provider durations,
cold-start classification and AWS native billing. Preserve `requests.json`,
`invocations-processed.json`, and the storage state's `datasets/*.json` reports
(row counts, file hashes, PostgreSQL version, size and key settings).

## Fair comparison and cost

The terminal runs the first workload driver. Use the same driver location for any
comparison, and a cloud driver for formal cloud latency studies. PostgreSQL row
storage/indexes, DuckDB Parquet scans, CPU/memory allocations, connection policy and
cache state must be reported. These compare application architectures; they do
not make both engines perform identical physical work.

Count Lambda **plus** database uptime/idle periods, EBS, IPv4, the private endpoint,
Secrets Manager, staging S3 and applicable logs/network costs. This PostgreSQL
server is continuously provisioned; a Lambda frontend does not make its database
serverless. The recorded cost model includes the database on both paths and reports its
assumptions and exclusions in the handoff.

## Framework perf-cost options

The native perf-cost experiment accepts `trigger: "library"` for the AWS SDK;
its default remains HTTP. Optional `record-attempts: true` saves initial and
classification-excluded invocations for billing, `fail-on-error: true` stops on
execution errors, and `max-attempts` bounds classification resampling. Existing
experiments retain their default behavior. The saved statistics distinguish
retained sample count from invocation attempt count. Cold classification refers
to the function environment; it does not reset PostgreSQL or its caches.

## Cleanup

Keep the storage attachment and dedicated cache until cleanup is verified.
`storage stop sql` retains the database. On AWS, stopping EC2 still retains the
disk, endpoint and secret, so use removal to finish the deployment.

Preview the dedicated deployment, then remove its resources through SeBS:

```bash
sebs resources cleanup --dry-run \
  --config configs/sql-postgresql-aws.example.json --deployment aws \
  --cache cache-postgresql-aws --output-dir results/postgresql-cleanup-preview

sebs resources cleanup \
  --config configs/sql-postgresql-aws.example.json --deployment aws \
  --cache cache-postgresql-aws --output-dir results/postgresql-cleanup-functions

sebs storage remove sql results/postgresql-aws-storage.json --delete-data --dry-run
sebs storage remove sql results/postgresql-aws-storage.json --delete-data
sebs storage status sql results/postgresql-aws-storage.json
```

Require the dry run to identify only the dedicated experiment. Full SeBS cleanup
removes the cached function together with its logs and ordinary benchmark bucket.
The SQL storage stack separately owns the PostgreSQL server, disk, staging bucket,
endpoint, secret and roles. Removal destroys its database. Confirm DELETE_COMPLETE.
If Lambda network interfaces are still draining, keep their role and retry SQL
removal after AWS releases them. If the function was removed in an earlier separate
command, its log group may need a separate exact-name cleanup because the cached
function entry is already gone. The evaluation guide provides a billing-report
archive and final verification helper for that case.

For local storage use the local example/cache and clean local function deployments
through SeBS, then run:

```bash
sebs storage remove sql results/postgresql-storage.json --delete-data --dry-run
sebs storage remove sql results/postgresql-storage.json --delete-data
```

Preserve local result files for analysis. If using the optional cloud driver,
collect all results and delete its separate stack before removing SQL storage;
see the evaluation guide. Other experiments/deployments are outside this cleanup.

## Implementation and checks

| Location | Responsibility |
|---|---|
| `sebs/faas/sql.py` | SQL storage interface |
| `sebs/storage/sql_config.py` | Serializable SQL configuration |
| `sebs/storage/postgresql.py` | Docker lifecycle, attachment and dataset identity |
| `sebs/storage/postgresql_loader.py` | Transactional loading, types, keys and hashes |
| `sebs/storage/aws_postgresql*.py` | AWS provisioning, bootstrap, S3/SSM loading and removal |
| `benchmarks/wrappers/common/python/sql.py` | Credentials, verified TLS, fresh/reused connections and timings |
| `720.sql-postgresql/input.py` | TPC-H generation, table declarations and validation |
| `720.sql-postgresql/python/function.py` | Select one approved query and return its rows |
| SeBS deployment adapters and packaging | Inject SQL configuration and package the shared runtime |

The benchmark and storage implementation do not import evaluation code. AWS SDK
invocation uses monotonic client timing and disables automatic retries. Native
REPORTs returned by SDK invocations are reused by metrics processing; missing
reports use CloudWatch. Runtime CA certificates are published atomically so
concurrent clients cannot read a partially written certificate file.

Offline checks (normally a few seconds, no cloud calls or Docker):

```bash
python -B -m unittest tests.test_sql_storage tests.test_sql_postgresql \
  tests.test_aws_function_options tests.test_sql_workload tests.test_perf_cost_sdk \
  tests.aws.test_library_trigger tests.aws.test_metrics -q
```

Reproduction and optional diagnostics: [AWS PostgreSQL evaluation](sql-postgresql-evaluation.md).
Results, limitations and cleanup: [handoff](../handoff/POSTGRESQL_HANDOFF.md).

## Azure provider

The same benchmark now has a native Azure storage/Flex Functions implementation.
Live Azure validation is pending; follow the [Azure guide](sql-postgresql-azure.md).
This is separate from the older Azure DuckDB/Synapse evaluation.
