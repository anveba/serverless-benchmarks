# Run the SQL comparison on Azure

Verified on 10–11 November 2026: SeBS Azure Functions, a persistent DuckDB VM and
Synapse serverless SQL in **Sweden Central**. The full SF1 study completed 2,400
validated requests without failures. Results, implementation details and remaining
work are in [AZURE_HANDOFF.md](../handoff/AZURE_HANDOFF.md); the credential-free
[recorded snapshot](../handoff/results/azure-swedencentral-sf1.json) includes all
2,400 measured request timings.

Functions uses the normal SeBS package build/deployment and HTTP trigger.
VM/Synapse orchestration is evaluation tooling under `evaluation/sql/`, not a new
SeBS deployment provider. Each traffic request executes one complete SQL query.

## 1. Checkout and local environment

Check out the branch/commit containing this guide, then run from its repository
root. The SQL data is generated with DuckDB; `benchmarks-data` is not required.
Use Linux with Python/venv and working Docker. The tested host was Ubuntu on WSL,
Python 3.12; the Function runtime is separately selected as Python 3.11.

```bash
python3 -m venv python-venv
source python-venv/bin/activate
python -m pip install -e .
sebs --help
python -c 'import duckdb; print(duckdb.__version__)'
docker info
```

Expected DuckDB: `2.0.0`. Reactivate the virtual environment in each new terminal.
If pip needs compiler prerequisites on Ubuntu (including Ubuntu in WSL), install
these and repeat the pip command:

```bash
sudo apt-get update
sudo apt-get install -y build-essential python3-dev libcurl4-openssl-dev libssl-dev
```

Docker must be installed, running and accessible to your user without sudo.
**WSL-specific workaround:** only if `/snap/bin/docker info` succeeds while
`docker info` selects the Windows Docker Desktop launcher:

```bash
export PATH="/snap/bin:$PATH"
hash -r
docker info
```

Otherwise fix Docker installation/socket permissions or Docker Desktop's WSL
integration first. SeBS runs Azure CLI inside its management container; host `az`,
SSH and a port-forwarding plugin are not required. Management/build images are
pulled by SeBS; the verified Python build image is
`spcleth/serverless-benchmarks:build.azure.python.3.11-1.2.1`.

## 2. Subscription and credentials

Use a subscription with permission to create resource groups, Functions/storage,
VM/network and Synapse resources. The tested deployment principal had Contributor
scoped to the intended subscription. Creating that principal also needs directory
permissions and permission to assign its role; reuse an administrator-provided
principal when you cannot create one yourself.

Select the intended subscription explicitly. Keep its ID for preflight:

```bash
read -rp 'Azure subscription ID: ' AZURE_SUBSCRIPTION_ID
export AZURE_SUBSCRIPTION_ID
```

For a new principal, run the existing SeBS helper once:

```bash
python tools/create_azure_credentials.py --subscription "$AZURE_SUBSCRIPTION_ID"
```

Enter a principal name and complete browser/device login. Save the returned
credentials privately. Do not create another principal every time you resume.
If the secret was lost, Azure Portal → Microsoft Entra ID → App registrations →
All applications → your application → Certificates & secrets → New client secret.
Copy **Value**, not Secret ID. Existing secret values cannot be recovered. An app
registration does not appear in the portal's ordinary All resources list.

In the **same terminal** used for the commands below:

```bash
read -rp 'Application/client ID: ' AZURE_SECRET_APPLICATION_ID
read -rp 'Tenant ID: ' AZURE_SECRET_TENANT
read -rsp 'Client secret VALUE: ' AZURE_SECRET_PASSWORD
printf '\n'
export AZURE_SECRET_APPLICATION_ID AZURE_SECRET_TENANT AZURE_SECRET_PASSWORD
python - <<'PY'
import os
for name in ['AZURE_SECRET_APPLICATION_ID', 'AZURE_SECRET_TENANT', 'AZURE_SECRET_PASSWORD']:
    print(name + ': ' + ('set' if os.environ.get(name) else 'MISSING'))
PY
```

