# Azure PostgreSQL benchmark handoff

Native SeBS benchmark `720.sql-postgresql` now runs on AWS and Azure. Python
functions use Psycopg to submit fixed TPC-H SQL to a private, self-hosted
PostgreSQL server. PostgreSQL executes the queries; DuckDB generates/reads input
and supports reference validation before/after measurements.

For installation, credentials, deployment and experiment commands, use
[the setup guide](../docs/sql-postgresql-azure.md). Sanitized results are in
[azure-postgresql-summary.json](results/azure-postgresql-summary.json).
Keep raw results, storage attachments and deployment caches local.

## Configuration and integration

- Sweden Central. PostgreSQL VM: Standard_D2s_v5, 2 vCPU, 8 GiB; Docker limit
  2 CPUs/6 GiB. OS disk: 64 GiB StandardSSD_LRS. Database persists between calls.
- Azure Functions Flex Consumption: Python 3.11, 512 MiB, zero always-ready
  instances. Psycopg 3.3.9. Private networking, verified TLS and read-only DB role.
- Separate regional Standard_D2s_v5 driver, Python 3.11.18/Psycopg 3.3.9.
  Driver uses the verified native Function SQL source package and database identity.
- Native SeBS storage lifecycle, input loading, function packaging/deployment,
  invocation/validation, processing and sql-workload are integrated.
- Evaluation helpers under evaluation/sql add controlled baselines, cold/warm,
  SF10 diagnostics/index treatments, regional paired replay, billing and cleanup
  verification. Shared AWS evaluation primitives remain dependencies; these helpers
  are not replacements for the native storage/benchmark implementation.

## Completed measurements

SF1 baselines: 31 sequential attempts per query/policy, initial sample excluded
from the 30 post-initial samples. Warm counts vary because Azure may use different
workers. Warm function means:

| Query | Fresh connection (s) | Reuse policy (s) |
|---|---:|---:|
| Q6 | 0.601 | 0.548 |
| Q1 | 4.876 | 4.795 |
| Q12 | 1.235 | 1.177 |
| Q3 | 0.752 | 0.698 |
| Q21 | 1.017 | 0.951 |

Policies were run sequentially; database/OS caches and time can confound this
comparison. Reuse policy does not guarantee that every invocation reuses a socket.

Controlled SF1: 3 retained cold and 3 retained warm observations per query, 50
invocation attempts in total. All retained answers validated. Function means:

| Query | Cold (s) | Warm (s) |
|---|---:|---:|
| Q6 | 1.655 | 0.616 |
| Q1 | 5.658 | 4.893 |
| Q12 | 2.153 | 1.270 |
| Q3 | 1.512 | 0.774 |
| Q21 | 1.753 | 1.038 |

These are handler times, not complete platform startup latency or billing units.
Cold credential retrieval averaged about 445–795 ms; SQL times changed little.

SF10: all five queries eventually produced validated answers at a separately
recorded 240-second SQL limit. Single observations, not repeated distributions:

| Query | SQL (s) | Handler (s) | Treatment |
|---|---:|---:|---|
| Q6 | 115.714 | 116.329 | Default indexes |
| Q1 | 132.367 | 132.956 | Default indexes |
| Q12 | 181.080 | 181.631 | Default indexes |
| Q3 | 166.682 | 167.249 | Default indexes |
| Q21 | 170.250 | 170.851 | Two additional indexes |

Original 120-second failures and failed Q21 240-second attempt remain preserved.
The Q21 treatment adds a covering lineitem(l_orderkey) index including supplier
and date columns, and a partial orders(o_orderkey) index for status F, followed
by VACUUM ANALYZE. SQL/data values did not change. Preparation took 377.572 s and
added 2,596,061,184 bytes (about 2.42 GiB); both are outside handler/query timing.
Long HTTP calls used independent Blob receipts; this overhead is outside the SQL
handler timing. No native Flex billing is inferred for those single observations.

## Paired regional workloads

