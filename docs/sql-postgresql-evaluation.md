# Reproduce the AWS PostgreSQL evaluation

These helpers are optional evaluation tooling. Running `720.sql-postgresql` through
SeBS requires only the [native setup guide](sql-postgresql-aws.md). The comparison
uses driver → PostgreSQL and driver → Lambda → the same PostgreSQL database.
It measures the function boundary, connection/credential handling and shared
server capacity; it does not compare PostgreSQL with DuckDB or Athena.

The recorded study is complete and all recorded resources were removed on
2026-11-21. New reproduction needs fresh resources/state, not the deleted stack
IDs or the original cache. Use the same checkout on the host and cloud driver.
All commands run from the repository root with credentials in that terminal.
Generated JSON, state, packages and raw results remain under ignored `results/`.
Only aggregate evidence is published in the handoff.

## Reproduction order

1. Follow the native guide from dependency installation through a validated AWS
   smoke invocation. The normal backend loads data and configures Lambda networking.
2. Prepare and process the SF1 ten-request workload below.
3. Run comparison preflight, launch the separate driver, and wait for DRIVER_READY.
4. Run the short paired check, then moderate and burst workloads. Collect and
   summarize every completed run before deleting any driver or staging bucket.
5. Run the controlled cold/warm measurements. Connection-policy baselines and
   SF10 checks/treatments are separate experiments, described below.
6. Collect costs before cleanup. Generate the offline cost report, remove the
   driver, run native SeBS deployment cleanup, remove SQL storage, then verify.

Approximate durations: initial storage/bootstrap 5–15 min; SF1 input preparation
1–3 min; short paired check 1–3 min; moderate 100×3 paired 20–25 min; burst 100×3 paired
15–20 min for each concurrency setting on the recorded database; cold/warm 3 reps
10–15 min. Capacity, package downloads and data generation can change these times.
SF10 statements can reach their 120/240 s limits; do not treat them as smoke tests.

## Prepare the SF1 workload

After the native SF0.01 smoke succeeds, create the local SF1 configuration:

```bash
unset SEBS_SQL_SCALE_FACTOR
python - <<'CONFIG'
import json
from pathlib import Path
config=json.loads(Path("configs/sql-postgresql-aws.example.json").read_text())
config["experiments"]["sql-workload"]["input-size"]="small"
config["experiments"]["benchmark-inputs"]["720.sql-postgresql"].update(
    queries=["q6"],connection_policy="fresh",statement_timeout_ms=120000)
Path("configs/sql-postgresql-aws.json").write_text(json.dumps(config,indent=2)+"\n")
CONFIG

sebs experiment invoke sql-workload \
  --config configs/sql-postgresql-aws.json --deployment aws \
  --storage-configuration results/postgresql-aws-storage.json \
  --cache cache-postgresql-aws --output-dir results/postgresql-aws-sf1-workload --verbose

sebs experiment process sql-workload \
  --config configs/sql-postgresql-aws.json --deployment aws \
  --cache cache-postgresql-aws --output-dir results/postgresql-aws-sf1-workload
```

Require all ten responses valid and native billing present. This input is SF1;
the earlier `test` dataset is SF0.01. Input loading is outside query timing.

## Native connection-policy baselines

Each query/policy uses 31 sequential invocations. Exclude the earliest initial sample
from each group for the reported 30 warm samples; preserve it for billing. The
complete two-policy baseline took about 15 minutes on the recorded setup. Create
generated configs under results/ and run the ordinary SeBS commands:

```bash
python - <<'CONFIGS'
import json
from pathlib import Path
base=json.loads(Path("configs/sql-postgresql-aws.json").read_text())
for policy in ["fresh","reuse"]:
    folder=Path("results")/("postgresql-aws-sf1-baseline-"+policy)
    folder.mkdir(parents=True,exist_ok=True)
    for query in ["q6","q1","q12","q3","q21"]:
        config=json.loads(json.dumps(base))
        config["experiments"]["benchmark-inputs"]["720.sql-postgresql"].update(queries=[query],connection_policy=policy)
        (folder/(query+".json")).write_text(json.dumps(config,indent=2)+"\n")
CONFIGS

for policy in fresh reuse; do
  for query in q6 q1 q12 q3 q21; do
    sebs benchmark invoke 720.sql-postgresql small \
      --config "results/postgresql-aws-sf1-baseline-$policy/$query.json" \
      --deployment aws --architecture x64 --language python --language-version 3.12 \
      --trigger library --repetitions 31 --validate \
      --storage-configuration results/postgresql-aws-storage.json \
      --cache cache-postgresql-aws \
      --output-dir "results/postgresql-aws-sf1-baseline-$policy/$query" || break 2
    sebs benchmark process \
      --config "results/postgresql-aws-sf1-baseline-$policy/$query.json" \
      --deployment aws --cache cache-postgresql-aws \
      --output-dir "results/postgresql-aws-sf1-baseline-$policy/$query" || break 2
  done
done
```

