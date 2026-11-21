# PostgreSQL benchmark handoff — 2026-11-21

## Current status

`720.sql-postgresql` is integrated into SeBS. Local Docker and AWS Lambda-to-EC2
PostgreSQL have been exercised end to end. SF1 baselines, paired moderate/burst
workloads, controlled cold/warm measurements and a cost model are complete for
the recorded scope. SF10 includes timeouts and a separately tuned Q21 observation.
No sparse PostgreSQL workload or general distributed query engine was implemented.

**Cleanup verified at 2026-11-21 17:13:15 UTC:** both stacks, both EC2 instances,
their disks, private endpoint, secret, Lambda, benchmark bucket and Lambda log group
were absent/removed. The final verifier checked 12 recorded resources and archived
1,611 native billing reports locally. This covers this PostgreSQL experiment;
other deployments/account resources were not checked. Reproduction needs fresh
cloud resources and state. Local raw results/cache/state were retained.

- Native benchmark setup from zero: [setup guide](../docs/sql-postgresql-aws.md).
- Optional comparison, diagnostics, costs and cleanup helpers: [evaluation guide](../docs/sql-postgresql-evaluation.md).
- Aggregate evidence without cloud identities: [results JSON](results/postgresql-aws-summary.json).
- Commit preparation: [push instructions](POSTGRESQL_PUSH.md).

## What belongs to SeBS

The benchmark selects one complete query, submits it to external PostgreSQL,
fetches rows and returns results. Q1/Q3/Q6/Q12/Q21 use the same SQL as
`710.sql-query`. DuckDB generates/validates TPC-H inputs on the host; PostgreSQL
performs the timed SQL computation. `710.sql-query` instead computes inside the
function over Parquet. These are different benchmark architectures.

| Concern | Native integration |
|---|---|
| Storage | `sebs storage start/status/stop/remove sql`; Docker or AWS EC2 provider |
| Attachment | Standard `--storage-configuration`, serializable config and cache |
| Declaration | `modules: ["sql"]`, `allocate_sql()` and generic table/key metadata |
| Preparation | Generate TPC-H, verify file hashes, transactional loading, primary keys, ANALYZE |
| Runtime | Shared SQL wrapper; fresh/reused connection, cached credentials, verified TLS |
| Deployment | Normal SeBS build, dependency packaging, source hashing, function cache and adapters |
| Experiments | Normal benchmark invoke/process/statistics, sql-workload and perf-cost |
| Validation | Existing query reference answers outside timed replay |

The generic storage/loader does not contain TPC-H query text. Schemas and content
fingerprints isolate datasets; changed input within the same identity is rejected
unless an explicit replacement is requested. No provisioning/loading happens in
the timed function. AWS roles, private networking and credential references come
from the storage backend. The database role used by the function is read-only.
The native benchmark/storage implementation does not import evaluation tooling.

Framework fixes retained: SQL module packaging/hashing and config injection;
local SQL-only deployment without an object store; accurate local handler timing;
AWS private-function role/VPC settings; monotonic SDK invocation timing with no
automatic retries; reuse of native REPORT metrics with bounded CloudWatch fallback;
atomic TLS CA publication for concurrent clients; optional SDK perf-cost trigger,
complete attempt accounting and bounded/fail-fast sampling. Existing perf-cost
HTTP/default sampling behavior is preserved when the new settings are omitted.

Other providers have configuration hooks but no validated PostgreSQL deployment
path. Do not label Azure/GCP external-PostgreSQL support production-ready.

## Recorded allocation and interpretation

PostgreSQL 16.16/bookworm, Psycopg 3.3.9; database host m7i-flex.large, 2 vCPUs / 8 GiB;
container cap 2 CPUs / 6144 MiB; 64 GiB gp3, 3000 IOPS and 125 MiB/s baseline. Lambda used
Python 3.12/x86-64, 512 MiB. Driver was another m7i-flex.large in the same AZ/subnet.
Flex CPU availability can vary. Database/OS caches persisted between phases.

The driver submits directly to PostgreSQL or invokes Lambda, which submits the
same query to the same PostgreSQL server. Admission concurrency is capped at 2 or 8;
queued requests wait in the driver. End-to-end numbers below include that wait.
They are not a measurement of unlimited Lambda scale or isolated database CPU.

## SF1 connection-policy baseline

