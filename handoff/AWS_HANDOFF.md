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

Each invocation runs all five queries sequentially. These results are not parallel query execution.

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
- **Input cache:** switching SF0.01 to SF1 skipped the new uploads and caused S3 404. Passing `--update-storage` fixed the run. Size-aware caching remains unfixed.
- **Account allocation limit:** AWS rejected 4096 MB. The 1024/2048 MB samples were already saved, so only 3008 MB was run separately. Original folders: `results/sql-aws-sf1-memory-sweep` and `results/sql-aws-sf1-memory-3008`.
- **WSL-specific PATH:** the Windows Docker Desktop launcher preceded the working `/snap/bin/docker`. Prepending `/snap/bin` fixed this host; it is not a general prerequisite.

## Measurement and deployment limits

- Correctness checks used `--trigger library` (AWS SDK); `perf-cost` uses HTTP and creates a public Function URL with `NONE` authentication. Do not compare client timings across these paths as if they were equivalent.
- `perf-cost` filters warm samples and discards warm-up, but does not independently validate SQL answers. Validation was performed separately.
- AWS testing used root credentials. Deployment with a restricted IAM identity has not been verified; the setup guide describes the intended IAM-based setup.
- AWS cache and local config are account-specific and are not committed. Keep the cache for reuse and eventual cleanup.
- Cleanup has not been run. The function, public URL, S3 data, and role may remain. Follow the setup guide's dry-run and cleanup steps when finished.
- Temporary disk is still Lambda's default; larger inputs/spills need separate validation. SF10 ran successfully; AWS SF30/SF100 runs were not performed.

## Next work

1. Expose one query ID per request through SeBS input/experiment configuration. The handler accepts a query list, but the generator currently selects all five.
2. Add a reproducible individual-query request stream, then persistent EC2/DuckDB and Athena baselines using identical files, SQL, and schedules. Include EC2 idle costs. Independent request concurrency is separate from splitting one query across workers.
3. Integrate parallel orchestration with SeBS experiments/workflows and port it to AWS. The existing Q1/Q6/Q12 coordinator is local-only and leaves intermediate objects behind.
4. Fix input-size caching and validate other providers. Direct `httpfs` access currently supports S3-compatible storage; other backends use download mode.

No EC2/Athena baseline, AWS parallel run, or other-provider test has been completed.
