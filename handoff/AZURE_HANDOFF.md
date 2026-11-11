# SQL benchmark: Azure handoff

Status: **11 November 2026**. The full SF1 traffic comparison is complete across
Azure Functions, VM/DuckDB and Synapse serverless SQL: **24 runs, 2,400/2,400 valid
answers, zero failures**. Cleanup is **not confirmed**. SF1/SF10 controlled
cold/warm tests are configured but have not been run on Azure.

For setup from zero, authentication, exact deployment/run/collection commands,
troubleshooting and cleanup, follow [Run the SQL comparison on Azure](../docs/sql-azure-setup.md).
Earlier AWS work is in [AWS_HANDOFF.md](AWS_HANDOFF.md). This document records the
completed implementation, evidence and remaining work; do not treat the pending
configs as completed experiments.

## Completed implementation

- Deployed `710.sql-query` through normal SeBS Azure package build/deployment,
  using Python 3.11/x64, Functions runtime v4 and classic Linux Consumption.
- Added native DuckDB Azure access with packaged extensions, curl TLS transport
  and `az://` Parquet paths. SeBS supplies storage credentials outside the input
  payload. Existing S3/httpfs paths remain supported.
- Prepared 20 SF1 Parquet objects through the benchmark input generator/upload
  path and reused the same saved manifest across all three backends.
- Provisioned a separate persistent SQL VM and Azure cloud driver, each
  Standard_D2s_v5 (2 vCPU, 8 GiB), ordinary Ubuntu 24.04 Gen2. The verified image
  version was `Canonical:ubuntu-24_04-lts:server:24.04.202610250`.
- Configured private authenticated HTTP between driver and SQL VM, direct
  Functions HTTPS calls and encrypted/certificate-verified ODBC to Synapse.
  Azure Run Command manages VMs; no SSH or benchmark-data tunnel is used.
- Created a Synapse workspace with its built-in serverless SQL endpoint and five
  views over the same Blob data. No dedicated SQL/Spark pool. Workspace default
  ADLS storage is separate from measured input storage. Synapse read/list access
  uses a 48-hour container SAS; SQL access is restricted to the driver IP.
- Reused the shared SeBS request scheduler and validator. Full jobs run in the
  background, rotate backend order, checkpoint/upload per backend run, reject
  duplicate output/submissions and prevent overlapping new jobs on the driver.
- Collected native Function execution units/count and Synapse processed-data
  counters, preserved public price records, and generated a cost evaluation.

The SQL VM uses two workers, two DuckDB threads and a 1536 MB limit per connection.
Connections are fresh and DuckDB external file cache is disabled. This is not a
total process memory cap. Functions reported four DuckDB threads and a 2320 MB
host memory cap; process peak RSS ranged 198–410 MB. Host cap/peak RSS are not
Azure billing allocation. Native observed-consumption meters determine the estimate.

Each traffic request selects **one complete query**, not one of five separately
deployed functions. No SQL query is split across workers/Lambdas in this study.

## Measured workloads

TPC-H SF1 (`small`), DuckDB 2.0.0, Sweden Central. Query order Q6/Q1/Q12/Q3/Q21,
twenty of each per 100-request run. Maximum two requests in flight on every
backend. Full results are returned; validation happens after timing. There is no
analytical warmup, forced cold starts, cold filtering or automatic query retry.

| Pattern | Spacing | Repetitions/backend | Queries/backend |
|---|---:|---:|---:|
| Moderate | 2 seconds | 3 | 300 |
| Burst | All at time zero | 3 | 300 |
| Sparse | 30 seconds | 2 | 200 |

Arrival-to-result seconds, including driver queue; p95 is nearest rank:

