# Native PostgreSQL benchmark on Azure

This deploys `720.sql-postgresql` through SeBS: one complete analytical query per
invocation on a separate PostgreSQL VM. It is distinct from the earlier
DuckDB/VM/Synapse evaluation. The Azure implementation is covered by offline tests;
**a live Azure deployment and answer-validation run are still required**.

## Architecture and scope

- Same benchmark SQL, dataset generator, fingerprint checks and validation as AWS.
- SQL storage provider creates an owned resource group: Ubuntu 24.04 VM, 64 GiB
  managed OS/data disk, private network/subnets, egress public IP, Blob staging,
  Key Vault and managed identities. It uses the pinned PostgreSQL 16.16 image,
  Psycopg 3.3.9 and the existing generic dataset loader.
- PostgreSQL binds its private IP with verified TLS. NSG admits port 5432 only from the
  Functions subnet and private server/driver subnet; other inbound traffic is denied.
- The SQL Function App uses Flex Consumption, 512 MiB by default, zero always-ready
  instances and per-instance HTTP concurrency 1. It joins the private function
  subnet and reads only the reader credential from Key Vault with managed identity.
- Deployment uses the existing SeBS builder/package/cache, with ARM Flex and
  OneDeploy publication. It does not require upgrading the old Functions Core Tools
  in the management image. The ready-to-run package includes its driver dependencies.
- Other benchmarks retain their existing Azure deployment path. SQL Function Apps
  have their own owned Flex plan, storage account and Application Insights/workspace.
  Their resource identities are recorded before allocation for cleanup recovery.