Exports are terminal-local and normally disappear in a new terminal. Store the
secret in a password manager, not a committed file or shared logs. Use a
subscription-scoped principal and verify the saved subscription after preparation;
multiple-subscription principal selection is not established by this experiment.

## 3. Check region, providers and VM quota before allocating

Keep Functions, input storage, the VM pair and Synapse in one region. The example
uses `swedencentral`. Region availability depends on the subscription; the tested
Free Trial rejected North Europe SQL provisioning and initially lacked resource
provider registration.

This registers missing providers and checks Linux Consumption, SQL eligibility
and the **four available vCPUs** needed for two ordinary Standard_D2s_v5 VMs. It
creates no VM/workspace and submits no benchmark queries. Registration can be
asynchronous; if told to wait, rerun this check after a few minutes.

```bash
python - <<'PY'
import os
from evaluation.sql.azure_control import AzureControl
from evaluation.sql.azure_prepare import check_providers, eligible_vm_candidates

subscription = os.environ['AZURE_SUBSCRIPTION_ID']
region = 'swedencentral'
with AzureControl(subscription) as az:
    states = check_providers(az.run, register=True)
    for namespace in ['Microsoft.Web', 'Microsoft.Storage', 'Microsoft.Insights', 'Microsoft.OperationalInsights']:
        state = az.run(['provider', 'show', '--namespace', namespace])['registrationState']
        if state not in ('Registered', 'Registering'):
            az.run(['provider', 'register', '--namespace', namespace], json_output=False)
            state = az.run(['provider', 'show', '--namespace', namespace])['registrationState']
        states[namespace] = state
        print(namespace + ':', state)
    if any(value != 'Registered' for value in states.values()):
        raise SystemExit('Provider registration pending; wait and rerun this check.')
    base = f'https://management.azure.com/subscriptions/{subscription}'
    locations = az.run(['rest', '--method', 'get', '--url', base +
        '/providers/Microsoft.Web/geoRegions?api-version=2025-03-01'
        '&sku=Dynamic&linuxDynamicWorkersEnabled=true'])
    names = {item['properties']['name'].replace(' ', '').lower() for item in locations['value']}
    if region not in names:
        raise SystemExit('Linux Consumption is not listed in the selected region.')
    sql = az.run(['rest', '--method', 'get', '--url', base +
        f'/providers/Microsoft.Sql/locations/{region}/capabilities?api-version=2023-08-01'])
    if sql.get('status') != 'Available':
        raise SystemExit('SQL provisioning is restricted; inspect capabilities before creating resources.')
    skus = az.run(['vm', 'list-skus', '--location', region, '--size', 'Standard_D2s_v5',
                  '--resource-type', 'virtualMachines', '--all'])
    candidates = [item for item in eligible_vm_candidates(skus, region)
                  if item['name'] == 'Standard_D2s_v5']
    if len(candidates) != 1:
        raise SystemExit('Selected VM size is restricted or absent; inspect regional alternatives.')
    usage = az.run(['vm', 'list-usage', '--location', region])
    available = {item['name']['value'].lower(): item['limit'] - item['currentValue'] for item in usage}
    family = candidates[0]['family'].lower()
    if available.get('cores', 0) < 4 or available.get(family, 0) < 4:
        raise SystemExit('Need four free total and VM-family vCPUs for the pair.')
    print('Preflight passed:', region, candidates[0]['name'])
PY
```

This is an eligibility check, not a capacity guarantee. When **resuming existing
VMs**, use their saved status instead: their allocation already consumes quota.
If another region/size is needed, check all three backends there, use separate
regional cache/state directories, and record the hardware change. Do not edit a
saved deployment plan to point at another region.

## 4. Prepare SF1 and deploy Functions through SeBS

