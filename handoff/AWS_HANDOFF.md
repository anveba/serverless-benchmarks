# SQL benchmark: AWS handoff

Status: 2026-11-02, branch `sql-benchmark`.

For setup from zero, deployment commands, validation, metrics, troubleshooting, and cleanup, follow [Run the SQL benchmark on AWS](../docs/sql-aws-setup.md). Earlier local implementation and measurements are in [HANDOFF.md](HANDOFF.md).

## Completed

- Standard SeBS ZIP deployment on AWS Lambda: Python 3.12, x64, DuckDB 2.0.0, `us-east-1`.
- Generated TPC-H Parquet data on the client and uploaded it to S3 through SeBS. Lambda reads it with `httpfs`.
- Validated all five complete queries (Q1, Q3, Q6, Q12, Q21) at SF0.01 and SF1.
- Repeated SF1 five times with validation; all passed and were warm.
- Ran and processed SeBS warm `perf-cost` experiments, first at 2048 MB, then a memory sweep.
- SF10 completed with the validation command after the SDK timeout fix. The saved result does not store validation pass/fail, and the original validation message was not preserved. SF10 reference answers are computed by the same DuckDB version on the host.
- Collected five cold and five warm HTTP samples each at SF1 and SF10, at 2048 MB; matched AWS metrics for all 20 samples.
- Fixed the SeBS SDK invocation timeout and disabled automatic invocation retries. Benchmark SQL was unchanged. Portable configs are in `configs/sql-aws*.example.json`.

The recorded experiments ran all five queries sequentially per invocation. A new optional single-query config is now available; it has offline coverage but has not been deployed on AWS. These results are not parallel query execution.

## Measurements

Five measured warm samples per allocation, zero execution failures. Durations include all five queries and connection/setup work.

| Allocated MB | Mean AWS execution, seconds | Mean billed GB-seconds |
|---|---:|---:|
| 1024 | 10.836 | 10.837 |
| 2048 | 7.021 | 14.043 |
| 3008 | 6.516 | 19.141 |

The 15 sanitized sample records are in [aws-sf1-memory.json](results/aws-sf1-memory.json). They omit account, resource, endpoint, and request identifiers. Raw output directories and deployment cache remain local.

More allocation reduced time but increased billed compute usage. These are preliminary results, excluding request, S3, transfer, and persistent idle costs. They establish no advantage over EC2 or Athena.

The single validated SF1 cold check used 391 MB maximum AWS-reported memory and about 10.91 seconds of execution. Each query uses a new DuckDB connection with the external file cache disabled; warm Lambda execution does not imply deliberately reusing a cached database connection.

## Cold and warm results

Five samples per group at 2048 MB, same HTTP trigger, zero execution failures:

| Dataset | Start | Mean AWS execution, seconds | Mean client latency, seconds | Mean AWS initialization, ms |
|---|---|---:|---:|---:|
| SF1 | Cold | 10.376 | 11.052 | 86.228 |
| SF1 | Warm | 7.145 | 7.677 | — |
| SF10 | Cold | 105.114 | 105.869 | 88.730 |
| SF10 | Warm | 102.374 | 102.642 | — |

All 20 samples, including per-query measurements and billing, are in [aws-cold-warm.json](results/aws-cold-warm.json). Cold minus warm mean client latency was 3.375 seconds at SF1 and 3.227 seconds at SF10. Warm client latency was 30.5% lower at SF1 and 3.0% lower at SF10. Most of the difference occurs in handler execution, not AWS-reported initialization; the precise cause has not been isolated.

These characterize the fixed five-query bundle, not individual-query service latency. Five samples per group support a preliminary comparison, not tail-latency guarantees.

## Failures and workarounds

- **Python 3.11 packaging:** attempted source compilation; a NumPy dependency failed without a compiler. Python 3.12 built successfully (ZIP approximately 31.9 MB).
- **SDK read timeout:** the default 60-second client timeout expired while SF10 continued for about 102–108 seconds. Logs showed repeated executions consistent with retries. The tested fix used a 360-second read timeout and one total attempt. The finalized fix uses 960 seconds to cover standard Lambda's 900-second maximum plus response margin, with one total attempt. The final setting was checked with a mocked client; AWS SF10 used the earlier 360-second setting.
- **Input cache:** switching SF0.01 to SF1 skipped the new uploads and caused S3 404. Passing `--update-storage` fixed the run. The cache now tracks sizes, but that fix has not run against S3.
- **Account allocation limit:** AWS rejected 4096 MB. The 1024/2048 MB samples were already saved, so only 3008 MB was run separately. Original folders: `results/sql-aws-sf1-memory-sweep` and `results/sql-aws-sf1-memory-3008`.
- **WSL-specific PATH:** the Windows Docker Desktop launcher preceded the working `/snap/bin/docker`. Prepending `/snap/bin` fixed this host; it is not a general prerequisite.

## Measurement and deployment limits