Use new output directories for new trials; do not rerun a loop over partially
completed measured groups. The measured policies run sequentially, so cache/time
variation prevents attributing every timing difference to connection reuse alone.

## Main comparison and optional diagnostics

For the main comparison, start at [cloud-driver comparison preflight](#cloud-driver-comparison-preflight)
and continue through paired and larger workloads. Diagnostics, disk/index changes,
input recovery and driver refresh are optional; do not apply tuning to SF1 by
following the SF10 treatment examples. Replace namespace/fingerprint placeholders
with values from your storage state's dataset report. Never substitute a new
fingerprint to bypass an existing dataset identity mismatch.

## Read-only resource and plan diagnostics

After a slow or failed run, use the prepared dataset namespace and fingerprint
from its invocation input. The diagnostic uses the existing SQL storage SSM/S3
transport and private administrator file on EC2; it does not print credentials.

```bash
python -m evaluation.sql.postgres_diagnose \
  --storage-configuration results/postgresql-aws-storage.json \
  --output-dir results/postgresql-aws-sf10-check/diagnostics \
  --namespace YOUR_PREPARED_NAMESPACE \
  --fingerprint YOUR_PREPARED_INPUT_FINGERPRINT
```

This collects disk/memory/container snapshots, relation sizes, settings, cumulative
statistics and estimated plans for all five statements. EXPLAIN has no ANALYZE,
so the benchmark statements do not execute. Database data/configuration is unchanged.
The full report is saved locally, with a copy under the owned staging bucket's
reports/ prefix. Counters are cumulative and snapshots describe collection time;
they are not historical per-query resource measurements. The submission is
checkpointed. Repeating the same command retrieves/resumes it; use a new --label
for another snapshot. The default label is sf10-diagnostic-v1.

To retrieve historical CPU and EBS metrics for saved five-query runs without
replaying queries or changing monitoring settings:

```bash
python -m evaluation.sql.postgres_metrics \
  --storage-configuration results/postgresql-aws-storage.json \
  --run-dir results/postgresql-aws-sf10-check
```

The collector saves diagnostics/historical-metrics.json, including instance/disk
allocation, query observation windows, raw meter values and their periods/status.
Basic EC2 CPU monitoring has five-minute periods; EBS metrics use one-minute
periods. The retrieval window is padded to full periods and must not be treated
as exact per-query attribution. Missing metric points remain missing, not zero.
For a single-query treatment directory, add --queries q21. Optional --failure-logs
also saves existing CloudWatch exception/REPORT messages; neither option replays
SQL. The collector includes instance EBS credit/limit and volume limit metrics;
unsupported/absent values remain missing. This collector makes read-only
CloudFormation/EC2/CloudWatch API requests.

## Controlled disk-throughput treatment

For a storage-bound SF10 query, change throughput as a separate evaluation
condition while retaining the original measurements:

```bash
python -m evaluation.sql.postgres_volume apply \
  --storage-configuration results/postgresql-aws-storage.json \
  --state-dir results/postgresql-aws-sf10-disk500 \
  --throughput 500

python -m evaluation.sql.postgres_volume status \
  --storage-configuration results/postgresql-aws-storage.json \
  --state-dir results/postgresql-aws-sf10-disk500
```

Run the treatment only after ready_for_measurement is true (modification completed).
Optimizing is transitional. Preserve the original 125 MiB/s results. The helper
modifies only the owned database root gp3 volume's Throughput; volume size, IOPS,
CPU and memory are unchanged. It saves allocation/request/response/status under
the experiment state directory and disables automatic mutation retries. Repeating
apply inspects the saved operation; it does not resubmit. Extra provisioned
throughput is billable from modification start until restored or deleted.

Restore after the experiment and wait for completion, or delete the database
storage stack as part of final cleanup:

```bash
python -m evaluation.sql.postgres_volume restore \
  --storage-configuration results/postgresql-aws-storage.json \
  --state-dir results/postgresql-aws-sf10-disk500
```

This is an explicit live EBS evaluation treatment, not a CloudFormation instance
update. Do not recreate the SQL stack or resize the filesystem. Keep the state
ledger until restoration or final volume deletion is confirmed. Reproducing from
scratch uses normal SeBS storage start followed by this recorded treatment.

## Q21 covering-index treatment

For the separately recorded q21-covering-v1 profile:

```bash
python -m evaluation.sql.postgres_q21_indexes prepare \
  --storage-configuration results/postgresql-aws-storage.json \
  --state-dir results/postgresql-aws-sf10-q21-indexes \
  --namespace YOUR_PREPARED_NAMESPACE \
  --fingerprint YOUR_PREPARED_INPUT_FINGERPRINT
```

This adds lineitem(l_orderkey) INCLUDE(l_suppkey,l_receiptdate,l_commitdate) and
orders(o_orderkey) WHERE o_orderstatus='F' in the selected schema. It performs
standard VACUUM (ANALYZE), then EXPLAIN without ANALYZE. The benchmark SQL does not
execute during preparation. Build/maintenance time, index/table sizes, visibility
and the estimated plan are saved in prepare-report.json. Inspect that plan before
running Q21 again in a fresh output directory. Data values and their fingerprint
remain unchanged. This combined index/maintenance profile must be reported as a
separate condition, with its preparation cost/storage; it is not the primary-key-
only baseline. Hardware and SQL limits do not change.

Preparation is checkpointed through the existing SQL storage SSM/S3 transport.
Repeating an interrupted command retrieves/resumes its SSM operation; it does not
silently launch another preparation. A failed SSM operation retains its logs;
inspect the cause before starting a new explicitly named experiment state directory.
The helper refuses existing indexes without a matching ownership/definition record.

To remove only these owned secondary indexes:

```bash
python -m evaluation.sql.postgres_q21_indexes remove \
  --storage-configuration results/postgresql-aws-storage.json \
  --state-dir results/postgresql-aws-sf10-q21-indexes \
  --namespace YOUR_PREPARED_NAMESPACE \
  --fingerprint YOUR_PREPARED_INPUT_FINGERPRINT
```

Removal preserves table values and primary keys; it does not restore previous
cache contents or planner statistics. Final storage-stack deletion also removes
all indexes and data on its owned disk.

## Cloud-driver comparison preflight

To prepare a comparison of a cloud driver submitting directly to PostgreSQL versus
submitting the same SQL through Lambda, first verify the existing deployment:

```bash
python -m evaluation.sql.postgres_cloud_preflight \
  --storage-configuration results/postgresql-aws-storage.json \
  --trace results/postgresql-aws-sf1-workload/sql-workload/trace.json \
  --cache cache-postgresql-aws \
  --state-dir results/postgresql-aws-cloud
```

This requires the SF1 workload's invocations-processed.json and the exact cached
Lambda ZIP. It compares Lambda's CodeSha256 with that ZIP and checks its handler,
SQL wrapper and query sources against the checkout. It checks the private SQL
attachment, driver candidate/AMI/subnet and approximate regional Standard-family
vCPU quota headroom. No SQL, Lambda invocations or resource changes occur. The
saved preflight.json is a candidate plan, not a launch guarantee. Driver launch,
measurement and cleanup are separate subsequent steps.

## Launch the separate PostgreSQL comparison driver

After the preflight passes, run from the same checkout and credentialed terminal:

```bash
python -m evaluation.sql.postgres_cloud_driver launch \
  --state-dir results/postgresql-aws-cloud \
  --trace results/postgresql-aws-sf1-workload/sql-workload/trace.json

python -m evaluation.sql.postgres_cloud_driver status \
  --state-dir results/postgresql-aws-cloud
```

Launch creates one additional EC2 VM of the preflight-selected type, a 30 GiB gp3
root disk, an instance role and profile in a separate CloudFormation stack. These
resources consume AWS usage/credits. It uses the existing SQL client security group
with no inbound rules, the database subnet/availability zone, IMDSv2, and SSM for
management. The public IPv4 address supports outbound bootstrap/API calls; no SSH
or public database endpoint is opened.

The allowlisted source bundle excludes local credentials, caches, and result
directories. Three PostgreSQL benchmark sources are copied verbatim from the
verified Lambda ZIP. Python 3.12 and Psycopg 3.3.9 are checked on the Ubuntu driver;
Amazon Linux native libraries from the Lambda ZIP are not loaded there. The role
can read the exact SQL secret, invoke/check the exact Lambda, read its immutable
source artifact, write its report prefix, and communicate through SSM. It cannot
administer the database or change the secret. No AWS access keys go into user data.

Rerun status until it prints DRIVER_READY. CloudFormation creation can complete
before pip/bootstrap and SSM are ready. An initial status call may submit a
read-only SSM check; subsequent calls retrieve that check. Readiness verifies the
source/package version, Lambda code/memory, secret format, and a private TCP
connection to PostgreSQL. It executes no benchmark queries. If bootstrap fails,
the last readiness output includes the end of the bootstrap log; diagnose that
failure instead of submitting another VM.

launch-request.json freezes the stack name, client token and template before
CreateStack. A repeated launch inspects the owned stack instead of creating a
second VM. Preserve all state; do not delete the directory to retry an ambiguous
launch. Driver deployment is an evaluation helper; native benchmark deployment
continues to use SeBS. Replay is a separate explicit step after readiness and the
original 125 MiB/s database throughput restoration have completed. This initial
comparison uses SF1, whose physical schema is separate from the tuned SF10 schema.

Before final SQL storage cleanup, collect the driver's results and delete the
separate driver stack first:

```bash
python -m evaluation.sql.postgres_cloud_driver remove \
  --state-dir results/postgresql-aws-cloud

python -m evaluation.sql.postgres_cloud_driver remove \
  --state-dir results/postgresql-aws-cloud --delete-driver

python -m evaluation.sql.postgres_cloud_driver status \
  --state-dir results/postgresql-aws-cloud
```

The first command previews deletion. The second deletes only the driver stack,
including its disk, role and profile. Confirm the stack is absent. Driver source
artifacts and uploaded reports remain in the SQL stack's staging bucket until
that separate storage stack is removed; downloaded local results remain intact.

## First paired PostgreSQL cloud replay

After DRIVER_READY, with the baseline gp3 allocation at 125 MiB/s, submit the comparison:

```bash
python -m evaluation.sql.postgres_cloud_replay start \
  --state-dir results/postgresql-aws-cloud --run-id comparison-01
```

The default is two paired repetitions of the saved ten-request SF1 trace: 40
queries total, offsets 0,2,...,18 seconds per phase, max two requests in flight.
Odd repetitions run direct PostgreSQL then Lambda; even repetitions reverse the
order. Both use the same SF1 namespace/fingerprint, SQL, fresh connections and
statement limit. PostgreSQL's buffer/OS cache persists between phases. No forced
cold starts or warm-up queries are added; Lambda cold starts are observed and
recorded. This is a comparison check, not a statistically complete evaluation.

start verifies the live database disk is gp3/125 MiB/s/3000 IOPS with no pending
modification. It loads an existing local DuckDB TPC-H extension regardless of its
recorded HTTP/HTTPS installation origin; only a missing extension is installed
from HTTPS. It obtains official SF1 answers and ships those references; it does
not generate another database on the driver.
Every returned answer is validated using the existing benchmark's comparison and
tolerance logic, outside timed replay. Source hashes and the database allocation
are saved with the run. No VM or IAM policy changes are needed.

The runner update is compressed and sent through SSM, so the initially deployed
driver needs no second source download or broader S3 access. One systemd unit runs
the workload independently of the terminal/SSM timeout. Direct worker threads use
separate wrapper/credential caches and the exact deployed benchmark handler, so
a shared client lock does not accidentally serialize the direct path. The Lambda
adapter uses monotonic response timing, a single invocation attempt and native
REPORT parsing on failures as well as successes. Missing native billing remains
unknown rather than zero. Native Lambda usage is not the full comparison cost:
PostgreSQL, driver and infrastructure uptime must also be accounted for.

If an earlier attempt has already submitted queries and then failed in client
processing, preserve it and its billing. After fixing the cause, use a fresh run
ID (for example comparison-02) consistently in start/status/collect/summarize.
Do not erase a submission marker to reuse an old ID. The Lambda adapter trims
trailing report whitespace and preserves returned rows even when billing parsing
fails; its explicit native_report_parse_error and unknown meter values distinguish
that case from an actual function error.

Check progress without resubmitting:

```bash
python -m evaluation.sql.postgres_cloud_replay status \
  --state-dir results/postgresql-aws-cloud --run-id comparison-01
```

A status inspection may require a second call once its read-only SSM command
finishes. Wait for state=complete and completed_runs=4. A successful submission
command means the background unit was started, not that queries have finished.
If state=failed, inspect the log and preserved results; do not restart the same
run ID. start persists its submission marker before SendCommand because that API
has no idempotency token. Even an ambiguous response cannot cause an automatic
second workload submission. Inspect the remote unit before using another run ID.

Collect the saved JSON files and summarize them locally:

```bash
python -m evaluation.sql.postgres_cloud_replay collect \
  --state-dir results/postgresql-aws-cloud --run-id comparison-01

python -m evaluation.sql.postgres_cloud_replay summarize \
  --state-dir results/postgresql-aws-cloud --run-id comparison-01
```

The driver uploads checkpoints to its existing reports prefix after each phase
and at completion/failure. Collected files go under runs/comparison-01/collected;
comparison-summary.json is saved beside collected. Failed uploads can leave data
only on the driver: preserve that VM until all result files have been collected.
The summary checks identical traces, request counts, validity and completion. It
reports end-to-end latency/queueing, per-query SQL+fetch timings, cold starts and
native Lambda GB-seconds. It does not present a zero dollar cost for unpriced
infrastructure. Keep per-repetition and first-use/cold observations for analysis;
the paired mean alone does not isolate all cache effects or establish causality.

## Larger traffic workloads on the same PostgreSQL deployment

After the short paired comparison validates, run the moderate workload:

```bash
python -m evaluation.sql.postgres_cloud_replay start \
  --state-dir results/postgresql-aws-cloud --run-id moderate-100-2 \
  --workload moderate --count 100 --max-in-flight 2 --repetitions 3
```

This schedules 100 queries per path per repetition (600 total), one every two
seconds with a shared limit of two in flight. Each of the five queries appears
20 times per phase and 60 times per backend across three repetitions. Allow about
20-25 minutes: six phases each have a 198-second arrival horizon plus drainage.
The same existing server, dataset, SQL and fresh-connection policy are retained;
no new resources are created. Each phase is measured independently and backend
order alternates. Cold starts are observed; none are forced. Native SQL results
are validated after timed phases. Original comparison data is not overwritten.

Use the same status/collect/summarize commands with --run-id moderate-100-2. Wait
for state=complete and completed_runs=6. Do not resubmit start while it is running.
Each new experiment needs a fresh run ID. --workload burst gives equal arrival
offsets; --workload sparse spaces arrivals by 30 seconds. These are subsequent
experiments, not additional automatic work from the moderate command. Concurrency
can be changed explicitly for both paths using --max-in-flight; a larger value
can overload the shared database, which is itself a measured behavior.

Without --workload, start retains the saved trace (ten requests by default).
--count requires --workload; its default is 100 when a named workload is selected.
The saved prepared input and earlier traces are never edited to generate a run.
The workload here compares two paths to one external PostgreSQL instance; it
cannot establish scaling advantages for SQL computation inside independent Lambdas.
Keep query-level SQL/fetch times separate from schedule-relative queue/response
latency, and account for the always-running database in both paths' cost models.

### Measured burst configurations

After the moderate run is complete and collected, submit the two-slot burst:

```bash
python -m evaluation.sql.postgres_cloud_replay start \
  --state-dir results/postgresql-aws-cloud --run-id burst-100-2-ca-fixed \
  --workload burst --count 100 --max-in-flight 2 --repetitions 3
```

Use status, collect and summarize with that run ID; require complete/6 runs. Allow
15–20minutes. Then submit the eight-slot burst and repeat those collection steps:

```bash
python -m evaluation.sql.postgres_cloud_replay start \
  --state-dir results/postgresql-aws-cloud --run-id burst-100-8-ca-fixed \
  --workload burst --count 100 --max-in-flight 8 --repetitions 3
```

Allow 15–20 minutes. The suffix records that both published runs used the atomic
CA-publication fix already present in the checkout. Do not change deployed code,
SQL input or database allocation between these matched runs. No sparse PostgreSQL
run was performed in this study; `--workload sparse` is an optional future study
and is not needed to reproduce the published tables. It would keep PostgreSQL
provisioned on both paths, unlike the earlier DuckDB engine-placement comparison.

## Diagnose concurrent PostgreSQL connection failures

When the first burst requests fail quickly during setup, preserve the run and
inspect the collected request/response JSON before changing database limits.
The direct adapter retains the shared wrapper's redacted stage and exception
class. A separate connection-only probe can inspect credential/TLS setup:

```bash
python -m evaluation.sql.postgres_connections \
  --state-dir results/postgresql-aws-cloud \
  --label connections-8-01 --workers 8 --rounds 3
```

Allow about 1-2 minutes. This performs 24 credential and connection attempts on the
existing driver, using the exact deployed wrapper's CA file path. It issues no
benchmark SQL and changes no database settings or resources. It records failures,
redacts passwords in libpq messages, and saves connection-checks/LABEL/report.json.
Secrets reads and authentication connections still occur. A pending command can
be retrieved by repeating the same invocation: its saved SSM ID is resumed.
Use a new label only for an intentional additional diagnostic; do not clear state.
A successful probe does not prove an intermittent failure is absent; retain the
original failed measurements and inspect server logs if needed.

For historical server errors from a collected failed phase, use:

```bash
python -m evaluation.sql.postgres_connections \
  --state-dir results/postgresql-aws-cloud --server-run-id burst-100-8 \
  --storage-configuration results/postgresql-aws-storage.json
```

Allow about 1-2 minutes. This reads bounded Docker logs on the existing PostgreSQL
server and reports configured shared memory plus current /dev/shm usage. It runs
no SQL and changes no allocation. The historical error lines and post-run usage
snapshot are distinct evidence. Output is saved under runs/RUN/postgresql-server-errors.txt.

The shared SQL runtime publishes its CA through a unique mode-0600 temporary
file and an atomic replacement. Concurrent clients must never truncate the shared
published CA while another connection reads it. After a shared-runtime correction,
rebuild/update the native Lambda through SeBS and synchronize the cloud driver's
verified benchmark sources to that same deployed package before replay. Keep old
results under their original source hashes; use a fresh run ID after correction.

## Restore original measured inputs after local cache changes

A local /tmp Parquet cache can disappear or be regenerated. Its new physical byte
hashes may differ, and the SQL loader correctly refuses to replace the existing
namespace without --update-storage. For ongoing comparisons, restore the exact
original source artifacts rather than replacing the measured database:

```bash
python -m evaluation.sql.postgres_restore_inputs \
  --state-dir results/postgresql-aws-cloud \
  --storage-configuration results/postgresql-aws-storage.json
```

Allow about 2-5 minutes for the SF1 source downloads. This helper is deliberately
limited to the measured five-table SF1/small input. It verifies stack/bucket
ownership, the saved report and every original S3 content hash, reconstructs the
recorded dataset fingerprint, and only then publishes the local Parquet cache.
The previous local cache is retained under small.before-restore-...; matched bytes
can be reused without a download. It issues no SQL, invokes no Lambda and writes
no cloud object. Keep the original source objects until evaluation/cleanup ends.

After restoration, normal SeBS input preparation can match and reuse the existing
SQL dataset. Retry the code-update smoke with a fresh output directory, preserving
the failed attempt's logs. Do not delete failed SSM checkpoints or change recorded
fingerprints. Use --update-storage only for an intentional new dataset treatment,
with its changed identity and setup cost recorded separately.

## Synchronize an existing driver after a native package update

After a native SeBS code-update smoke passes, synchronize the existing driver to
that exact deployed ZIP before replay:

```bash
python -m evaluation.sql.postgres_cloud_refresh \
  --state-dir results/postgresql-aws-cloud \
  --storage-configuration results/postgresql-aws-storage.json \
  --cache cache-postgresql-aws --label ca-fix-01
```

Allow about 1-2 minutes. The tool checks package/source hashes and attachment,
updates the existing driver's frozen handler/queries/wrapper and checkout mirrors,
and preserves previous sources/profile. It refuses updates while a comparison is
running. Failed readiness restores the previous files/profile. The local active
profile changes only after remote confirmation and a fresh live Lambda hash check.
No benchmark SQL executes; no resource is created or resized. Repeat the same
command if pending to retrieve its existing SSM operation. An ambiguous submission
or failed operation must be inspected before creating another refresh label.

After success, run the connection-only probe under a new label, then replay using
a new run ID. Keep prior results associated with their original source hashes.
Do not run launch again to apply a source update; its original ownership/request
must remain intact for cleanup. A strict concurrency-only comparison uses the same
corrected source profile at each tested concurrency. Older results are still valid
observations under their recorded earlier profile.

## Controlled cold/warm measurements

Use the existing PostgreSQL deployment at SF1/512MiB with fresh connections:

```bash
python -m evaluation.sql.postgres_cold_warm run \
  --config configs/sql-postgresql-aws.example.json \
  --storage-configuration results/postgresql-aws-storage.json \
  --cache cache-postgresql-aws \
  --output-dir results/postgresql-aws-sf1-cold-warm-3 --repetitions 3

python -m evaluation.sql.postgres_cold_warm status \
  --output-dir results/postgresql-aws-sf1-cold-warm-3

python -m evaluation.sql.postgres_cold_warm summarize \
  --output-dir results/postgresql-aws-sf1-cold-warm-3
```

Allow 10–15 minutes and keep the run terminal open. Check status from another terminal;
wait for complete/five queries before summarizing. This gives3 cold and 3 warm retained
samples per query, 30 total, normally plus 10 initial samples. All attempts are saved,
validated and billed. PostgreSQL and its caches remain running while native SeBS
forces fresh Lambda environments. These samples characterize typical times, not
reliable p95/p99 distributions. The helper defaults to 10 reps if omitted; use 3 to
match the published study. An interrupted job is preserved and cannot silently
restart in the same output directory. Use an explicitly new directory for a new
trial. Native and in-function timing are used; WSL client latency is excluded.

## Cost evidence before cleanup

After collecting all query results, preserve the owned live resource metadata and
current public AWS price records before deleting the stacks:

```bash
python -m evaluation.sql.postgres_costs collect \
  --storage-configuration results/postgresql-aws-storage.json \
  --state-dir results/postgresql-aws-cloud \
  --output-dir results/postgresql-aws-costs
```

Allow 1-3 minutes with AWS credentials set. This uses only read APIs: the database
and driver CloudFormation resource/event histories, EC2 allocations and volumes,
private endpoint metadata, DescribeSecret (never GetSecretValue), selected Lambda
configuration fields, and the AWS Price List Query API. No SQL, SSM command or
cloud mutation is submitted. Pricing permissions include pricing:GetProducts.
The helper currently supports the us-east-1 study only.

Each call saves a new timestamped snapshot, an inventory.json, raw pricing/*.json,
pricing-status.json, and an output-directory latest.json pointer. Missing pricing
is reported as unresolved, never priced at zero. This collection does not produce
a dollar report. Retain it with the raw experiment files and the recorded gp3
apply/restore timestamps. The public prices are current catalog prices, not an
invoice or proof of prices at an earlier experiment date. EC2 LaunchTime is not
proof of continuous uptime when instances have been stopped/restarted.

Cost reporting must distinguish database allocation during each full workload
window (on both paths), additional Lambda usage and requests, driver measurement
costs, and the campaign's full setup/idle/failure/maintenance costs. Include disks,
temporary throughput, private endpoints, public IPv4 and secrets; disclose any
unmeasured storage/request/log/transfer usage. Preserve interruption attempts
separately from successful experimental samples. Do not equate a difference in
Lambda GB-seconds with a difference in total system cost.


### Offline allocation-cost report

After the collection succeeds, calculate the report without any AWS calls:

```bash
python -m evaluation.sql.postgres_costs report \
  --snapshot results/postgresql-aws-costs/20261121T154918304383Z \
  --state-dir results/postgresql-aws-cloud \
  --results-dir results \
  --throughput-state results/postgresql-aws-sf10-disk500
```

Use your own timestamped snapshot path (or omit --snapshot to use the collector's
latest.json pointer). Allow under one minute. Outputs in the snapshot directory:
cost-report.json, cost-report.md and known-lambda-ledger.json.

The report selects unambiguous current paid OnDemand prices, preserving the SKU,
unit, effective date and rate code. It prices the three main saved paired runs:
moderate-100-2, burst-100-2-ca-fixed and burst-100-8-ca-fixed. Every phase must be
complete, valid, have the same input/schedule and complete native Lambda billing.
The database is charged for the full observation window on both paths, using the
recorded run allocation. Shared endpoint/secret allocation is amortized over that
window, not billed as a new minimum-hour resource for each phase. Driver costs
are shown separately. gp3 throughput catalog GiBps-month units are converted to
MiB/s allocation units; baseline 3000 IOPS/125 MiB/s is not charged twice.

A separate infrastructure model extends through the inventory capture timestamp,
including the recorded 125-to-500-to-125 MiB/s treatment. It assumes continuous
compute/current IPv4 allocation since the latest LaunchTime; it is not proof of
historical uptime or an invoice. Resource metadata and cleanup confirmation times
must be retained if reconciling the actual account bill later. Endpoint partial
hours are rounded up in this campaign model, once across its lifetime. Monthly
allocation is prorated using a declared 30-day month (these study windows are in
November). Current catalog dates may differ from historical resource dates.

The Lambda ledger deduplicates AWS request IDs across raw/processed native files,
warm-up attempt records, saved cloud responses and recovered failed-trial reports.
It flags missing native meters and interrupted jobs. This gives known saved usage;
unsaved calls may be absent. Never describe it as a complete campaign invoice.
Do not add workload-window subtotals to the uptime model: those costs overlap.
Credits/free grants/discounts and unmetered API/storage/log/transfer costs are not
applied. This is an allocation-cost evaluation with explicit omissions.

Billing references: [Lambda](https://aws.amazon.com/lambda/pricing/),
[EBS](https://aws.amazon.com/ebs/pricing/),
[PrivateLink](https://aws.amazon.com/privatelink/pricing/),
[Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/).


### Final logs and recorded-resource verification

Function-only cleanup removes the function from the SeBS cache before a later
full cleanup can discover its log group. After driver/SQL stack deletion and
normal SeBS deployment cleanup, finish this exact recorded deployment with:

```bash
python -m evaluation.sql.postgres_cleanup finish \
  --snapshot results/postgresql-aws-costs/20261121T154918304383Z \
  --output-dir results/postgresql-cleanup-final
```

Use the cost inventory snapshot recorded for your deployment. Allow 1–3 minutes.
This checks the AWS account against the inventory, requires the recorded Lambda
to be absent, archives only native REPORT billing lines from its exact log group,
and saves an archive checksum/group identity before deleting that group. A failed
archive or identity mismatch prevents deletion. It then checks the recorded stack
IDs, EC2 instance IDs, disk IDs, endpoint, secret, Lambda, benchmark bucket and
log group. No SQL or secret-value retrieval. No other resource is deleted by
this helper. Access failures/remaining resources cannot produce a successful
verification. It does not scan or delete other experiments/account resources.

Keep lambda-billing-reports.json, archive-complete.json, log-deletion.json and
verification.json alongside your results. All checked experiment resources must
report absent (terminated instances/deleted stacks count as removed). A secret
still scheduled for deletion is reported separately and keeps verification open.
To repeat only the read-only checks, use postgres_cleanup verify with the same
arguments. Known-request billing reconciliation can use the archived REPORT lines;
these may contain previously unsaved warm-up/interrupted-call billing records.
The original cost snapshot/report ends at its earlier capture time, not cleanup.

## Export aggregate evidence

After all primary result files, costs and final verification are saved:

```bash
python -m evaluation.sql.postgres_export_results \
  --snapshot results/postgresql-aws-costs/20261121T154918304383Z \
  --output handoff/results/postgresql-aws-summary.json
```

Use your snapshot path for a new reproduction. Allow under one minute; offline only.
The exporter whitelists aggregate result fields and excludes raw request IDs,
accounts, resource identities, network addresses, dataset manifests and local
state. Its source hashes identify the exported checkout files. It rejects an
incomplete primary workload/native baseline/cleanup verification. The SF10 failure
and tuning observations remain separately described in the handoff.

## Cleanup order

Collect all cloud driver results and cost evidence first. Remove the separate
driver stack using its remove preview, then --delete-driver, then confirm absence.
Follow the [native guide cleanup](sql-postgresql-aws.md#cleanup) for the dedicated
SeBS deployment and PostgreSQL storage stack. Full SeBS cleanup in one pass removes
cached function logs too; function-only cleanup followed by a later full cleanup
can leave the old log group. The final finish helper above archives billing and
removes only that exact group, then checks recorded resources. Keep all local
state until verification reports all checked experiment resources removed.