```bash
cp configs/sql-azure-workload.example.json configs/sql-azure.json
python -m evaluation.sql.azure_prepare \
  --config configs/sql-azure.json \
  --cache cache-sql-azure-swedencentral \
  --output-dir results/sql-azure-sweden \
  --count 30 --deploy-function --skip-preflight
```

The template selects Sweden Central, Python 3.11/x64 and native Azure access.
The local copy is gitignored; credentials are read from the environment. The
helper uses benchmark input preparation to generate/upload 20 SF1 Parquet objects
and `get_function` for the standard SeBS build/deploy path. It prepares a source
trace but makes **no cloud SQL invocation**. The full suite later derives
100-request schedules from this same input snapshot; `--count 30` is not a limit
on that suite. Preflight was already performed above.

Expected: `Prepared SF1: 20 Parquet objects, 30 arrivals, 2 slots`, followed by
saved `trace.json` and `function.json`. Verify subscription binding:

```bash
python - <<'PY'
import json, os
from pathlib import Path
state = json.loads(Path('results/sql-azure-sweden/function.json').read_text())
assert state['subscription_id'] == os.environ['AZURE_SUBSCRIPTION_ID']
assert state['region'] == 'swedencentral'
print('Prepared subscription and region match.')
PY
```

The function reads `az://container/key` through DuckDB's packaged Azure extension.
SeBS injects its storage connection string; no credential is part of the query
payload. Curl transport keeps TLS verification enabled. A clean clone downloads
packages/images/extensions; data generation and upload are outside request timing.

## 5. Provision the VM pair and Synapse

```bash
python -m evaluation.sql.azure_infra provision \
  --state-dir results/sql-azure-sweden \
  --vm-size Standard_D2s_v5 --security-type Standard
python -m evaluation.sql.azure_infra status --state-dir results/sql-azure-sweden
```

Always pass the recorded state/VM/security arguments; the helper retains legacy
North Europe/confidential-VM defaults for earlier saved attempts. The explicit
command pins the ordinary Ubuntu 24.04 Gen2 image version and requests two
2-vCPU/8-GiB VMs plus a Synapse workspace's built-in serverless SQL endpoint.
There is no dedicated SQL or Spark pool. VMs, disks, IPs and storage are billable.

Repeat **status** until Synapse is `Succeeded`, both VMs are `VM running` and both
show `BOOTSTRAP_READY`. Do not repeatedly launch. Request markers distinguish
explicit rejection from accepted/transport-unknown requests; inspect deployments
before retrying an unknown outcome.

## 6. Configure the service, driver and Synapse views

```bash
python -m evaluation.sql.azure_configure start --state-dir results/sql-azure-sweden
python -m evaluation.sql.azure_configure status --state-dir results/sql-azure-sweden
```

Repeat **status** until server and driver both show `SEBS_READY`. Setup installs
repository dependencies/extensions, the authenticated DuckDB systemd service,
ODBC Driver 18, and Synapse database/credentials/views over the saved Blob files.
It issues SQL DDL/metadata commands, but no Q1/Q3/Q6/Q12/Q21 analytical invocation.
Driver and server readiness metadata are saved locally by status.

The driver calls the server's private IP directly. NSGs deny inbound access except
server port 8080 from the driver's private IP; bearer authentication also applies.
Synapse allows SQL access from the driver's public IP. VM public IPs provide
outbound access; SSH is not opened. Azure Run Command is management only.

Source bundles are allowlisted and SHA256-verified. Credentials are delivered
separately into root-only files. Synapse's read/list container SAS lasts 48 hours.
On a diagnosed setup failure, apply the source fix and use:

```bash
python -m evaluation.sql.azure_configure start \
  --state-dir results/sql-azure-sweden --retry-failed
```

Then check status again. This sends a new bundle to the existing VMs, reuses pip
packages and does not replay queries. Use this setup refresh before a new run if
the SAS expired. Never reconfigure while a measured workload is active.

## 7. Run the full SF1 traffic suite once