Same SF1 data, fresh connections, queries cycling Q6/Q1/Q12/Q3/Q21. Each study
has 3 repetitions x 100 requests per backend; order alternates across repetitions.
All 1,800 answers validated; zero failures. No automatic warmup or SQL retries.

| Study | Direct mean / p95 / queue (s) | Functions mean / p95 / queue (s) |
|---|---:|---:|
| Moderate, 2 in flight | 3.014 / 7.483 / 0.203 | 3.231 / 7.521 / 0.267 |
| Burst, 2 in flight | 86.967 / 163.438 / 83.549 | 89.080 / 165.948 / 85.614 |
| Burst, 8 in flight | 88.649 / 166.286 / 75.222 | 89.461 / 167.635 / 76.002 |

Total latency is scheduled arrival to returned answer, including driver queueing.
Burst arrivals are simultaneous, but the driver submits only 2 or 8 requests at
once; the rest wait locally. More simultaneous clients did not improve mean
latency against the shared database. This does not measure unrestricted Function
scaling. No sparse PostgreSQL experiment was run on AWS or Azure.

## Cost evidence

Native Flex counts match all 300 Function requests per study. OnDemand execution
units are divided by 1,024,000 to convert MB-milliseconds to GB-seconds. Five-minute
meter margins and merging overlapping windows avoid counting buckets twice.
Attribution assumes no unrelated Function invocations in these windows.

Regional Azure Retail Prices API records verify VM $0.102/hour, Flex On Demand
$0.000026/GB-second and $0.40/million executions. Raw response URLs, effective
dates, units and paid-tier thresholds are preserved. These are paid public rates
before free grants/credits, not the older classic Consumption plan rates.

| Study | Function GB-s (300 requests) | Direct compute USD/100 | DB + Function compute USD/100 |
|---|---:|---:|---:|
| Moderate, 2 | 451.60 | 0.005666 | 0.009621 |
| Burst, 2 | 519.55 | 0.004865 | 0.009475 |
| Burst, 8 | 2002.50 | 0.004911 | 0.022326 |

Database VM compute is allocated over each path's complete observation window.
Driver compute is reported separately. Disk, IP/network, Blob, Key Vault,
monitoring, setup/inter-run uptime, other attempts and taxes are excluded. This
is a compute subtotal, not a full invoice or the broader AWS PostgreSQL cost scope.
Burst-8 used about 3.85x the Function GB-seconds of burst-2 without lower latency.

Reports/evidence are local under results/postgresql-azure-cloud/cost-evidence.
They can be recalculated offline after deletion. Initial incomplete collections
are retained; the latest successful collection has no missing meters/rate errors.

## Cleanup and push preparation

User confirmed native Function deletion and SQL storage DELETE_COMPLETE. This
confirms SQL group removal, including the database/driver VMs and contained disks.
The final user-run read-only verification at 2026-11-25 13:04:46 UTC confirmed
all recorded experiment resources absent and no other retained resources in the
checked groups. The general SeBS storage account and its deployment group were
also removed after the initial 42-resource check. Cleanup is complete for this
deployment; this is not an audit of unrelated Azure groups or the final bill.
Repeat verification with:

```bash
python -m evaluation.sql.postgres_azure_cleanup \
  --state-dir results/postgresql-azure-cloud \
  --storage-state-dir results/postgresql-azure-storage
```

Allow 1–3 minutes. This is read-only, records exact-resource/parent-group absence
and reports other retained resources in checked groups separately. It does not
delete those resources, purge Key Vault names or audit the entire Azure account.
Do not claim account-wide zero charges from this deployment-specific check.

Keep two commits, as requested:

1. Native integration: paths in AZURE_POSTGRESQL_COMMIT_FILES.txt.
2. Evaluation helpers/tests and sanitized results: paths in
   AZURE_POSTGRESQL_EXPERIMENT_FILES.txt.

No credentials, local configs, raw result directories, deployment templates,
state/cache files, ZIPs or SAS URLs belong in either commit. The manifest lists
are preparation aids; inspect the actual staged changes before committing.
No Git commands have been run by the assistant.
