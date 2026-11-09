# SQL benchmark: Google Cloud

Status: 2026-11-09. `710.sql-query` runs on Cloud Functions gen2 and validates at SF0.01 and SF1.
Setup is in [docs/sql-gcp-setup.md](../docs/sql-gcp-setup.md).

## What was needed

- DuckDB reads Cloud Storage with an HMAC key of the deployment service account. SeBS passes
  `GCS_HMAC_KEY_ID` and `GCS_HMAC_SECRET` to functions that use storage.
- The config needs all three GCP sections (`function-gen1`, `function-gen2`, `container`).
- Gen2 only supports the HTTP trigger in SeBS. Functions are publicly invocable.

## AWS vs GCP, SF1

Same 10-request trace (Q6, Q1, Q12, Q3, Q21 twice, 2 s apart, at most 2 in flight), both driven
from a laptop. Results: [aws-gcp-sf1-workload.json](results/aws-gcp-sf1-workload.json).

| | AWS Lambda | GCP Cloud Functions gen2 |
|---|---|---|
| Region | eu-north-1 | europe-west1 |
| Resources | 2048 MB, CPU proportional | 2048 MB, 1 vCPU, 1 request per instance |
| Valid answers | 10/10 | 10/10 |
| Cold starts | 2 | 2 |
| Mean latency, all / warm | 2.49 s / 1.99 s | 3.84 s / 3.52 s |
| Mean warm handler time | 1.66 s | 3.08 s |

Warm DuckDB query time (s), mean of the warm runs:

| | Q6 | Q1 | Q12 | Q3 | Q21 |
|---|---:|---:|---:|---:|---:|
| AWS | 0.55 | 0.86 | 1.17 | 0.98 | 2.38 |
| GCP | 1.65 | 2.09 | 2.04 | 2.56 | 4.05 |

Both read the same data with the same number of storage requests per query.

## Cost per 100 queries (estimated)

| | AWS | GCP |
|---|---:|---:|
| Compute | $0.0071 | $0.0099 |
| Invocation requests | $0.00002 | $0.00004 |
| Storage requests | $0.0094 | $0.0106 |
| Total | $0.0165 | $0.0205 |

AWS compute uses the billed GB-seconds from the Lambda report. SeBS gets no billing data from GCP,
so GCP compute is the handler time rounded up to 100 ms, priced per vCPU-second and GiB-second.
Assumed list prices: Lambda $0.0000166667/GB-s, Cloud Run $0.000024/vCPU-s and $0.0000025/GiB-s,
$0.0004 per 1000 GET/class B requests on both. Prices were not checked for these regions, and free
tiers are ignored. Storage requests cost more than compute on both clouds.

## Limits

- 10 requests, each query twice: a first look, not a measurement.
- Different regions, and a laptop driver.
- CPU is not equal: 1 vCPU on GCP, and a share proportional to 2048 MB on Lambda.
- On GCP, DuckDB found no memory limit and used its default; on Lambda it is 75% of the function
  memory. Both use 2 threads.

## Next

1. Set DuckDB's memory limit on GCP the same way as on Lambda.
2. Repeat with the 100-request trace, and with 2 vCPUs on GCP.
3. Run both from drivers in their regions, or choose nearby regions.