```bash
python -m evaluation.sql.azure_run start \
  --state-dir results/sql-azure-sweden --run-id workloads-100 --workload all
python -m evaluation.sql.azure_run status \
  --state-dir results/sql-azure-sweden --run-id workloads-100
```

| Pattern | Queries/run/backend | Repetitions/backend | Spacing |
|---|---:|---:|---:|
| Moderate | 100 | 3 | 2 seconds |
| Burst | 100 | 3 | All at time zero |
| Sparse | 100 | 2 | 30 seconds |

This is 24 backend runs, 2,400 queries, each type twenty times/run, and two client
slots. Backend order rotates by repetition. Validation occurs after timing; there
is no analytical warmup, cold-start filtering or automatic query retry. A failed
backend saves available evidence and stops the job before the next backend.

Plan for roughly six hours: schedules alone require 5 h 26 m 42 s, including
4 h 57 m for sparse traffic. The detached driver job survives local terminal
closure. Both VMs remain billable; there is no automatic cleanup.

Repeat **status**, not **start**. Completion is `Status: complete`, `Completed
runs: 24 / 24`; status prints only the latest ten summaries. A local submission
marker, existing output guards and a driver-wide lock protect against duplicate
or overlapping jobs. Inspect an unknown/failed submission before changing state.

For an intentionally new experiment, use a new run ID. Patterns may be run
separately using `--workload moderate`, `burst`, or `sparse`; defaults remain
3/3/2 repetitions. `--count` and `--repetitions` change the study size and must be
recorded when comparing with AWS.

An optional initial check can use `azure_run start --run-id comparison-01` without
`--workload`: that executes the prepared 30-request trace on all three backends.
It is **not required** before the full suite. Keep separate Function experiments
at least ten minutes apart when using padded aggregate billing windows, and send
no unrelated invocations during a study, to avoid contamination of meter counts.

## 8. Collect, summarize and price the results

```bash
python -m evaluation.sql.azure_run collect \
  --state-dir results/sql-azure-sweden --run-id workloads-100
python -m evaluation.sql.azure_run summarize \
  --state-dir results/sql-azure-sweden --run-id workloads-100
python -m evaluation.sql.azure_costs \
  --state-dir results/sql-azure-sweden --run-id workloads-100 --refresh-meters
```

The recorded full study downloaded 135 JSON/log files and reported `Complete and
valid: True`. Each backend/run retains trace, requests, responses and summary;
Synapse also retains before/after processed-data counters. Local results are in
`results/sql-azure-sweden/runs/workloads-100`. These raw files remain gitignored.

The cost command first collects Azure Monitor execution count/units, public VM
metadata and resource inventory, then applies cached or freshly fetched regional
Azure Retail Prices API records. It writes `cost-report.json` and `.md`, with raw
price records under `pricing/`. It performs no benchmark queries. Missing metrics
stay unknown; a count mismatch makes the Function estimate provisional. Retry
only meter collection, not workload execution. The five-minute reporting margin
fixed the recorded 796/800 mismatch to 800/800; `azure_metrics --padding-minutes`
can adjust that window. Check for unrelated calls before attributing its usage.

Backend cost scope matches the AWS window study: VM own observation windows
including sparse idle gaps; Functions native GB-seconds plus validated request
count; Synapse processed-data deltas. Select one non-resetting daily/week/month
counter per Synapse run, never add the three periods. The explicit Synapse
conversion is 1,000,000 MB/TB; `--synapse-mb-per-tb 1048576` gives a binary-unit
sensitivity estimate. Reconcile units with billed usage for invoice accuracy.

Rates are paid USD list rates before credits/grants/discounts/taxes. Common driver,
OS disks, IPv4, Blob capacity/operations, logs and networking are separate. The
report keeps allocation proxies and unknown overhead explicit. Native Function
minute buckets do not provide exact per-pattern billing; only the aggregate
Function cost is reported. Preserve the price snapshots for historical reruns;
`--refresh-prices` deliberately fetches current rates. On API throttling, retain
usage and cached prices and retry pricing later, without rerunning SQL.