| Pattern | Backend | Mean | Median | p95 | Mean queue | Mean run duration |
|---|---|---:|---:|---:|---:|---:|
| Moderate | VM | 0.585 | 0.494 | 0.965 | 0.000 | 198.96 |
| Moderate | Functions | 0.843 | 0.706 | 1.493 | 0.000 | 199.48 |
| Moderate | Synapse | 1.466 | 1.191 | 2.458 | 0.040 | 200.44 |
| Burst | VM | 20.543 | 20.535 | 38.721 | 19.740 | 40.72 |
| Burst | Functions | 21.084 | 20.586 | 40.002 | 20.248 | 42.67 |
| Burst | Synapse | 35.407 | 35.426 | 67.134 | 33.992 | 71.94 |
| Sparse | VM | 0.636 | 0.549 | 1.101 | 0.000 | 2971.02 |
| Sparse | Functions | 0.916 | 0.775 | 1.592 | 0.000 | 2971.47 |
| Sparse | Synapse | 1.586 | 1.245 | 2.741 | 0.000 | 2972.52 |

Functions had 1/1/2 observed cold starts in moderate/burst/sparse traffic. Burst
queues reflect the two-slot driver cap; after dispatch, means were VM 0.803 s,
Functions 0.836 s and Synapse 1.414 s. These are not unrestricted elasticity results.

A separate initial 30-request/backend check validated 90/90 answers. Its mean
latencies were VM 0.625 s, Functions 0.885 s, Synapse 10.229 s. It is not pooled
into the full study. Managed statistics/cache/process state was not reset between
studies; the Synapse improvement's cause has not been isolated.

## Cost evaluation

The final native meter count matches **800/800 Function requests**, using
**423.80875 GB-seconds**. Synapse processed-data deltas sum to **93,018 MB** across
eight runs, choosing one non-resetting counter period per run.

Paid USD list-rate estimates for 800 queries/backend:

| Backend | Estimated cost | Average /100 queries |
|---|---:|---:|
| VM | $0.188730 | $0.023591 |
| Functions | $0.006941 | $0.000868 |
| Synapse | ~$0.465090 | ~$0.058136 |

| Pattern | VM /100 | Synapse /100 |
|---|---:|---:|
| Moderate | $0.005637 | ~$0.057767 |
| Burst | $0.001154 | ~$0.058457 |
| Sparse | $0.084179 | ~$0.058210 |

Rates: Linux VM $0.102/hour; classic Functions $0.000016/GB-second plus $0.20 per
million requests; Synapse $5/TB. Public rate responses were captured on 11 November
2026. VM cost includes its own observation windows and idle gaps. Functions uses
native usage plus validated request count. Its delayed/shared minute buckets are
not allocated to individual patterns. Synapse uses aggregate workspace deltas,
including automatic statistics; the explicit estimate assumes 1,000,000 MB/TB.
A binary conversion gives approximately $0.444 overall. Reconcile units with
billed usage before claiming invoice accuracy.

These estimates exclude credits, free allowances, discounts/taxes, common driver,
disks, IPv4, Blob capacity/requests, logs and networking. They are not the Azure
invoice. The common driver during matched windows is about $0.569 compute;
both VMs together continuously allocated through the latest snapshot would be
about $2.24 compute over roughly 11 hours, including setup and idle time. These
allocation proxies overlap the window estimates and are not additive line items.

## AWS comparison and interpretation

The matched AWS EC2/Lambda study used the same SF1 query mix, 100-request traces,
3/3/2 repetitions and two client slots. All 1,600 answers validated. It used
us-east-1, m7i-flex.large VMs (2 vCPU, 8 GiB), Lambda Python 3.12 at 2048 MB and
two DuckDB threads. Azure used Python 3.11 Functions, four detected threads,
managed host allocation, native Blob transport and HTTPS rather than regional SDK
invocation. CPU hardware and billing models are not equal.

| Pattern | AWS EC2 mean/p95 | AWS Lambda mean/p95 | Azure VM mean/p95 | Azure Functions mean/p95 |
|---|---:|---:|---:|---:|
| Moderate | 1.308 / 2.603 | 1.626 / 2.978 | 0.585 / 0.965 | 0.843 / 1.493 |
| Burst | 33.700 / 63.669 | 40.219 / 76.103 | 20.543 / 38.721 | 21.084 / 40.002 |
| Sparse | 1.352 / 2.710 | 1.679 / 3.148 | 0.636 / 1.101 | 0.916 / 1.592 |

