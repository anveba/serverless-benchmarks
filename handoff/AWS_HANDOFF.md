# SQL benchmark: AWS handoff

Status: 2026-11-01, branch `sql-benchmark`.

For setup from zero, deployment commands, validation, metrics, troubleshooting, and cleanup, follow [Run the SQL benchmark on AWS](../docs/sql-aws-setup.md). Earlier local implementation and measurements are in [HANDOFF.md](HANDOFF.md).

## Completed

- Standard SeBS ZIP deployment on AWS Lambda: Python 3.12, x64, DuckDB 2.0.0, `us-east-1`.
- Generated TPC-H Parquet data on the client and uploaded it to S3 through SeBS. Lambda reads it with `httpfs`.
- Validated all five complete queries (Q1, Q3, Q6, Q12, Q21) at SF0.01 and SF1.
- Repeated SF1 five times with validation; all passed and were warm.
- Ran and processed SeBS warm `perf-cost` experiments, first at 2048 MB, then a memory sweep.
- No benchmark source changes were needed during AWS testing. The portable config is `configs/sql-aws.example.json`.

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

## Failures and workarounds

- **Python 3.11 packaging:** attempted source compilation; a NumPy dependency failed without a compiler. Python 3.12 built successfully (ZIP approximately 31.9 MB).
- **Input cache:** switching SF0.01 to SF1 skipped the new uploads and caused S3 404. Passing `--update-storage` fixed the run. Size-aware caching remains unfixed.
- **Account allocation limit:** AWS rejected 4096 MB. The 1024/2048 MB samples were already saved, so only 3008 MB was run separately. Original folders: `results/sql-aws-sf1-memory-sweep` and `results/sql-aws-sf1-memory-3008`.
- **WSL-specific PATH:** the Windows Docker Desktop launcher preceded the working `/snap/bin/docker`. Prepending `/snap/bin` fixed this host; it is not a general prerequisite.

## Measurement and deployment limits

- Correctness checks used `--trigger library` (AWS SDK); `perf-cost` uses HTTP and creates a public Function URL with `NONE` authentication. Do not compare client timings across these paths as if they were equivalent.
- `perf-cost` filters warm samples and discards warm-up, but does not independently validate SQL answers. Validation was performed separately.
- AWS testing used root credentials. Deployment with a restricted IAM identity has not been verified; the setup guide describes the intended IAM-based setup.
- AWS cache and local config are account-specific and are not committed. Keep the cache for reuse and eventual cleanup.
- Cleanup has not been run. The function, public URL, S3 data, and role may remain. Follow the setup guide's dry-run and cleanup steps when finished.
- Temporary disk is still Lambda's default; larger inputs/spills need separate validation. AWS SF10/SF30/SF100 runs were not performed.

## Next work

1. Build a persistent EC2/DuckDB baseline with the same engine version, SQL, and S3 files. Define request-arrival schedules and include idle costs.
2. Add Athena over the same data as a managed-service baseline.
3. Integrate parallel orchestration with SeBS experiments/workflows and port it to AWS. The existing Q1/Q6/Q12 coordinator is local-only and leaves intermediate objects behind.
4. Fix input-size caching and validate other providers. Direct `httpfs` access currently supports S3-compatible storage; other backends use download mode.

No EC2/Athena baseline, AWS parallel run, or other-provider test has been completed.