## 9. Clean up the evaluation VMs/Synapse after saving evidence

The remaining cold/warm Function experiments do not need the VM pair or Synapse.
Preview, then delete only the saved evaluation group:

```bash
python -m evaluation.sql.azure_infra cleanup --state-dir results/sql-azure-sweden
python -m evaluation.sql.azure_infra cleanup --state-dir results/sql-azure-sweden --apply
```

Verify asynchronous deletion finishes in Azure, including any Synapse-managed
resource group, disks and IPs. This retains local results/cache and the separate
SeBS Function/input-storage group. Cloud cleanup had **not been confirmed** at
handoff time. A push or local file deletion does not stop Azure charges.

## 10. Optional remaining SF1/SF10 controlled cold/warm tests

These configs are prepared but **not cloud-verified**. Run only after the traffic
suite completes and its meters are saved; SeBS changes the same Function App's
settings to force cold starts. Here one invocation runs **all five queries
sequentially**, matching the earlier AWS diagnostics. These are not individual
query traffic latencies. SeBS retains five cold/five warm samples and can execute
extra discarded/warmup/classification samples. Actual classifications determine
whether a sample was cold.

```bash
sebs experiment invoke perf-cost \
  --config configs/sql-azure-sf1-cold-warm.example.json --deployment azure \
  --cache cache-sql-azure-swedencentral --output-dir results/sql-azure-sf1-cold-warm --verbose
sebs experiment process perf-cost \
  --config configs/sql-azure-sf1-cold-warm.example.json --deployment azure \
  --cache cache-sql-azure-swedencentral --output-dir results/sql-azure-sf1-cold-warm
sebs benchmark statistics results/sql-azure-sf1-cold-warm/perf-cost/cold_results-processed.json
sebs benchmark statistics results/sql-azure-sf1-cold-warm/perf-cost/warm_results-processed.json
```

Upload and validate SF10 first; stop if validation or execution fails:

```bash
sebs benchmark invoke 710.sql-query large \
  --config configs/sql-azure-sf10-cold-warm.example.json --deployment azure \
  --architecture x64 --language python --language-version 3.11 --trigger http \
  --repetitions 1 --validate --update-storage --cache cache-sql-azure-swedencentral \
  --output-dir results/sql-azure-sf10-check --verbose
sebs experiment invoke perf-cost \
  --config configs/sql-azure-sf10-cold-warm.example.json --deployment azure \
  --cache cache-sql-azure-swedencentral --output-dir results/sql-azure-sf10-cold-warm --verbose
sebs experiment process perf-cost \
  --config configs/sql-azure-sf10-cold-warm.example.json --deployment azure \
  --cache cache-sql-azure-swedencentral --output-dir results/sql-azure-sf10-cold-warm
sebs benchmark statistics results/sql-azure-sf10-cold-warm/perf-cost/cold_results-processed.json
sebs benchmark statistics results/sql-azure-sf10-cold-warm/perf-cost/warm_results-processed.json
```

SF10 generation needs additional host disk/RAM and upload time. Its distinct size
prefix retains the SF1 snapshot. Azure's HTTP response limit can constrain long
queries; completion is not guaranteed. Before these diagnostic invocations, open Function App → Application Insights
and enable it if needed; provider-metric processing depends on those logs. Turning
it on later cannot recreate earlier application traces. Preserve raw results if
monitoring is delayed.
`memory-sizes: []` deliberately uses Consumption allocation: classic Consumption
has no equivalent to Lambda's 1024/2048/3008 MB allocation sweep.

## 11. Remove SeBS Function/storage resources when all tests are finished

Keep the matching regional cache until cleanup. It contains connection strings,
so never paste it wholesale. Print only the saved group name:

```bash
python - <<'PY'
import json
from pathlib import Path
cache = json.loads(Path('cache-sql-azure-swedencentral/azure.json').read_text())
print(cache['resources']['resource_group'])
PY
read -rp 'Exact SeBS resource group printed above: ' AZURE_SEBS_GROUP
sebs resources remove resource-groups "$AZURE_SEBS_GROUP" --dry-run \
  --config configs/sql-azure.json --deployment azure --cache cache-sql-azure-swedencentral \
  --output-dir results/sql-azure-sebs-cleanup
```

Verify the preview lists only the intended group. Then delete:

```bash
sebs resources remove resource-groups "$AZURE_SEBS_GROUP" \
  --config configs/sql-azure.json --deployment azure --cache cache-sql-azure-swedencentral \
  --output-dir results/sql-azure-sebs-cleanup
```

This branch fixes the resource-group `--dry-run` guard; older code deleted groups
even with that flag. The command matches prefixes, so use the complete saved name
and inspect the preview. Verify deletion in Azure. Retain cache/state locally for
audit; use a fresh cache/state directory for redeployment after deleting resources.

Old attempts remain separate: `results/sql-azure-comparison` and
`cache-sql-azure-northeurope`, plus an earlier `cache-sql-azure`. Inventory and clean
each using its matching region/config/subscription. The North Europe evaluation
group can use `azure_infra cleanup --state-dir results/sql-azure-comparison` and
then `--apply` after preview. Do not apply a Sweden config/cache to another region.
Check App Insights/Log Analytics workspaces outside the deleted groups; remove
only dedicated resources after verifying other applications do not depend on them.

## 12. Troubleshooting and offline checks

| Symptom | Resolution |
|---|---|
| Missing Azure exports after reopening a terminal | Reactivate venv and export the three values again. Reuse the principal; create a new secret if its Value was lost. |
| Service-principal secret begins with a dash or contains spaces | The login path now uses argv with `--password=value` and redacts failures. Restore exports and retry authentication. |
| Credential helper parse failure or undefined subscription variable | Use the fixed helper: it waits for login completion, parses account JSON separately, selects subscription and scopes Contributor creation. |
| Cache `KeyError: resource_group` | The fixed AzureResources loader accepts partially initialized caches. Preserve state; do not discard known cloud resources. |
| Provider not registered / SQL region restricted / VM quota zero | Check all four providers, SQL and Linux Consumption eligibility, regional SKU and both total/family quota before allocating. |
| Blob TLS CA error in the function | Use the Azure curl transport with TLS verification. Redeploy updated code through SeBS; do not disable verification. |
| `securityProfile: null` status crash | Status now treats null/missing profile as Standard and checks both bootstraps. |
| DuckDB URL is “not a known repository name” | Azure install uses `repository_url`; TPC-H preinstall matches the validator's default repository to avoid cached-origin mismatches. Resubmit diagnosed application setup with `--retry-failed`. |
| Setup/run appears stuck | Poll status; check saved log tail and request markers. Do not launch again merely because the CLI returned or timed out. |
| 796 native executions versus 800 valid requests | Refresh padded monitoring window; the recorded fix produced 800/800. Do not replay SQL to obtain billing data. |

Run the offline checks before committing; they do not launch cloud resources:

```bash
python -m unittest \
  tests.test_create_azure_credentials tests.test_azure_resources tests.test_azure_cli tests.test_resources_remove \
  tests.test_sql_query tests.test_sql_azure tests.test_sql_workload \
  evaluation.sql.test_service evaluation.sql.test_replay_service \
  evaluation.sql.test_cloud_driver evaluation.sql.test_workloads evaluation.sql.test_athena \
  evaluation.sql.test_azure_comparison evaluation.sql.test_azure_infra \
  evaluation.sql.test_azure_setup evaluation.sql.test_azure_run \
  evaluation.sql.test_azure_metrics evaluation.sql.test_azure_costs -q
```

Local configuration, `cache*`, raw `results/`, secrets, deployment archives and
private state are not commit inputs. The explicit proposed source/document list
is [AZURE_COMMIT_FILES.txt](../handoff/AZURE_COMMIT_FILES.txt).