For these configurations, Azure VM mean latency was 39–55% lower and Functions
45–48% lower. Azure Functions' aggregate backend cost estimate was about 6.2×
lower than Lambda's; Azure VM window cost was about 5.1% higher than EC2's because
fixed sparse windows dominate and its hourly rate was higher. Storage fees are
excluded, so this is not a full-system cost ranking.

Athena was tested separately in eu-north-1 from a laptop, two moderate repetitions
per backend. Mean/p95: Athena 2.154/3.408 s, paired Lambda 1.576/2.837 s. Estimated
Athena scan cost was ~$0.0302/100; paired Lambda compute/request cost ~$0.0049/100
plus ~$0.0094/100 S3 GET/HEAD. Driver/region/repetition/pricing differences prevent
a controlled Athena-versus-Synapse ranking. See the AWS handoff's Athena section.

## Files and responsibilities

| File | Purpose |
|---|---|
| `benchmarks/.../710.sql-query/input.py` | Input parameters and native Azure mode; shared generator/upload/validator. |
| `benchmarks/.../710.sql-query/python/function.py` | Query selection, native Blob credentials/curl transport, measured host limits. |
| `benchmarks/.../710.sql-query/python/package.sh` | Bundles Azure extension for Azure and httpfs for other providers. |
| `tools/create_azure_credentials.py` | Complete device login, explicit subscription and scoped principal creation. |
| `sebs/azure/config.py` | Recover partially initialized resource caches. |
| `sebs/azure/cli.py` | Secret-safe argv login, including leading-dash/space passwords; JSON bytes interface retained. |
| `sebs/cli.py` | Resource-group removal now respects `--dry-run`. |
| `evaluation/sql/azure_control.py` | SeBS Azure CLI container, safe argv, secret redaction and control-file delivery. |
| `evaluation/sql/azure_prepare.py` | SF1 upload and normal SeBS Function deployment; public source manifest. |
| `evaluation/sql/azure_infra.py` | Workspace/VM/network requests, bootstrap, guarded status and evaluation-group cleanup. |
| `evaluation/sql/azure_configure.py` | Verified source delivery, private settings, background application setup/status. |
| `evaluation/sql/azure_remote_setup.py` | Persistent service, ODBC and Synapse credential/views setup on the VMs. |
| `evaluation/sql/azure_client.py` | Existing SeBS Azure HTTP trigger normalized for shared replay. |
| `evaluation/sql/synapse_client.py` | Fresh encrypted ODBC requests, Q3/Q21 LIMIT→TOP translation, complete rows. |
| `evaluation/sql/azure_replay.py` | One-backend replay using shared scheduler/validator; Synapse counter snapshots. |
| `evaluation/sql/azure_run.py` | Repeated workload plan, detached job/lock, checkpoints, collection and summaries. |
| `evaluation/sql/azure_metrics.py` | Padded native execution meters and public VM metadata. |
| `evaluation/sql/azure_costs.py` | Paid retail rate selection, units/formulas, cost reports and separate overhead. |
| `evaluation/sql/service.py`, `replay_service.py`, `launch_driver.py` | Shared service/replay/bundle helpers reused by AWS and Azure. |
| `configs/sql-azure*.example.json` | Portable workload and pending SF1/SF10 diagnostic configurations. |

## Evidence, resume and cleanup

Committed evidence: [azure-swedencentral-sf1.json](results/azure-swedencentral-sf1.json).
It contains methodology, aggregates, per-query summaries, cost assumptions and all
2,400 relative request timings. It excludes credentials, account/resource names,
endpoints, SAS tokens and invocation IDs. Source archive/runner hashes are retained.
Raw data stays under ignored `results/sql-azure-sweden/runs/workloads-100/`;
regional deployment state and credentials remain in ignored `cache*`/`results/`.

