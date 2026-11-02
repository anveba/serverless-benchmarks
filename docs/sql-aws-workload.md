# Replay individual SQL requests on Lambda

Prerequisite: complete [AWS setup](sql-aws-setup.md) and validate each supported
query individually with the [single-query config](../benchmarks/700.analytics/710.sql-query/README.md#one-query-per-invocation).

This experiment submits independent complete queries to the same deployed function.
It does not split a query across workers. AWS decides which execution environments
serve requests. There is no warm-up, cold-start forcing, or cold/warm filtering;
observed starts are retained as part of the workload.

## Configure

```bash
cp configs/sql-aws-workload.example.json configs/sql-aws-workload.json
```

Set your execution-role ARN if required; credentials remain in the environment.
The example uses Python 3.12/x64, SF1, 2048 MB, and the SDK trigger. Its explicit
trace has ten requests, two of each supported query, spaced two seconds apart.

Inside `experiments.sql-workload`:

- `requests`: ordered objects with `at` (seconds from replay start) and `query`.
  Supported IDs: `q1`, `q3`, `q6`, `q12`, `q21`. Repeated IDs are allowed.
- Equal `at` offsets represent simultaneous arrivals (a burst); larger gaps model
  sparse use. Keep the exact trace identical for later baseline runs.
- `max-in-flight`: worker-pool limit on concurrent invocations. Requests queue on
  the driver if no slot is free. Queue delay is recorded, not silently discarded.
- `input-size`, `memory`, and `trigger`: dataset, allocation, and SeBS invocation
  path. Use the same settings when comparing traces. HTTP creates a public URL
  with the current AWS config; SDK is the example default.

## Run and process

```bash
sebs experiment invoke sql-workload \
  --config configs/sql-aws-workload.json --deployment aws \
  --cache cache-sql-aws --output-dir results/sql-aws-workload-01 --verbose
sebs experiment process sql-workload \
  --config configs/sql-aws-workload.json --deployment aws \
  --cache cache-sql-aws --output-dir results/sql-aws-workload-01
```

Use a fresh output directory for each replay. If switching input size, add
`--update-storage` to the invocation command. Data generation, upload, deployment,
and configuration updates occur before the arrival clock starts. A deployment
update may affect whether the first request is cold; save that context when
comparing systems. The measured observation window ends after the last request
completes, not at the last arrival. There is no post-run idle window yet.

## Outputs in `sql-workload/`

- `trace.json`: exact schedule, settings, input manifest, and scale factor.
- `requests.json`: one record per arrival, including query ID, scheduled/start/end
  offsets, driver queue delay, invocation latency, schedule-to-completion latency,
  request ID, failure/cold flags, and answer validation status.
- `invocations.json`: standard SeBS results with outputs and per-query timings.
- `invocations-processed.json`: provider execution, initialization, memory, and
  billing metrics after `process`; join to requests by request ID. Check whether
  metrics were found for every returned invocation before using billing values.

Validation happens after all timed requests finish, so reference computation
cannot alter arrivals. It checks each returned answer using the existing validator.
Failed/invalid requests remain in the output; do not report latency of only
successful answers without also reporting the failure count. Requests are not
retried. A client exception has an unknown server-side outcome and may still incur
charges; its record is retained even when it has no invocation ID/provider metrics.

Request records and invocation results are checkpointed as responses are collected.
An interrupted run may be incomplete; compare its request count with `trace.json`.
Raw files contain account/resource details and should remain local.

The runner is integrated with SeBS function deployment and processing. EC2/Athena
adapters, a dollar-cost calculator, and a common baseline observation window are
not implemented. This small trace is a smoke test, not a final benchmark design.

## Verified Lambda smoke test

The example trace completed on AWS at SF1/2048 MB with ten correct answers,
zero execution failures, two cold starts, and AWS metrics matched for all ten
requests. Billed Lambda compute totaled 48.166 GB-seconds; S3 and request charges
are not included. Client queueing affected two requests because of the configured
two-slot limit. See [the AWS handoff](../handoff/AWS_HANDOFF.md#individual-query-lambda-milestone)
for the table and sanitized samples. This validates the request-stream path;
it is not a performance comparison or a sufficient sample for tail latency.

Inspect correctness before interpreting timings:

```bash
python - <<'PYCODE'
import json
from pathlib import Path
folder = Path("results/sql-aws-workload-01/sql-workload")
trace = json.loads((folder / "trace.json").read_text())
requests = json.loads((folder / "requests.json").read_text())
assert len(requests) == len(trace["requests"]), "Incomplete replay"
assert all(r.get("valid") and not r["failure"] for r in requests), "Invalid/failed requests"
print(f"{len(requests)} requests completed with valid answers")
PYCODE
```

Metrics must also be processed with all invocation IDs matched before reporting
provider execution or compute cost. Keep the same input snapshot and arrival
trace for the forthcoming EC2/Athena adapters.
