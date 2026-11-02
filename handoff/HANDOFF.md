# Handoff: SQL benchmark for SeBS

**AWS update (2026-11-02):** Single-function AWS deployment, answer validation at SF0.01/SF1, SF10 execution, the SF1 memory sweep, and SF1/SF10 cold/warm measurements now work. Follow [the AWS setup guide](../docs/sql-aws-setup.md) for reproducible commands; read [AWS_HANDOFF.md](AWS_HANDOFF.md) for results and remaining gaps. The local findings below describe the earlier local work.

Status of the `sql-benchmark` branch, based on upstream `master` at `089e5c6`.
Remove this folder before opening a PR upstream: `git rm -r handoff`.

## Summary

`710.sql-query` runs TPC-H queries with DuckDB over Parquet files in object storage, on one
function or split across several. It works end to end on the SeBS local deployment.
The local results show that the benchmark works and roughly how much memory it needs; they do not establish cloud speed or cost. The subsequent AWS runs are recorded separately in [AWS_HANDOFF.md](AWS_HANDOFF.md).

| Project goal | Status |
|---|---|
| One query on a single function | Validated locally and on AWS at SF0.01/SF1 |
| Comparison with an always-on service and Athena | Not started |
| Different input sizes | Works locally from SF0.01 to SF30 |
| Query split across workers | Prototype gives correct answers, performance unknown |
| Other providers | Not started |

## What is on the branch

* `benchmarks/700.analytics/710.sql-query/`: the benchmark. Start with its `README.md`.
* `scripts/sql_parallel.py`: coordinator for parallel queries. It runs on the client and only
  supports local deployments.
* `sebs/local/local.py`, `sebs/cli.py`: three changes to local deployments:
  * Functions can reach storage on non-Linux hosts.
  * `local start --memory` limits memory and CPU, with CPU proportional to memory as on Lambda.
  * `local start --deployments N` starts N containers. It used to return the same one N times.
* `duckdb` is now a SeBS dependency, because data is generated on the host.

The number `700.analytics` is a placeholder and has not been discussed with the SeBS maintainers.
`600` is used by workflows on upstream's `feature/workflows` branch.

## Running it locally

Tested on Apple Silicon (arm64) with Python 3.11 functions and DuckDB 2.0.0. x64 has not been built.
Use your machine's architecture in `--architecture`.

```bash
git submodule update --init
./install.py --local            # add --aws for AWS
. python-venv/bin/activate

python -m sebs.cli storage start all configs/storage.json --output-json storage.json

# Single function, checking the answers
python -m sebs.cli benchmark invoke 710.sql-query test --config configs/example.json \
  --deployment local --storage-configuration storage.json --architecture arm64 --validate

# Parallel query on 4 instances, limited like a 1769 MB Lambda
python -m sebs.cli local start 710.sql-query small deployment.json --config configs/example.json \
  --storage-configuration storage.json --deployments 4 --memory 1769 --architecture arm64
python scripts/sql_parallel.py deployment.json --query q12 --baseline
```

* `perf-cost` also ran at SF0.01 with 0 errors. Its config needs `"deployment": "local"`, the right
  `architecture`, and `"benchmark": "710.sql-query"`.
* `storage.json` contains local storage keys, and `out_*.json` files are not gitignored. Do not commit them.
* Set `SEBS_SQL_SCALE_FACTOR=30` when deploying to use SF30. It needs about 17 GB in the temp directory,
  plus about 8 GB in the storage volume.
* Docker's memory must exceed the `--memory` value. The SF30 runs used 16 GB.
* Functions reach storage through the host's LAN IP, so restart storage if the network changes.

## Results

Raw data is in `results/`. Times are in milliseconds, and each run is on one laptop, with storage on
the same machine. Limited runs had CPU set in proportion to memory.

| File | Runs | Contents | Timing |
|---|---|---|---|
| `limits.jsonl` | 56 | Q1, Q3, Q6, Q12 at SF1 and SF10, 256 MB to 5.3 GB, both read modes | Median of 3 |
| `parallel.jsonl` | 26 | Q1, Q6, Q12 at SF1 and SF10, 1 to 8 workers at 1769 MB | Fastest of 3 |
| `ceiling.jsonl` | 36 | Q21 at SF1, SF10, SF30; all five queries at SF30, up to 10 GB | Median of 3 |

No run returned a wrong answer. Answers are checked against DuckDB's official TPC-H answers at SF0.01
and SF1. SF10 and SF30 have none, so they are checked against the same queries run on the host, with
the same DuckDB version. A DuckDB bug would not show up at those sizes.

### Findings likely to carry over to AWS

**Memory limits (approximate).** Q21 is the only benchmark query that ran out of memory at the sizes
tested:

| Q21 | Ran out of memory | Lowest size that always worked | Read |
|---|---|---|---|
| SF1 | 128 MB | 256 MB | 0.3 GB |
| SF10 | 1024 MB (512 MB passed once, then failed) | 1280 MB | 3.5 GB |
| SF30 | 1769 MB (passed once, then failed) | 3538 MB | 11 GB |