To resume, reactivate the virtual environment and restore credential exports,
then inspect saved status/results instead of submitting `start` again. The full
reproduction and collection commands are in the setup guide. Repricing with
`azure_costs --refresh-meters` is read-only; never replay queries merely to repair
missing billing data. Preserve state until cloud cleanup is confirmed.

**Cleanup is outstanding.** No deletion was performed while preparing this handoff.
The Sweden evaluation group can be removed after preserving evidence; this removes
both VMs, disks/IPs/network, Synapse and default workspace storage. It retains the
SeBS Function/input account, allowing pending cold/warm tests. Clean that group
separately once finished. Earlier North Europe/West Europe attempts, default
monitoring workspaces and any Synapse-managed group need their own inventory.
The setup guide includes scoped preview/removal commands; do not delete caches
before identifying the cloud resources they track.

## Failures resolved

- WSL selected a Windows Docker launcher; `/snap/bin` PATH precedence fixed that
  host only. It is not an Azure requirement.
- Credential helper parsed a partial login stream and referenced an undefined
  subscription variable; it now waits for login completion and scopes creation.
- Core SeBS login used a formatted command that could misparse/echo a secret;
  argv with `--password=value` now preserves leading dashes/spaces and redacts
  failure output. The final login/cleanup guards have offline regression coverage;
  no new cloud deployment was run while preparing this handoff.
- Partial resource cache omitted `resource_group`; cache restoration now accepts it.
- Subscription registration/region/SKU restrictions blocked North Europe; all
  measured backends/data were deployed together in Sweden Central.
- DuckDB Blob TLS failed until curl transport was selected with verification on.
- VM status crashed on `securityProfile: null`; null profiles are handled.
- Extension URL was passed as a repository name; Azure uses `repository_url` and
  TPC-H preinstall matches the validator's repository metadata.
- Native monitoring returned 796/800 executions in a tight window; five-minute
  reporting margins produced 800/800 and 423.80875 GB-seconds without more queries.
- Resource-group cleanup ignored `--dry-run`; the deletion guard is fixed and
  covered by dry-run and actual-removal mock tests. Cloud cleanup is not verified.

## Pending work and commit preparation

1. Confirm evaluation-resource cleanup; VMs do not stop when a report is saved.
2. Run prepared SF1/SF10 controlled cold/warm tests through SeBS after collecting
   traffic-study meters; these change the same Function App's settings.
3. Measure higher concurrency, larger traffic inputs, matched Athena runs and
   storage/network/log-inclusive costs as separate experiments.
4. Classic Consumption has no Lambda-equivalent allocation sweep. Another Azure
   hosting plan would require an explicitly different study.

The [commit manifest](AZURE_COMMIT_FILES.txt) lists the intended source/config/
test/document files, not raw deployment artifacts. Review it before staging;
this handoff preparation ran no Git commands. The [setup guide](../docs/sql-azure-setup.md#12-troubleshooting-and-offline-checks)
contains the consolidated offline validation command. Local SQL/service/replay,
AWS adapters and Azure/credential/cache/cleanup regression checks are included;
no cloud run is performed by those tests.


From the repository root, review and stage the explicit manifest yourself:

```bash
git status --short
git diff --check
git add --pathspec-from-file=handoff/AZURE_COMMIT_FILES.txt
git diff --cached --stat
git diff --cached --check
git diff --cached
```

Review the staged content and any previously staged unrelated changes before:

```bash
git commit -m "Add Azure SQL comparison, results and handoff"
git push -u origin HEAD
```

The manifest assumes the existing SQL/AWS branch is the base; it is not a patch
that adds the whole SQL benchmark to upstream master. It deliberately omits
`configs/sql-azure.json`, caches, raw results, private state, credentials and
archives. No `git add .` is needed. Deleting cloud resources is a separate step.