31 sequential invocations per query/policy; earliest initial sample excluded from
statistics, retained for billing. 30 warm samples per cell,300 retained overall.
All answers validated; reuse flags confirmed reuse. Policies were measured in
sequence, so cache/time variation can contribute to total timing differences.

| Query | Fresh AWS execution mean | Reuse AWS execution mean |
|---|---:|---:|
| Q6 | 0.557445s | 0.505743s |
| Q1 | 4.332096s | 4.260205s |
| Q12 | 1.132403s | 1.077837s |
| Q3 | 0.700114s | 0.619073s |
| Q21 | 0.935485s | 0.867315s |

Fresh connection setup averaged 21.7–22.9 ms; reused setup about 0.007 ms. WSL client
latencies are excluded from end-to-end conclusions; use cloud-driver evidence.

## Main paired SF1 workloads

Each row pools 3 runs of 100 requests per backend. Moderate spacing is 2 s; burst
requests all arrive at 0 s. Query cycle Q6, Q1, Q12, Q3, Q21. Connections are fresh.
All 1,800 requests in these three paired studies validated, with no execution failures.
Both corrected bursts use the same runtime sources and database/input profile.

| Workload | Path | Mean | Median | p95 | Mean admission queue |
|---|---|---:|---:|---:|---:|
| Moderate, 2 slots | Direct PostgreSQL | 2.706s | 2.170s | 6.849s | 0.138s |
| Moderate, 2 slots | Lambda→PostgreSQL | 2.734s | 2.240s | 6.772s | 0.162s |
| Burst, 2 slots | Direct PostgreSQL | 80.762s | 80.311s | 152.258s | 77.585s |
| Burst, 2 slots | Lambda→PostgreSQL | 80.790s | 80.783s | 151.529s | 77.630s |
| Burst, 8 slots | Direct PostgreSQL | 82.779s | 82.389s | 155.236s | 70.188s |
| Burst, 8 slots | Lambda→PostgreSQL | 84.184s | 84.580s | 157.912s | 71.456s |

| Workload | Lambda cold starts | Lambda billed GB-seconds |
|---|---:|---:|
| Moderate, 2 slots | 2 | 382.959 |
| Burst, 2 slots | 1 | 471.261 |
| Burst, 8 slots | 9 | 1904.904 |

Eight slots gave no useful completion-time improvement and about 4.04× Lambda
billed usage compared with the matched two-slot burst. Requests spent less time
waiting for admission but more time after dispatch. This supports a shared server
capacity limit; latency alone does not identify CPU versus I/O as the bottleneck.
Medians/p95 are descriptive pooled values, not independent confidence intervals.

## Controlled cold/warm SF1

Fresh connections, 512 MiB; 3 cold and 3 warm samples per query, 30 retained. PostgreSQL
stayed running while SeBS forced fresh Lambda environments. All recorded answers
validated. All attempts including initial samples consumed 54.633 GB-seconds.

| Query | Cold AWS execution | Warm AWS execution | Cold init | SQL cold/warm |
|---|---:|---:|---:|---:|
| Q6 | 2.764s | 0.580s | 77.0ms | 0.538/0.553s |
| Q1 | 6.651s | 4.514s | 80.0ms | 4.455/4.480s |
| Q12 | 3.464s | 1.165s | 93.7ms | 1.163/1.137s |
| Q3 | 2.905s | 0.724s | 81.3ms | 0.707/0.692s |
| Q21 | 3.170s | 0.967s | 78.7ms | 0.943/0.942s |

Initialization is separate from AWS execution. Cold execution added about 2.2 s;
credential lookup accounted for 1.08–1.15 s and connection setup 144–150 ms, versus
20–29ms connect when warm. The reported components do not explain every remaining
millisecond. Warm does not imply a reused connection under the fresh policy.
Three samples per condition characterize typical times, not reliable tail latency.
The interrupted earlier ten-repetition job remains a separate failed trial.

## SF10 limits and separate Q21 treatment

These are individual observations, not a replicated SF10 performance distribution.
Default primary-key schema and 120 s statement limit: Q1, Q12, Q3 validated with AWS
execution 113.331 s, 116.049 s, 108.936 s; Q6/Q21 reached the statement deadline. At a
later 240 s limit, Q6 validated at 59.379 s AWS / 57.277 s SQL, while Q21 still timed out.
The later Q6 result reflects run variation, not a speedup caused by raising a limit.