- Correctness checks used `--trigger library` (AWS SDK); `perf-cost` uses HTTP and creates a public Function URL with `NONE` authentication. Do not compare client timings across these paths as if they were equivalent.
- `perf-cost` filters warm samples and discards warm-up, but does not independently validate SQL answers. Validation was performed separately.
- The public URL, with the role's full S3 access, was exposed to SQL injection through the request in the code used for this run. The function now validates requests, but anyone with the URL can still use it, so delete it after running experiments.
- AWS testing used root credentials. Deployment with a restricted IAM identity has not been verified; the setup guide describes the intended IAM-based setup.
- AWS cache and local config are account-specific and are not committed. Keep the cache for reuse and eventual cleanup.
- Cleanup has not been run. The function, public URL, S3 data, and role may remain. Follow the setup guide's dry-run and cleanup steps when finished.
- Temporary disk is still Lambda's default; larger inputs/spills need separate validation. SF10 ran successfully; AWS SF30/SF100 runs were not performed.

## Individual-query Lambda milestone

**Lambda request-stream path verified on AWS; next step is the persistent EC2/DuckDB baseline.**

One deployed function supports five query IDs. Each request in this experiment
selects one ID and executes one complete query. Reused warm environments and
additional environments for overlapping requests are managed by AWS. The client
limit of two in-flight requests is not an AWS concurrency limit.

The `sql-workload` experiment is integrated with SeBS deployment and metrics
processing. Follow [the request-stream guide](../docs/sql-aws-workload.md) and copy
`configs/sql-aws-workload.example.json`. Optional query selection for ordinary
invocations uses `configs/sql-aws-single-query.example.json` and is preserved in
experiment metadata. The original five-query default remains available.

Verified SF1 smoke test: ten arrivals at offsets 0, 2, …, 18 seconds, query order
Q6/Q1/Q12/Q3/Q21 repeated twice, 2048 MB, Python 3.12/x64, SDK invocation, and at
most two simultaneous client invocations. All ten answers validated and provider
metrics matched ten of ten requests. Two requests were cold. Total billed Lambda
compute was **48.166 GB-seconds**, excluding request/S3/transfer charges.

| Request | Query | Cold | Client queue, s | Arrival-to-result, s | AWS execution, s |
|---:|---|---|---:|---:|---:|
| 0 | Q6 | Yes | 0.000 | 5.376 | 4.595 |
| 1 | Q1 | Yes | 0.001 | 5.056 | 4.447 |
| 2 | Q12 | No | 1.377 | 3.495 | 1.822 |
| 3 | Q3 | No | 1.057 | 2.875 | 1.514 |
| 4 | Q21 | No | 0.000 | 3.334 | 3.046 |
| 5 | Q6 | No | 0.000 | 1.124 | 0.768 |
| 6 | Q1 | No | 0.000 | 1.308 | 0.988 |
| 7 | Q12 | No | 0.000 | 2.469 | 2.166 |
| 8 | Q3 | No | 0.000 | 1.882 | 1.566 |
| 9 | Q21 | No | 0.000 | 3.306 | 2.999 |

[Sanitized workload samples](results/aws-sf1-workload.json) preserve the exact trace,
per-request timings, correctness, and provider billing without account, endpoint,
object-path, or request identifiers. Original raw files remain local under
`results/sql-aws-workload-01/sql-workload/`.

Q12/Q3 waited on the client because the first two requests occupied both slots.
Cold Q6/Q1 were slower than their warm repeats, but there is only one cold/warm
pair each. This is a functional smoke test, not statistical evidence that Lambda
beats another service. The configured resource update before replay can affect
the first requests' cold-start behavior. Validation occurs after replay; it is
excluded from measured request latency. No automatic warm-up or retries are used.

The SDK timeout fix is also exercised by the request runner. Existing query
selection, SDK timeout, and new workload tests pass offline. No EC2/Athena adapter
or distributed AWS query execution was added.

## Next work

1. Implement a persistent EC2/DuckDB service accepting the same query IDs and
   reading identical S3 files with DuckDB 2.0.0. Validate each query, then replay
   the identical schedule through an adapter to the shared runner.
2. Add an Athena adapter. Match SQL semantics and complete-result retrieval.
3. Define longer repeated traces for sparse, sustained, and bursty traffic;
   record cache/result-reuse policy, driver placement, resource configurations,
   queueing, errors, and the full cost observation window, including EC2 idle time.
4. Treat splitting one query across functions as separate future work. The
   existing parallel Q1/Q6/Q12 coordinator remains local-only.
5. Validate other providers. Restricted-IAM deployment was verified on a second account in `eu-north-1`.


## EC2 comparison milestone (comparison branch)

The moderate, burst and sparse SF1 workloads are complete: 1,600 valid answers,
zero execution failures. Reproduction, scripts, settings, limitations, sanitized
per-request results and cleanup commands are consolidated in the
[SQL comparison runbook](../evaluation/sql/README.md). EC2 had lower latency at
two concurrent requests; sparse Lambda measured-window compute was about 14.3x
cheaper. Cost estimates exclude full setup/inter-run uptime and storage/network
charges. Higher concurrency and Athena are next; neither has been measured.
Cleanup commands are prepared; AWS termination/deletion is not yet verified.