* Only the sizes in the raw data were tried, so the real limits may be lower.
* Q1, Q3, Q6 and Q12 passed at every size tried, and peaked under 700 MB at SF30.
* Extrapolating, Q21 at SF100 would need about 10 GB, Lambda's maximum. This is untested.
* In a separate one-off script at SF10, Q9 and Q18 worked at 512 MB and failed at 256 MB. They are not
  in the benchmark, and only their row counts were looked at.
* Failures are kills from the out-of-memory killer, not DuckDB errors, and the same query can pass
  and then fail near the limit. Repeat runs on AWS.

**Shuffle cost.** For Q12 at SF10 with 8 workers:

| | Objects written | GET requests |
|---|---|---|
| Single function | 0 | 1,638 |
| 8 partitions | 136 | 2,046 |
| 32 partitions | 544 | 2,782 |

**Read modes.**

* Download mode fetches whole tables: about 2 GB for `lineitem` at SF10. That exceeds Lambda's default
  512 MB `/tmp`, and SeBS does not set `EphemeralStorage`. Use `httpfs` first.
* `httpfs` read 537 MB for Q1 at SF10, so column pruning works there.
* `httpfs` read much more on joins: 1.67 GB for Q12 at SF10, against 482 MB with local files. The
  cause is unknown, and it matters for the comparison with Athena.

### Findings that are local only

Do not report these as performance results.

* **Parallel speedup stalled.** Q1 at SF10 took 2.2 s on one function, 1.2 s with 2 workers, and
  1.1 s with 4 or 8. Q12 never clearly beat one function (2.9 s against 2.8 s). All workers and
  storage shared one machine, which may be the bottleneck.
* **More memory stopped helping.** Q21 at SF30 took 32 s at 3538 MB and 40 s at 10 GB, and Q6 took
  2.7 s and 6.1 s. The cause was not investigated. Lambda may behave differently.

### Unexplained

* At SF10, Q21 got through one run at 512 MB, but failed on its first run at 1024 MB.

## Known gaps

* **AWS single-function path tested:** Python 3.12/x64 packaging, S3 access, and SF0.01/SF1 validation passed. Python 3.11 packaging failed. Azure and GCP only support download mode. See the AWS handoff for remaining gaps.
* **Local timer bug, not fixed.** `dockerfiles/local/python/server.py` stops the timer before calling
  the handler, so local `exec_time` measures only the import (SeBS recorded 7 microseconds for a
  warm run with a 253 ms client time). Use client time or the benchmark's own timers. A fix needs a
  rebuild of the run image. Node's `server.js` was not checked.
* **Parallel queries:**
  * Q21 and Q3 are not parallelized, only Q1, Q6 and Q12.
  * The coordinator is client-side and local only.
  * Shuffle objects are not removed after a query.
  * The shuffle has no optimizations, such as Starling's combining.
* **Other:**
  * Not in the regression list (`sebs/regression.py`).
  * `function.py` has a few flake8 warnings, and the benchmark was not run through `black`.
  * The sweep scripts that produced `results/` are not included.

## Measurement notes

* Each query uses a new DuckDB connection with `enable_external_file_cache` off. With a reused
  connection, repeat queries fetched 0 bytes from storage, which would make warm runs look much faster.
  Caching in warm instances could be studied separately.
* `bytes_read` comes from DuckDB's profile, and `get_requests` and `head_requests` from its HTTP log.
* The first local image pull may fail with a macOS Keychain error (`User canceled the operation`).
  The images are public, so `docker pull` gets past it.

## Original next steps (superseded by AWS_HANDOFF.md)

1. **Set up AWS.** Create budget alerts first, then credentials, and use one region for everything.
   Check for research or education credits, since they decide how large the runs can be.
2. **Smoke test at SF0.01** with `--deployment aws --validate`. Expect some small fixes, starting
   with the function's S3 permissions.
3. **Single function at SF1 and SF10.** Run a memory sweep with `perf-cost`, cold and warm, using `httpfs`.
   This gives the first real numbers, and shows whether more memory helps on Lambda.
4. **SF30, then possibly SF100.** Generate data on an EC2 instance in the same region, because uploading
   this much from home is slow. Test the Q21 extrapolation. The benchmark's 300 s timeout may
   need raising.
5. **Athena and always-on baselines.** Run the same queries on the same files, with DuckDB on EC2 as
   the always-on system, so that comparison isolates serverless from the engine. These need new
   scripts, as SeBS does not cover them. The main plot is cost per query against time between queries.
6. **Parallel on Lambda.** Either adapt the coordinator to invoke Lambdas directly, or build on
   upstream's `feature/workflows` (PR #303), whose `660.map-reduce` has the same shape. That framework
   needs a Redis VM for measurements, and its AWS `Map` state may cap concurrency, so check this first.
7. **Talk to the SeBS maintainers.** Nothing has been sent yet. Ask about the benchmark's number,
   direct `httpfs` access, and where the baselines should live. The three local fixes, and the timer
   fix once made, could be small PRs on their own.

Watch costs from step 4 on. Athena charges by data scanned, and EC2 instances cost money until stopped.
