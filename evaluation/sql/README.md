# Reproduce the AWS SQL comparison

This is evaluation tooling, not a SeBS EC2 deployment
provider. It compares the existing `710.sql-query` Lambda with a persistent
DuckDB HTTP service on EC2. Higher concurrency has not been measured here; Athena
results are in [Athena](#athena). No query is split across functions.

## Recorded experiment and results

SF1 (`small`), DuckDB 2.0.0, Python 3.12, x86-64, `us-east-1`. The query order is
Q6, Q1, Q12, Q3, Q21 repeated 20 times per run. Each request executes one complete
query. Both systems read the same immutable Parquet object paths in S3 and return
complete results. Validation uses the existing benchmark validator after timing.

The server and separate load driver were `m7i-flex.large` (2 vCPU, 8 GiB), in the
same subnet/AZ. Lambda was configured with 2048 MB and two DuckDB threads. The
EC2 service used two workers, two threads per connection and a 1536 MB per-query
DuckDB limit. These settings do not make their CPU hardware or total memory equal.
M7i-flex CPU availability can vary; this is not a sustained fixed-CPU comparison.

The driver calls EC2 over private HTTP, with bearer authentication and a security
group restricted to the driver. Lambda uses the regional SDK, synchronous calls,
no automatic retries. SSM is management only; no tunnel carries measured queries.
The persistent service creates a fresh connection per request. DuckDB external
file caching is disabled on both. This does not imply every OS/network/extension
cache is cold. No explicit warm-up or forced cold starts; all starts are retained.
Both backends have a client limit of two in-flight requests. Burst arrivals wait
in the driver, so burst latency includes that queue. This tests a bounded service,
not Lambda's ability to scale beyond two environments.

| Workload | Arrival spacing | Runs per backend | Requests per backend | EC2 mean / p95 | Lambda mean / p95 |
|---|---:|---:|---:|---:|---:|
| Moderate | 2 s | 3 | 300 | 1.308 / 2.603 s | 1.626 / 2.978 s |
| Burst | all at 0 s | 3 | 300 | 33.700 / 63.669 s | 40.219 / 76.103 s |
| Sparse | 30 s | 2 | 200 | 1.352 / 2.710 s | 1.679 / 3.148 s |

All 1,600 requests validated; zero execution failures. Lambda had 2, 1 and 1
cold starts respectively. Samples pooled within each workload; p95 uses nearest
rank. Query types are mixed, and consecutive samples share environments: these
are descriptive results, not independent confidence intervals.

| Workload | EC2 mean run duration | Lambda mean run duration | EC2 estimated cost / 100 | Lambda estimated cost / 100 |
|---|---:|---:|---:|---:|
| Moderate | 200.67 s | 200.92 s | $0.00534 | $0.00536 |
| Burst | 67.73 s | 81.17 s | $0.00180 | $0.00527 |
| Sparse | 2972.70 s | 2972.96 s | $0.07907 | $0.00553 |

**Cost scope:** measured workload windows only, before credits/discounts. EC2 is
window seconds × $0.09576/3600; Lambda is native billed GB-seconds × $0.0000166667
plus $0.20/million invocations. These are recorded pricing assumptions, not live
quotes. Check [Lambda pricing](https://aws.amazon.com/lambda/pricing/) and
[EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/) before a new study.
Excluded: S3 requests/storage, EBS, public IPv4, logs, driver, setup, and inter-run
EC2 uptime. The actual AWS bill includes all running time, including while the
other backend is being measured. Do not present these estimates as that bill.
The idle gaps *inside* the sparse window are included. At this load, EC2 was
faster; sparse Lambda window compute cost was about 14.3 times lower.

Sanitized per-request timings, billing, per-run metrics and aggregate results are
in [recorded-results.json](recorded-results.json). Raw account-specific manifests,
state and responses stay under gitignored `results/`. Preserve them for audit.

## 1. Start from a clean checkout and deploy Lambda

Record the exact commit you checked out. From the
repository root, follow [AWS SQL setup](../../docs/sql-aws-setup.md), sections 1–6,
including dependencies, Docker, credentials, Lambda role, smoke and SF1 validation.
Use Python 3.12, x64, 2048 MB and `httpfs`. The Docker PATH workaround in that guide
is **WSL-specific**; the remaining AWS steps apply to Linux generally.

Then follow [individual-query workload setup](../../docs/sql-aws-workload.md):
copy/edit `configs/sql-aws-workload.example.json`, invoke and process the ten-request
trace into `results/sql-aws-workload-01`. Require ten valid answers and provider
metrics for all ten invocations. Add `--update-storage` if switching input size.
Do not overwrite any S3 inputs between backends. SF1 here is generated TPC-H data,
not a manually downloaded dataset or the complete TPC-H benchmark suite.

Credentials must be exported in each local terminal that runs AWS commands.
Never commit keys, bearer tokens, local configs, cache or raw resource state.
EC2 processes use instance-role credentials, not copied local keys. Deployment
permissions include EC2 instances/networking, IAM role/profile/policy management
and PassRole, SSM Run Command/parameters, S3, and the existing Lambda permissions.
Roles/profiles created by these helpers are `sebs-sql-ec2-role` and
`sebs-sql-driver-role`; use a separate account/state directory for unrelated work.

Run offline checks before creating resources:

```bash
python -m unittest evaluation.sql.test_service evaluation.sql.test_replay_service \
  evaluation.sql.test_cloud_driver evaluation.sql.test_workloads -v
```

## 2. Provision the SQL server

The helper selects the current Canonical Ubuntu 24.04 amd64 AMI and records its ID.
The original image was Ubuntu Noble build 20261023. A newer image/dependency set
can affect timing; the scripts preserve versions in metadata but do not pin the
whole operating-system image or every transitive pip dependency.

A default public subnet in `us-east-1a` with an internet-gateway route is required.
Use `--subnet subnet-...` if your account has no suitable default subnet. The
instances have public IPs for outbound package/S3/AWS access; no public ingress
is opened. This setup does not create a NAT gateway, load balancer or SSH key.

```bash
python -m evaluation.sql.provision_server
python -m evaluation.sql.check_ready server
```

If SSM/bootstrap is not ready, wait and repeat **check_ready**, not provisioning.
Cloud-init may take several minutes. If bootstrap fails, inspect EC2 cloud-init
and SSM logs before continuing. A fresh account may reject an instance type due
to its account-plan restrictions; the tested account accepted `m7i-flex.large`.
The helper checks AZ availability but cannot guarantee capacity or eligibility.
If IAM instance-profile propagation delays launch, retry the same provision command:
it reuses the exact saved request and client token. Do not delete launch state.

The script writes `results/sql-ec2/instance.json`, `deployment.json`, the launch
request, and a checksum-addressed S3 service bundle. It creates a 30 GiB gp3 root
volume with deletion on termination and a role with SSM plus read-only bucket access.
Install the service, then restrict its private endpoint to the driver group:

```bash
python -m evaluation.sql.deploy_service
python -m evaluation.sql.prepare_private_access
```

Each command must report `Success`. If it remains in progress, inspect its printed
SSM command ID before repeating. The access helper derives the Lambda name from
the processed ten-request results; `--lambda-function NAME` can select it explicitly.
It creates the driver role, security group, and a standard SSM SecureString token,
then restarts the server bound to its private IP. Tokens are not printed or bundled.
Do not rerun `deploy_service` after private setup: it installs the initial loopback
unit; rerun private setup afterward if you intentionally reinstall the service.

## 3. Provision and check the separate driver

```bash
python -m evaluation.sql.launch_driver
python -m evaluation.sql.check_ready driver
```

Again, repeat readiness until `Success`, without launching another driver. The
check verifies the bootstrap marker, private EC2 health/input manifest and Lambda
configuration. It submits no SQL. If it fails because a newly created IAM policy
has not propagated, retry readiness later.

The source allowlist includes the explicitly selected trace and plan, not local
credentials/configs/caches or the entire results tree. The driver is launched in
the server's subnet with the same AMI; it has a 30 GiB gp3 root volume. Driver
source is a snapshot: later local edits do not update an already-running driver.
`--trace PATH` on provision_server and launch_driver supports another trace path;
pass the same SF1 trace to both. The default path reproduces this study.

No AWS CLI, Session Manager plugin or port-forwarding tunnel is required by the
new commands; they use boto3/SSM. Those tools were used during initial interactive
debugging but are not part of the measured path.

## 4. Run the three workloads sequentially

Use these commands from the local repository terminal. They submit remote jobs
and return a command ID. They do not block your terminal for the workload duration.
**Finish each workload before submitting the next.** All commands have no retries
of SQL requests; the driver saves failures and stops the suite if a run is invalid.

```bash
python -m evaluation.sql.workloads submit moderate --repetitions 3
python -m evaluation.sql.workloads status
```

Wait for the submitted command to report `Success` and six summaries with
`valid=100/100`. Download before moving on:

```bash
python -m evaluation.sql.workloads download
python -m evaluation.sql.workloads submit burst --repetitions 3
python -m evaluation.sql.workloads status
```

After burst succeeds with six summaries:

```bash
python -m evaluation.sql.workloads download
python -m evaluation.sql.workloads submit sparse --repetitions 2
python -m evaluation.sql.workloads status
```

After sparse succeeds with four summaries:

```bash
python -m evaluation.sql.workloads download
python -m evaluation.sql.workloads report \
  --ec2-hourly 0.09576 --lambda-gb-second 0.0000166667 \
  --lambda-million-requests 0.20
```

Expected durations: moderate ~20 minutes, burst several minutes, sparse ~3 hours
20 minutes. Keep both instances running throughout; closing your terminal is fine.
Backend order alternates: EC2/Lambda in repetition 1, Lambda/EC2 in repetition 2,
EC2/Lambda in repetition 3. There is no cooldown between runs. Thirty-second gaps
do not guarantee Lambda cold starts; use the recorded flags.

Output lives under `results/sql-ec2/workloads-100/<workload>/repeat-N/<backend>/`:
`trace.json` (snapshot, placement/settings), `requests.json` (latency/cold/validation),
`responses.json` (complete answers, timings, Lambda billing), and `summary.json`
(counts/window). The report adds `comparison-cost-summary.json`. No extra SeBS
`process` call is needed: cloud Lambda responses already include AWS report metrics.
Missing billing fails reporting rather than silently counting it as zero.

A repeated output directory is rejected. To intentionally start a fresh study on
existing instances, pass a new label to **all** commands, e.g.
`python -m evaluation.sql.workloads --label study-02 submit moderate --repetitions 3`.
Keep old state and results. Do not manually resubmit after a client timeout without
checking SSM and the saved results; the remote command may still be running.
SSM truncates long displayed logs; use status plus saved summaries, not the tail
of stdout, as completion evidence.

## 5. Fixes required during the original setup

- Credentials exported in one terminal do not reach a service already started in
  another. Restart that process with credentials; EC2 uses roles instead.
- SSM Run Command did not set HOME, causing the post-run DuckDB validator to fail
  even though SQL requests succeeded. All supplied remote scripts set HOME=/root.
  We revalidated the original smoke responses without rerunning SQL and retained
  the pre-fix requests file. The 1,600-request study used the corrected environment.
- The first service start raced the initial health check. Deployment now retries
  connection-refused while the service starts.
- Default 60-second SDK reads were too short for SF10. The Lambda adapter uses
  read=960 seconds, connect=10, one total attempt. Function timeout remains 300 s.
- The SSM tunnel and laptop service were functional checks only. They were retired
  before measurement; do not compare their timings with the cloud-driver results.
- Identical object paths are verified, not S3 content hashes. Do not mutate files.

## 6. Cleanup now and after every reproduction

First download and validate all results using the commands above. Preserve local
results and state. Preview the exact instance IDs and attached-volume deletion:

```bash
python -m evaluation.sql.workloads cleanup
```

Then terminate **only the two recorded instances**, set SQL Lambda reserved
concurrency to zero, and remove its public Function URL:

```bash
python -m evaluation.sql.workloads cleanup --apply
```

This is termination, not stopping: the two root volumes are deleted with their
instances. The helper refuses if a recorded attached volume would survive.
It preserves S3 and logs for audit; IAM roles/groups and the standard SSM parameter
are retained and do not themselves incur instance charges. Verify termination:

```bash
python - <<'PY'
import json
from pathlib import Path
import boto3
p = Path('results/sql-ec2')
states = [json.loads((p / name).read_text()) for name in ['instance.json', 'driver-instance.json']]
ec2 = boto3.client('ec2', region_name=states[0]['region'])
ids = [s['instance_id'] for s in states]
for reservation in ec2.describe_instances(InstanceIds=ids)['Reservations']:
    for instance in reservation['Instances']:
        print(instance['InstanceId'], instance['State']['Name'])
PY
```

Repeat that read-only check until both say `terminated`. Review EC2 volumes and
Elastic IPs in the same region; the helpers allocate no Elastic IPs. AWS usage
reporting can lag. **S3, EBS or log storage retained elsewhere still costs money.**

To remove the remaining SeBS deployment, including its bucket/input snapshot and
Lambda, preview the framework cleanup before applying it:

```bash
sebs resources cleanup --config configs/sql-aws.json --deployment aws \
  --cache cache-sql-aws --dry-run
# After reviewing the resource list and preserving everything you need:
sebs resources cleanup --config configs/sql-aws.json --deployment aws \
  --cache cache-sql-aws
```

The framework cleanup also attempts to delete the SQL CloudWatch log group.
The bucket can contain earlier SF1/SF10 results and deployment bundles. This deletes
those remote copies too. Do not delete local cache before framework cleanup.
Inspect CloudWatch log groups afterward; remove the SQL group's retained logs
explicitly if no longer needed (the function name is in private-driver.json).
Do not delete unrelated groups. Also inspect S3 for any buckets listed in the
cleanup preview that were retained or failed deletion. Confirm deletion in AWS;
a successful local command alone is not a complete bill audit.

Deleting the public URL or disabling concurrency makes old SeBS cache trigger
metadata stale. For the next fresh reproduction, archive the old local state/cache,
use a new state directory/cache or a fresh checkout, and redeploy from section 1.
Do not reuse terminated instance launch requests to try to recreate a deployment.

## Files to commit

Commit `evaluation/__init__.py`, this entire `evaluation/sql/` source/test/docs
folder (including sanitized recorded-results.json), and the handoff links. Keep
these evaluation-only helpers separate from a later upstream SeBS benchmark PR.
Do not commit gitignored results, local AWS configs, service/driver archives, launch
state, cache, venv or secrets. Earlier SQL benchmark/framework files and portable
configs are prerequisites already described in the linked Lambda setup guide.

## Athena

Create Glue tables over the files of a Lambda `sql-workload` trace, then replay a trace against
Athena. Tables, the workgroup and its result location are reused by later runs.

```bash
python -m evaluation.sql.athena_setup --trace results/sql-aws-workload-sf1/sql-workload/trace.json \
  --region eu-north-1 --state results/sql-athena/athena.json
python -m evaluation.sql.cloud_replay --backend athena --trace TRACE \
  --athena-state results/sql-athena/athena.json --output-dir results/sql-athena/run-01
```

The deployment user needs `AmazonAthenaFullAccess`. The workgroup cancels queries scanning
more than 10 GiB, and result reuse is disabled per query. Results are written to the benchmark
bucket, so the SeBS cleanup removes them; the Glue database and workgroup stay.

## Next experiments

Raise the client limit together with the EC2 service worker configuration and
record oversubscription; leave the baseline settings as a separate experiment.
For Lambda elasticity, explicitly allow more concurrent environments and record
account limits/throttling. Add Athena against the same immutable input snapshot,
SQL semantics, arrivals and complete-result retrieval; account for scanned bytes,
minimum billing, polling, queueing and result reuse. Neither extension is included
in the results above.