Increasing gp3 throughput alone from 125 to 500 MiB/s did not complete Q21 within 240 s.
A separate covering-index/VACUUM/ANALYZE treatment then completed Q21 with the same
SQL/data at 65.372 s SQL, 67.519 s AWS, 33.794 GB-seconds. It used 500 MiB/s during that run.
Index preparation took 68.441 s and added about 2.42 GiB. Original timed-out, billed
runs remain separate; censored timeouts do not establish an exact speedup ratio.
The disk was restored to 125 MiB/s before the paired SF1 studies; indexes were only
on the SF10 schema. They are not benchmark defaults.

## Costs

Public USD list-price allocation model, no credits, free tiers or discounts.
Database compute/disk/IPv4 and amortized endpoint/secret allocation are included
on both paths. Lambda adds native compute and requests. Driver instrumentation
is separate. The report includes rate units, SKU/effective dates and exclusions.

| Workload | Direct PostgreSQL USD/100 | Lambda→PostgreSQL USD/100 |
|---|---:|---:|
| Moderate, 2 slots | 0.006562 | 0.008710 |
| Burst, 2 slots | 0.005247 | 0.007855 |
| Burst, 8 slots | 0.005336 | 0.015995 |

A separate conditional infrastructure model was 17.1532 USD through the
2026-11-21 15:49:18 UTC inventory snapshot. It assumes continuous compute/current
IPv4 allocation since saved latest launch times, current disks and the recorded
throughput treatment. It is not a final cleanup total or a reconciled invoice.
Known saved Lambda billing: 1,611 unique requests, 4,185.5975 GB-seconds, 0.070082 USD;
coverage remains explicitly incomplete as campaign accounting. Do not add workload
allocation costs to the campaign uptime model; they overlap.

Unmetered/excluded: Secrets API calls, endpoint data, S3 storage/requests, logs/
monitoring, network transfer, taxes and unrelated account resources. Monthly
allocation uses a declared 30-day month; the recorded windows are in November.
This report does not establish exact account charges.

Unlike the DuckDB Lambda-versus-EC2 engine-placement study, a Lambda frontend
leaves PostgreSQL provisioned on both paths. Sparse requests cannot remove its
idle database cost. No sparse PostgreSQL traffic study was performed. Direct
client here is the driver, not an application VM that Lambda replaces.

## Corrections and preserved failed trials

- Initial paired Lambda responses were lost to a client REPORT-parser error.
  Native logs showed completed invocations. The parser now trims whitespace and
  preserves returned rows if meter parsing fails; failed-trial billing is retained.
- Two concurrent direct Q12 requests failed during the original eight-slot burst.
  A non-atomic shared CA file could be read at zero bytes. Atomic publication now
  prevents partial reads; the 24-connection probe and 1,200 matched burst requests
  passed after the fix. Original failed runs remain in raw results.
- Changed local Parquet bytes triggered the loader's fingerprint guard during a
  runtime redeploy. Original frozen objects were restored, not relabeled/replaced.
- Interrupted calls are not retried automatically. Sampling and SSM submissions
  retain checkpoints so an ambiguous response cannot silently repeat SQL.

## Validation and review scope

Latest offline checks: 63 SeBS/storage/runtime/SDK/metrics/perf-cost tests passed;
80 evaluation tests passed with 8 opt-in Docker tests skipped. Two aggregate-export checks passed for identity redaction and refusing incomplete
cleanup; 14 shared evaluation client/replay checks also passed.
Earlier recorded real local Docker checks passed all 8 tests, including actual
function build/deployment/invocation, all five answers, reuse and cleanup. Docker
checks were not rerun during commit preparation; no new AWS runs or Git commands
were executed by the agent. No runtime/database allocation was changed for push.

Commit 1 contains native SeBS integration, generic framework fixes/tests, examples,
setup documentation and this aggregate handoff/results artifact. Commit 2 contains
optional AWS experiment helpers/tests and their reproduction guide. Detailed
resource state, credentials, generated configs, datasets, archives and raw results
remain ignored. The commit lists are file scopes, not a Git diff; review staged
hunks because existing shared files can contain other contributors' changes.

Remaining follow-ups are optional: more repetitions, a controlled sparse study,
validated PostgreSQL deployment on other providers, stronger resource bottleneck
attribution, and actual-invoice reconciliation. None requires keeping this deleted
AWS deployment alive. Do not resume old launch state as if resources still exist.