Legacy Consumption has no outbound VNet integration; Flex is used to retain
execution-based billing while reaching a private database. See Microsoft's
[networking comparison](https://learn.microsoft.com/en-us/azure/azure-functions/functions-networking-options),
[Flex model](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan),
[package deployment](https://learn.microsoft.com/en-us/azure/azure-functions/deployment-zip-push),
and [managed identity endpoint](https://learn.microsoft.com/en-us/azure/app-service/overview-managed-identity).

This integration does not yet port the AWS cloud-driver comparison, native Flex
billing collector or cost reporter. Native benchmark invocations and sql-workload
are the first validation targets. Do not run the old DuckDB Azure comparison
scripts against this storage attachment or apply legacy Consumption cost meters
to Flex. Record VM/disk/runtime differences before making provider comparisons.

## 1. Dependencies and credentials

Run from the checkout containing these files, using a working Docker installation:

```bash
source python-venv/bin/activate
python -m pip install -e '.[sql]'
sebs --help
docker info --format 'Docker: {{.ServerVersion}} | {{.OSType}}/{{.Architecture}}'
unset SEBS_SQL_SCALE_FACTOR
```

On a fresh checkout, create the environment first with `python3 -m venv python-venv`.
The documented function build is Python 3.11/x64; the host can use Python 3.10+.
Only the Docker PATH/integration workaround in the general Azure setup guide is
WSL-specific. The storage/function integration itself is not specific to WSL.

Use the existing Azure service principal and set these in every local terminal
that runs SeBS. Keep secrets out of configuration files and chat:

```bash
export AZURE_SECRET_APPLICATION_ID='<application-client-id>'
export AZURE_SECRET_TENANT='<tenant-id>'
export AZURE_SECRET_PASSWORD='<client-secret>'
export AZURE_SUBSCRIPTION_ID='<subscription-id>'
```

The deployment principal needs resource creation/deletion, provider registration
and role-assignment write permission. Contributor alone cannot create the managed
identity role assignments. Start checks effective permission on its owned SQL
resource group before requesting the VM. If it stops for permission, an authorized
subscription administrator can grant RBAC administration to the deployment principal
on that exact group; then repeat the same start command/state. No benchmark query
is retried. This avoids needing to put database passwords in host configuration.

## 2. Read-only preflight

Expected duration 1–3 minutes; no resource creation or SQL:

```bash
sebs storage preflight sql --provider azure \
  --region swedencentral \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --vm-size Standard_D2s_v5
```

Review provider registrations, VM subscription/region restrictions, regional/family
vCPU quota and Flex region availability. This is not a capacity reservation. Select
a region/VM available to your subscription; the default database allocation needs
2 vCPUs and at least 7 GiB host memory (6 GiB container plus 1 GiB host allowance).
Registration of missing providers happens only during an explicit storage start.

## 3. Create local configuration

The following creates ignored local files from tracked examples:

```bash
python - <<'CONFIG'
import json, os
from pathlib import Path
subscription=os.environ["AZURE_SUBSCRIPTION_ID"]
for source,target in [
    ("configs/storage-postgresql-azure.example.json","configs/storage-postgresql-azure.json"),
    ("configs/sql-postgresql-azure.example.json","configs/sql-postgresql-azure.json"),
]:
    data=json.loads(Path(source).read_text())
    if "sql" in data:
        data["sql"]["postgresql"]["azure_subscription_id"]=subscription
    else:
        data["deployment"]["azure"]["credentials"]["subscription_id"]=subscription
    Path(target).write_text(json.dumps(data,indent=2)+"\n")
CONFIG
```

Both examples use swedencentral. If changing it, edit both region fields. Edit the
VM size/disk SKU after preflight if necessary. The VNet range must be a nonconflicting
private IPv4 /16 through /22. Storage configuration is frozen by its state directory;
use fresh state for a different allocation or after deleting the deployment.

## 4. Start PostgreSQL through SeBS

Expected duration 10–20 minutes for packages, Docker image and role propagation:

```bash
sebs storage start sql configs/storage-postgresql-azure.json \
  --output-json results/postgresql-azure-storage.json

sebs storage status sql results/postgresql-azure-storage.json
```

This creates billable Azure resources. Start waits for the ARM deployment, managed
bootstrap command and PostgreSQL readiness. The attachment/state are saved before
waiting. Preserve results/postgresql-azure-storage and its command checkpoints.
Unknown submission outcomes are inspected, not blindly sent again. A new explicit
start may retry a known-terminal setup failure on the same owned resource IDs;
bootstrap passwords/certificates are retained. Dataset/benchmark queries are never
automatically replayed. Use status/start with the same config after interruption.

Public attachment/state contain references, not passwords. Admin credentials stay
in mode 0600 files on the VM. Key Vault stores the read-only login/public CA. Bootstrap
certificates last 30 days; renew/recreate before expiry. Managed disk performance is
not equivalent to AWS gp3; record its SKU instead of assuming equal I/O allocation.

## 5. Native smoke and validation

Expected duration 3–8 minutes initially for input loading, build and publishing:

```bash
sebs benchmark invoke 720.sql-postgresql test \
  --config configs/sql-postgresql-azure.json --deployment azure \
  --architecture x64 --language python --language-version 3.11 \
  --trigger http --repetitions 1 --validate \
  --storage-configuration results/postgresql-azure-storage.json \
  --cache cache-postgresql-azure \
  --output-dir results/postgresql-azure-smoke --verbose
```

`test` is SF0.01, `small` is SF1 and `large` is SF10. Start with the default Q6 and
require successful answer validation. Loading is outside timing; immutable Parquet
objects and loader bundles use Blob transport/VM identity, then generic transactional
loading into native PostgreSQL tables. Function publication/indexing issues are
separate from SQL execution; inspect saved state rather than resubmitting a timed
query blindly. The function uses native SeBS HTTP transport and the actual Flex
hostname returned by Azure, not a guessed hostname.

Process timings without executing SQL again (allow 1–3 minutes for Azure telemetry):

```bash
sebs benchmark process \
  --config configs/sql-postgresql-azure.json --deployment azure \
  --cache cache-postgresql-azure --output-dir results/postgresql-azure-smoke

sebs benchmark statistics results/postgresql-azure-smoke/results.json
```

Application Insights is configured explicitly and sampling is disabled for these
SQL apps. Missing/delayed provider telemetry is not a zero duration or zero bill.
Do not infer native billing from handler time or a legacy Azure memory label.

## 6. Native multi-request workload

After the smoke passes, run the configured ten arrivals, one query per invocation:

```bash
sebs experiment invoke sql-workload \
  --config configs/sql-postgresql-azure.json --deployment azure \
  --storage-configuration results/postgresql-azure-storage.json \
  --cache cache-postgresql-azure --output-dir results/postgresql-azure-workload --verbose

sebs experiment process sql-workload \
  --config configs/sql-postgresql-azure.json --deployment azure \
  --cache cache-postgresql-azure --output-dir results/postgresql-azure-workload

sebs benchmark statistics results/postgresql-azure-workload/sql-workload/invocations-processed.json
```

Allow 1–3 minutes plus first-use setup. The example tests all five query IDs twice
with 2 seconds spacing and at most 2 in flight, initially at SF0.01. Change the
sql-workload input-size to small for SF1, and use a new output directory. Connection
policies/timeouts live in experiments.benchmark-inputs.720.sql-postgresql, as on AWS.
The default statement limit is 120 s. Long synchronous Azure HTTP requests also have
platform response limits; do not assume increasing a SQL deadline guarantees a
response. Keep unknown/failed outcomes and their cost evidence.

## 7. Cleanup

Collect all responses, dataset reports, metadata and any needed Azure usage meters
first. Use the dedicated cache and preview function deletion (normally 1–3 minutes):

```bash
sebs resources cleanup --resource-type functions --dry-run \
  --config configs/sql-postgresql-azure.json --deployment azure \
  --cache cache-postgresql-azure --output-dir results/postgresql-azure-cleanup-preview

sebs resources cleanup --resource-type functions \
  --config configs/sql-postgresql-azure.json --deployment azure \
  --cache cache-postgresql-azure --output-dir results/postgresql-azure-cleanup-functions
```

Native SQL function cleanup removes its app, code storage, Flex plan and owned
monitoring components. Then preview and delete SQL storage (allow 5–15 minutes):

```bash
sebs storage remove sql results/postgresql-azure-storage.json --delete-data --dry-run
sebs storage remove sql results/postgresql-azure-storage.json --delete-data
sebs storage status sql results/postgresql-azure-storage.json
```

SQL removal refuses live attached Function Apps. It can recover recorded orphan
support resources after a partial app creation, checking ownership before deletion.
Then it deletes only the owned SQL resource group. Confirm DELETE_COMPLETE; deletion
is asynchronous. Key Vault soft-deleted names can remain reserved for recovery;
this helper does not purge them. Stop only deallocates the VM and retains data;
disks, public IP, vault and Blob storage can remain billable.

SeBS's ordinary deployment resource group/data storage are separate. Inspect the
exact group with sebs resources list resource-groups and preview sebs resources
remove resource-groups EXACT_GROUP before deleting the dedicated deployment group.
That command matches prefixes: verify the preview contains only the intended group.
Keep local results/cache/state until all intended resource groups are confirmed gone.

## Offline verification

```bash
python -B -m unittest tests.test_sql_postgresql_azure tests.test_sql_storage \
  tests.test_sql_postgresql tests.test_sql_azure tests.test_azure_cli \
  tests.test_azure_resources tests.test_resources_remove -q
```

These mock the control/data planes and create no Azure/Docker resources. Live
Azure validation is still a separate required step. Handoff:
[AZURE_POSTGRESQL_HANDOFF.md](../handoff/AZURE_POSTGRESQL_HANDOFF.md).

## AWS-matched PostgreSQL evaluation (2026-11-22)

The native Azure private-PostgreSQL smoke, all five SF0.01 queries and the
10-request SF1 mixed workload have now validated. Do not rerun these setup checks.
The following baseline matches the AWS fixed invocation counts: each of five
queries runs 31 sequential times for each fresh/reuse policy (310 invocations).
The first invocation per cell is preserved but excluded from the reported groups.
Azure may dispatch sequential requests to different environments: the report lists
observed cold/warm counts and actual connection-reuse counts, rather than claiming
30 warm samples per cell. Raw responses and all attempts are kept.

Expected time: 15–30 minutes, depending on query speed and telemetry delay. The
persistent database remains billable. Run once from the repository root:

```bash
python -m evaluation.sql.postgres_azure_baseline run \
  --config configs/sql-postgresql-azure-sf1.json \
  --storage-configuration results/postgresql-azure-storage.json \
  --cache cache-postgresql-azure \
  --output-dir results/postgresql-azure-sf1-baselines
```

Read progress in another terminal (instant, local files only):

```bash
python -m evaluation.sql.postgres_azure_baseline status \
  --output-dir results/postgresql-azure-sf1-baselines
```

The run also processes each cell and writes summary.json; it does not require
separate manual processing per query. A recorded failed/unknown run is never
replayed. Preserve its files and diagnose before starting any replacement run.
Local client latency is not used for a fair cloud-driver comparison. Missing
provider measurements remain null; Flex costs are not inferred from handler time.

Remaining AWS-matched scope: paired direct/Functions moderate traffic with two
slots (3 x 100 requests per backend), bursts at two and eight slots (3 x 100
per backend for each limit), controlled cold/warm (3 samples of each per query),
SF10 checks, then native billing/infrastructure cost and cleanup. The paired Azure
PostgreSQL driver and Flex cost collectors still need implementation; the earlier
DuckDB driver cannot be substituted without adapting identities, TLS and results.
AWS sparse PostgreSQL traffic was not measured, so it is not required for parity.
AWS disk-throughput/index troubleshooting trials are conditional follow-ups,
not mandatory Azure baseline treatments.

### Controlled SF1 cold/warm parity

After baseline completion, request three cold and three warm retained samples per
query with fresh connections, matching AWS. SeBS changes an environment variable
and waits for propagation; the sampler verifies the actual cold flag and bounds
the number of attempts rather than assuming a restart succeeded. The database
cache is not reset. Initial/discarded attempts are preserved and answers validated.
No per-query code republish or memory sweep is needed; the native default is
512 MiB. Use the existing attachment, cache and SF1 dataset.

Expected duration: 15–25 minutes, including restart waits. Run once:

```bash
python -m evaluation.sql.postgres_azure_cold_warm run \
  --config configs/sql-postgresql-azure-sf1.json \
  --storage-configuration results/postgresql-azure-storage.json \
  --cache cache-postgresql-azure \
  --output-dir results/postgresql-azure-sf1-cold-warm-3 \
  --repetitions 3
```

Progress in another terminal (instant, local files only):

```bash
python -m evaluation.sql.postgres_azure_cold_warm status \
  --output-dir results/postgresql-azure-sf1-cold-warm-3
```

The runner validates and summarizes automatically; summary.json reports function,
SQL protocol/fetch, credentials and connect timings. Missing provider timings are
null; no native Flex usage is invented. The default is 30 retained samples plus
at least ten initial samples; wrong-classification attempts remain in the ledger.
On failure, preserve status/attempts and diagnose; rerunning a recorded job never
resubmits it. This is a native timing study, not the paired cloud-driver experiment.

### SF10 default-profile checks

After the controlled SF1 run, attempt Q6,Q1,Q12,Q3,Q21 once each at SF10 with fresh
connections and a 120000ms PostgreSQL statement limit, matching the first AWS
size check. This is not a replicated timing study. No additional indexes, disk
performance changes or server restarts are applied. SF1 datasets are retained.
The first query prepares/loads SF10 outside function measurement; subsequent
queries reuse that prepared dataset. Expected total duration: 20–45 minutes,
including first-time local generation, Blob upload and PostgreSQL loading.

```bash
python -m evaluation.sql.postgres_azure_sf10 run \
  --config configs/sql-postgresql-azure-sf1.json \
  --storage-configuration results/postgresql-azure-storage.json \
  --cache cache-postgresql-azure \
  --output-dir results/postgresql-azure-sf10-check
```

Progress (instant, local files only):

```bash
python -m evaluation.sql.postgres_azure_sf10 status \
  --output-dir results/postgresql-azure-sf10-check
```

The runner processes saved native results and writes summary.json. Known recorded
execution failures remain distinct from invalid answers and unknown outcomes. If
HTTP fails without a saved invocation, it stops the batch for inspection instead
of resubmitting or overlapping a possibly running query. A statement failure must
be diagnosed from native exception/server evidence before labeling it a timeout.
Use a new explicitly planned follow-up for a larger deadline or index treatment;
never overwrite the original censored result. Keep state/remote results until
collection is complete. Native Azure billing and paired-driver studies are still
separate pending work.

### Longer Q6 attempt with independent result collection

The initial SF10 Q6 produced a QueryCanceled exception around the configured
120-second statement limit. Preserve results/postgresql-azure-sf10-check as the
original failed treatment. The following is a new, explicit Q6 attempt with a
240-second SQL statement limit and the existing 300-second Function timeout.
It does not change PostgreSQL indexes, disk, CPU, memory, data or SQL text.
It does not guarantee Q6 completes before the new limit.

Native SeBS builds/deploys the same benchmark with an optional Azure wrapper
receipt. The wrapper atomically creates a running private Blob before executing
SQL, then saves success or an exception before returning HTTP. The write-only
SAS is scoped to one blob in the function's existing storage account, expires in
one hour and is not persisted in public local state. ETag conditions prevent
re-executing an already claimed job or overwriting a changed receipt. The helper first performs a query-free GET probe for the receipt-capable wrapper
and records its probe count separately. It then sends exactly one POST with redirects disabled; HTTP timeout/error leads only to
Blob reads. No background thread starts after returning from the function.
A crashed/killed host can leave the receipt unresolved; no retry is inferred safe.
Ordinary benchmark requests without sebs_sql_result retain the normal HTTP path.

Expected duration: 5–15 minutes, including rebuild/publish and up to four minutes
of SQL. The SF10 dataset is reused. Run once:

```bash
python -m evaluation.sql.postgres_azure_long_query start \
  --config configs/sql-postgresql-azure-sf1.json \
  --storage-configuration results/postgresql-azure-storage.json \
  --cache cache-postgresql-azure \
  --output-dir results/postgresql-azure-sf10-q6-240
```

Read local progress in another terminal (instant):

```bash
python -m evaluation.sql.postgres_azure_long_query status \
  --output-dir results/postgresql-azure-sf10-q6-240
```

Start collects and validates automatically. If the local observer is interrupted,
collect reads the existing receipt without a second SQL invocation (normally less
than one minute if finished, otherwise waits up to seven minutes):

```bash
python -m evaluation.sql.postgres_azure_long_query collect \
  --output-dir results/postgresql-azure-sf10-q6-240
```

summary.json is written only for a completed collected answer; receipt.json and
status.json preserve exceptions or unresolved state. Native answer validation uses
SF10 references. Function/SQL handler times exclude Blob publication; no provider
billing or client latency comparison is inferred. The receipt container is part
of the function's owned storage account and follows its existing cleanup. Preserve
collected results locally before deleting it. This is an extended-limit treatment,
not a replacement for the initial 120-second result or prior SF1 timing samples.

Azure's documented synchronous HTTP response limit is independent of function
execution timeout: https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale#function-app-time-out-duration

### Remaining SF10 queries after successful extended Q6

The separate Q6/240s run validated, measuring 115.713842s SQL protocol/fetch and
116.328521s SQL-handler time. Credentials were 586.38ms and connect 25.04ms.
Because it finished below 120s on this attempt, do not attribute its success solely
to the increased deadline: cache and run variation remain uncontrolled. Keep it
separate from the original cancellation. No native Flex billing is inferred.

Run the remaining Q1,Q12,Q3,Q21 once each at the original 120s statement limit,
using the same independent Blob result collection. Expected time: 8–20 minutes;
no Q6 repeat, SF10 regeneration, forced rebuild, index or disk treatment. Native
SeBS still updates the deployed function if its source hash actually changed.

```bash
python -m evaluation.sql.postgres_azure_sf10_remaining run \
  --config configs/sql-postgresql-azure-sf1.json \
  --storage-configuration results/postgresql-azure-storage.json \
  --cache cache-postgresql-azure \
  --output-dir results/postgresql-azure-sf10-remaining
```

Progress (instant, local files only):

```bash
python -m evaluation.sql.postgres_azure_sf10_remaining status \
  --output-dir results/postgresql-azure-sf10-remaining
```

Collection, correctness validation and summary are automatic. Recorded execution
failures are retained and do not prevent collecting the other queries. Unknown
or invalid outcomes stop the batch without retries. Batch complete means all
planned attempts ended; valid/failure counts distinguish correctness from job
completion. A recorded batch is never replayed. If a single local observer is
interrupted, use postgres_azure_long_query collect --output-dir <batch/query> to
collect that receipt only. Inspect before continuing any remaining batch work.
For an explicit separate larger-deadline treatment, the generic job helper also
accepts --query <id> --statement-timeout-ms 240000 with a new output directory;
its defaults retain the original Q6/240s command behavior.

### Diagnose SF10 cancellations before changing deadlines/resources

Several remaining SF10 queries returned durable QueryCanceled exceptions at the
initial 120s limit. Once the batch ends, capture disk/VM metadata, container
limits/counters, database settings and non-executing query plans. Expected time:
2–4 minutes. This runs metadata SELECTs and EXPLAIN without ANALYZE; it executes
no benchmark query and changes no PostgreSQL/disk allocation. Reports are saved
through the existing VM managed identity and Blob staging. A diagnostic Managed
Run Command/checkpoint and report objects are created; no VM is provisioned.

```bash
python -m evaluation.sql.postgres_azure_diagnose \
  --storage-configuration results/postgresql-azure-storage.json \
  --output-dir results/postgresql-azure-sf10-diagnostics \
  --label sf10-01
```

Use a fresh label for a deliberately new snapshot. Repeating the same label/payload
reuses the checkpointed operation, so its report may be an earlier snapshot.
Counters are cumulative; EXPLAIN estimates alone do not prove a CPU/I/O bottleneck.
The Azure managed disk SKU is not the AWS gp3 profile; compare measured limits,
memory/spill counters and estimated plans before proposing a separate treatment.
Preserve all failed attempts and the original profile. Do not interpret cancelled
queries as completed timings or silently replace them with tuned observations.

### Separate Q21 covering-index treatment

The extended 240s observations validated Q1 (132.367s SQL), Q12 (181.080s) and
Q3 (166.682s); Q21 still returned QueryCanceled. Preserve all original 120s/240s
results. Apply the exact fixed index profile from the AWS treatment to SF10 only:
lineitem(l_orderkey) INCLUDE(l_suppkey,l_receiptdate,l_commitdate), and
orders(o_orderkey) WHERE o_orderstatus='F', followed by ordinary VACUUM ANALYZE.
No SQL text, data values, VM, disk or container allocation changes. Azure's disk
profile remains different from the AWS 500MiB/s disk used for its indexed trial.
This is a separate tuned observation, not the default benchmark schema.

Expected preparation time: 5–20 minutes. No benchmark query executes. At least
8GiB free disk is checked; actual new index bytes and maintenance seconds are
reported separately. All benchmark traffic must have ended first.

```bash
python -m evaluation.sql.postgres_azure_q21_indexes prepare \
  --storage-configuration results/postgresql-azure-storage.json \
  --state-dir results/postgresql-azure-sf10-q21-indexes
```

The helper reuses the native storage's owned VM, managed-command checkpoints and
managed-identity Blob staging. Index ownership/fingerprint guards prevent replacing
unowned indexes. Completed report files prevent repeated maintenance. The Azure
renderer also fixes the AWS template's ownership-file trailing literal-backslash
newline, so saved ownership remains valid JSON. Inspect the preparation report
before the new Q21 attempt; the estimated plan does not itself execute Q21.

After successful preparation, run one distinct Q21 attempt (5–10 minutes including
control/collection, up to 240s SQL):

```bash
python -m evaluation.sql.postgres_azure_long_query start \
  --config configs/sql-postgresql-azure-sf1.json \
  --storage-configuration results/postgresql-azure-storage.json \
  --cache cache-postgresql-azure --query q21 --statement-timeout-ms 240000 \
  --output-dir results/postgresql-azure-sf10-q21-indexes/query
```

If restoring the original schema for future measurements, explicit remove uses
the same attachment/state-dir and removes only these owned evaluation indexes.
Capture and retain all measured reports before removing resources or data.

## Paired Azure PostgreSQL cloud-driver comparison

The indexed Q21 observation validated (170.250127s SQL; 170.851235s handler), so
all five query IDs now have a validated SF10 result across separate treatments.
Q21 used two added indexes with 377.572065s preparation and about 2.42GiB index
storage; keep this separate from its default-schema timeouts and from SF1.
These single observations do not establish replicated timing distributions.

Next is an AWS-matched SF1 comparison from a separate Azure driver VM: the same
trace to direct private PostgreSQL and to the native Azure Function. Planned
studies: moderate with two in flight, bursts with two and eight, 3x100 requests
per backend for each; backend order alternated. Costs/driver setup are separate.
The SF10-specific indexes do not change the separate SF1 namespace.

### Read-only driver preflight

Expected duration 1–3 minutes. No VM, roles, database load or benchmark SQL is
created/submitted. Writes only a local preflight.json. It reads the existing
Function package blob to verify bytes; it does not upload or republish code.

```bash
python -m evaluation.sql.postgres_azure_cloud_preflight \
  --config configs/sql-postgresql-azure-sf1.json \
  --storage-configuration results/postgresql-azure-storage.json \
  --cache cache-postgresql-azure \
  --state-dir results/postgresql-azure-cloud
```

This checks the current source hash and ZIP members, latest successful native
OneDeploy publication and its remote ZIP checksum, live Flex allocation/VNet/
SQL-identity references, existing SF1 dataset fingerprint, running PostgreSQL VM,
frozen Ubuntu image, regional/VM-family headroom for two more vCPUs and scoped
role-assignment permission. Quota availability does not reserve capacity. It
records expected Python3.11/Psycopg3.3.9 for the direct client; exact driver runtime,
private connectivity, artifact access and TLS must be validated after launch.

Review the preflight before driver provisioning. The driver launch/readiness,
paired replay and Flex-native billing adaptation are not yet implemented by this
step. Do not point the earlier DuckDB/Synapse evaluation helpers at this database
attachment or treat its metadata-only checks as completed performance results.
The persistent PostgreSQL VM remains billable while these steps are prepared.

### Launch the separate PostgreSQL comparison driver

The recorded preflight passed with Standard_D2s_v5 in Sweden Central, 2/4 regional
and DSv5-family vCPUs used, scoped RBAC permission, and verified native package/
SF1 snapshot. Launch creates one additional two-vCPU/8GiB driver VM, 32GiB managed
disk, private NIC with denied inbound access, public egress IP and a scoped Blob
role assignment. It leaves database/Function allocations and data unchanged.
The driver VM uses the SQL VNet server subnet; no SSH/database port is opened to
the Internet. Its SystemAssigned identity accesses experiment artifacts, while
the existing Function user-assigned identity supplies the same read-only Key
Vault SQL credentials. No service-principal secret or DB password is bundled.

Expected launch time: 3–8 minutes. Initial bootstrap may take another 10–20 minutes
for packages and the Python image. These resources consume credits/charges while
running. No TPC-H query is executed by launch/bootstrap/readiness.

```bash
python -m evaluation.sql.postgres_azure_cloud_driver launch \
  --storage-configuration results/postgresql-azure-storage.json \
  --cache cache-postgresql-azure \
  --state-dir results/postgresql-azure-cloud
```

The explicit source allowlist excludes results, caches, local private configs and
keys. Critical SQL source files are copied verbatim from the verified native ZIP.
Ubuntu/image/VM choices use the frozen preflight. The driver runs a Python3.11
container, pins Psycopg3.3.9, resolves/stores the base-image digest and image ID,
and saves pip freeze. Python patch version is recorded by readiness; minor
version/source/driver checks are mandatory. Container image installation has not
been validated offline; the live bootstrap/status output supplies that evidence.

Check bootstrap and readiness (normally 1–2 minutes per status call):

```bash
python -m evaluation.sql.postgres_azure_cloud_driver status \
  --storage-configuration results/postgresql-azure-storage.json \
  --state-dir results/postgresql-azure-cloud
```

Wait for DRIVER_READY, source_package_matches=true, private_postgres_tcp=true,
tls_verified=true, read_only=true and sf1_fingerprint_matches=true. Readiness
executes only role/metadata checks in a separate process; its credential cache
is not reused by measured workers. It does not invoke the Function or a TPC-H
query. Preserve driver-plan/request/template and driver-command checkpoints.
Repeated launch inspects its recorded deployment and never repeats an unknown
submission. Setup errors require inspection rather than deleting state/relaunching.
A resource name present without the owned launch checkpoint is never adopted.

The paired replay helper remains the next implementation step. Its code can be
installed on this existing driver without launching a replacement VM. No fair
comparison or Flex cost results exist yet merely because readiness passed.
Collect all results before final native Function/SQL cleanup; removing the owned
SQL group also removes this driver. Do not delete the database group just to stop
the driver while database experiments remain active.

Driver live validation exposed an invalid nested subnet ID in the initial
NIC template (`virtualNetworks/subnets/sql/server` instead of
`virtualNetworks/sql/subnets/server`). The renderer and canonical-ID regression
are fixed. An explicit repeat of launch can resume a known Failed/Canceled ARM
deployment with the same frozen artifact/resources after checking ownership of
all partially created driver VM/NIC/NSG/public-IP resources. It preserves the
original template under driver-retries and changes only the malformed subnet
reference. Running/succeeded deployments are inspected, not resubmitted; unknown
submission outcomes still require manual inspection. Keep the existing state.

### Paired regional replay: moderate then two/eight-slot bursts

Driver readiness passed live: Python3.11.18, Psycopg3.3.9, exact native source
members, private TCP, verified TLS, read-only role and matching SF1 fingerprint.
No analytical SQL was executed by readiness. The paired runner now reuses the
AWS DirectPostgresClient and shared SeBS scheduler/validator; Azure normalizes its
native HTTP result while preserving raw request/time/body observations.

Skip another small pilot: offline adapter/scheduler/reference interoperability
is covered, and both native query correctness and driver metadata are verified.
Start the AWS-matched moderate study once (1–2 minutes to submit; approximately
20–25 minutes detached execution): 3 repetitions x100 requests/backend, one arrival
every2s, max2 in flight, query cycle Q6,Q1,Q12,Q3,Q21. Direct-first on odd repetitions,
Functions-first on even repetitions. No analytical warmup, reset or query retry.
Database caches are retained; backend order rotates, without claiming identical
cold-start/cache state or independent confidence intervals.

```bash
python -m evaluation.sql.postgres_azure_cloud_replay start \
  --state-dir results/postgresql-azure-cloud \
  --cache cache-postgresql-azure --run-id moderate-100-2 \
  --workload moderate --count 100 --max-in-flight 2 --repetitions 3
```

This installs only the checksum-verified replay module on the existing driver
and runs it in the existing Python image with a read-only source mount. No VM
replacement, package rebuild, table reload or Function republish is performed.
Host source/latest-publication checks happen before submission. Guest checks
runtime/source/SQL identity references before each phase. Existing active or
unresolved jobs and duplicate run IDs block submission; preserve checkpoints.
A failed run does not automatically allow a new job until its outcome is inspected.

Status normally takes1–2 minutes and submits no SQL:

```bash
python -m evaluation.sql.postgres_azure_cloud_replay status \
  --state-dir results/postgresql-azure-cloud --run-id moderate-100-2
```

Wait for state=complete and completed_runs=6, then collect and summarize (1–3
minutes collection; local summary is instant). Do not collect JSON snapshots
mid-phase while they are being updated; use status for progress. Failed stopped
runs can also be collected for diagnosis, without replaying SQL.

```bash
python -m evaluation.sql.postgres_azure_cloud_replay collect \
  --state-dir results/postgresql-azure-cloud --run-id moderate-100-2
python -m evaluation.sql.postgres_azure_cloud_replay summarize \
  --state-dir results/postgresql-azure-cloud --run-id moderate-100-2
```

Raw Azure invocations, responses, queues, errors, phase observation windows,
runtime-image metadata, exact runner/settings hashes and validation are retained.
Provider duration/Flex billing and persistent database/driver cost remain a
separate collection step. No dollar rate or AWS billing formula is assumed.

After moderate succeeds, run the same start/status/collect/summarize lifecycle
with distinct run IDs and workload burst, first max-in-flight2, then8, keeping
count100/repetitions3. Each study is6 backend runs/600requests. Estimated burst
duration is15–30 minutes per study depending on current database throughput;
queueing is included, no unrestricted function scale-out claim. Do not start
another study while a run is active. SF10 indexes are confined to the separate
SF10 namespace and are not part of the SF1 paired default profile.


## Completed paired workloads and Flex cost evidence (24 November 2026)

All three paired SF1 studies are complete: three repetitions of 100 requests per
backend, fresh PostgreSQL connections, same private server and input fingerprint.
All 1,800 answers validated; zero failures. Latencies include driver queueing.

| Study | Direct mean / p95 (s) | Functions mean / p95 (s) |
|---|---:|---:|
| Moderate, 2 in flight | 3.014 / 7.483 | 3.231 / 7.521 |
| Burst, 2 in flight | 86.967 / 163.438 | 89.080 / 165.948 |
| Burst, 8 in flight | 88.649 / 166.286 | 89.461 / 167.635 |

This supersedes earlier pending paired-replay notes above. Burst concurrency 8
runs more simultaneous clients against the same database, not eight databases.
It did not improve mean latency. These capped studies do not measure unrestricted
serverless scaling. All raw responses and native invocation records are collected
under results/postgresql-azure-cloud/runs/RUN_ID/collected.

Collect native Flex usage and current resource/price evidence before cleanup
(typically 3–8 minutes; no SQL or cloud configuration changes):

```bash
python -m evaluation.sql.postgres_azure_costs collect \
  --state-dir results/postgresql-azure-cloud
```

Default run IDs are moderate-100-2, burst-100-2 and burst-100-8. The collector
requires complete local results and matching resources/input. It snapshots VM
and OS-disk metadata, owned SQL resource inventory and selected live Function
hosting fields. It does not read app settings, secret values or storage keys.

It reads OnDemandFunctionExecutionCount and OnDemandFunctionExecutionUnits,
plus Always Ready counters. MB-milliseconds / 1,024,000 gives native GB-seconds:
https://learn.microsoft.com/en-us/azure/azure-functions/monitor-functions-reference

Overlapping minute buckets for adjacent repetitions are merged within a workload
before summation. Windows of different workloads must not overlap. Meter execution
counts must match the expected requests per merged window. Missing/mismatched
usage produces unknown Function costs; retry collection later, never replay SQL.
Count alignment alone does not prove there were no unrelated app invocations.

Each collection is a separate timestamped cost-evidence directory. Regional USD
Azure Retail Prices API responses retain source URLs, effective dates, meter IDs
and units. Rate selection requires a recognized unit and unambiguous positive
rate effective no later than the earliest study. Flex On Demand prices never fall
back to legacy Consumption rates. Unknown prices remain unknown for inspection.

The generated cost-report.json/md covers measured-window PostgreSQL VM compute
in both paths, plus native Flex execution/request charges for the Functions path.
Driver VM compute is separate. This is a compute subtotal before allowances and
credits, not a full infrastructure bill. Disk capacity/transactions, IP/network,
Blob operations/storage, Key Vault, monitoring, setup/inter-run uptime, failed
attempts outside these windows and taxes are excluded. Disk/resource snapshots
are preserved for additional infrastructure costing. Do not compare this scope
as equivalent to the broader AWS PostgreSQL infrastructure report.

Recalculate offline from the latest evidence (normally under a second):

```bash
python -m evaluation.sql.postgres_azure_costs report \
  --state-dir results/postgresql-azure-cloud
```

Review missing meter/rate diagnostics before finalizing costs. Once metadata and
raw usage are preserved, cleanup can proceed using section 7; reports remain
recalculable offline. SQL group deletion also removes the owned comparison driver.
Verify removal of both VMs and their disks/IPs, native Function resources and owned
support resources; requesting deletion alone is not confirmation of completion.


### First billing collection: paid tiers and meter-window correction

The first live collection preserved regional retail prices and resource metadata,
but its narrow metric windows counted fewer Function executions than the 300
expected per workload. This does not change the 1,800 validated query results.
Function dollars remain unknown rather than being estimated from partial usage.

The collector now defaults to a five-minute margin before/after metric windows.
Adjacent windows within a workload are merged, preserving each expected request
once; overlap between different workloads is refused. Repeat the read-only
collection, not the SQL experiments. This tests whether metric time alignment
explains the missing counts; it does not assume the missing usage is zero.

The rate selector also now handles Azure's free-grant tier followed by a paid
tier. For this saved Sweden Central response, the positive On Demand Flex rates
are $0.000026/GB-second and $0.000004 per 10 executions ($0.40/million).
These come from saved regional API records, effective 2024-12-01; they are not
legacy Consumption rates. Reports use the first positive paid tier before free
allowances, retain its threshold/source metadata and reject ambiguous rates.
Ten offline tests pass, including paid/free/volume tiers and padded-window merging.
The locally saved 18 phase records produce three non-overlapping padded windows,
with 300 expected Function requests per workload. Live widened collection remains
pending. Missing/mismatched meters still produce unknown costs.


## Final billing and cleanup verification (24 November 2026)

The widened native meter collection succeeded: all three workloads matched 300
Function executions. Paid regional VM rate: $0.102/hour; Flex On Demand rates:
$0.000026/GB-second plus $0.40/million executions. Free allowances are not applied.

| Study | Native Function GB-s | Direct VM compute USD/100 | DB + Function compute USD/100 |
|---|---:|---:|---:|
| moderate-100-2 | 451.60 | 0.005666 | 0.009621 |
| burst-100-2 | 519.55 | 0.004865 | 0.009475 |
| burst-100-8 | 2002.50 | 0.004911 | 0.022326 |

These remain compute subtotals with the exclusions stated above. Raw regional
prices and meter responses are retained; no handler-time billing approximation
is used. No further queries are required for this planned evaluation.

Native Function deletion and SQL storage DELETE_COMPLETE were confirmed by user
output. Verify remaining recorded support resources (read-only; 1–3 minutes):

```bash
python -m evaluation.sql.postgres_azure_cleanup \
  --state-dir results/postgresql-azure-cloud \
  --storage-state-dir results/postgresql-azure-storage
```

The check uses recorded resource identities and the saved inventory, treats
lookup/permission errors as unknown, and writes a timestamped JSON report under
results/postgresql-azure-cloud/cleanup-verification. Parent-group absence proves
contained-resource absence. Other retained resources in checked groups are
reported separately and are not deleted. This is not an audit of the whole
subscription; soft-deleted Key Vault names are not purged.

The final results/limitations and separate native/evaluation commit manifests
are linked in handoff/AZURE_POSTGRESQL_HANDOFF.md. Keep all local result/state/cache
files private; only the sanitized summary in handoff/results belongs in the push.


Final cleanup verification on 25 November 2026 at 13:04:46 UTC confirmed all
recorded experiment resources absent and no other retained resources in the
checked groups. The remaining general SeBS storage account and its deployment
group were also deleted. Deployment cleanup is complete; unrelated Azure groups
and subscriptions are outside this verification's scope.
