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

## Burst, SF1

100 requests arriving at once, in the order Q6, Q1, Q12, Q3, Q21 repeated, same configuration as
above. Results: [gcp-burst-sf1.json](results/gcp-burst-sf1.json).

| Requests in flight | Instances | Cold starts | Mean / p95 latency | All done after |
|---|---:|---:|---:|---:|
| 2 | 4 | 4 | 80.3 s / 146.6 s | 156 s |
| 20 | 19 | 19 | 16.1 s / 25.0 s | 29 s |

200/200 valid. With 2 in flight, latency is mostly queueing in the driver, as in the EC2/Lambda
burst. With 20, GCP started 19 instances and finished 5x sooner; each request took longer on
average (4.8 s against 3.1 s), mostly because of cold starts.

For reference, the us-east-1 EC2/Lambda burst with 2 in flight had mean latencies of 33.7 s (EC2)
and 40.2 s (Lambda). Regions, drivers and CPU allocations differ, so this is not a direct comparison.

## Sparse, SF1

100 requests, one every 30 s, two runs (about 50 min each). Results:
[gcp-sparse-sf1.json](results/gcp-sparse-sf1.json).

| Run | Valid | Cold starts | Instances | Mean / p95 latency | Est. cost per 100 queries |
|---|---:|---:|---:|---:|---:|
| 1 | 100/100 | 3 | 3 | 3.07 s / 5.40 s | $0.0192 |
| 2 | 100/100 | 1 | 1 | 3.42 s / 6.02 s | $0.0202 |

With requests 30 s apart, GCP kept an instance warm: 4 cold starts in 200 requests (6.4 s mean,
against 3.2 s warm). Warm latency per query: Q6 2.0 s, Q1 2.5 s, Q12 2.8 s, Q3 3.1 s, Q21 5.5 s.
Of the cost per 100 queries, about $0.009 is compute and $0.0105 storage requests.

The us-east-1 sparse runs (EC2 and Lambda, 2 runs each) had mean / p95 latencies of
1.35 / 2.71 s (EC2) and 1.68 / 3.15 s (Lambda), and compute cost per 100 queries of $0.079 (EC2,
including idle time in the window) and $0.0055 (Lambda), without storage requests. GCP compute
without storage requests was about $0.009 per 100. Regions, drivers and CPU allocations differ.

## Function vs BigQuery, SF1

BigQuery runs the same five queries through external tables over the same Parquet files, with the
query cache off. The SQL is unchanged and all answers validate. Setup and replay use
`evaluation/sql/bigquery_setup.py` and `bigquery_client.py`. Moderate workload (100 requests every
2 s, at most 2 in flight), two runs per backend. Results:
[gcp-function-bigquery-sf1.json](results/gcp-function-bigquery-sf1.json).

| Query | Function mean / p95 (s) | BigQuery mean / p95 (s) | BigQuery MB billed |
|---|---:|---:|---:|
| Q6 | 2.25 / 4.57 | 0.67 / 0.85 | 321 |
| Q1 | 2.63 / 4.08 | 0.73 / 0.86 | 447 |
| Q12 | 2.79 / 3.67 | 0.95 / 1.25 | 246 |
| Q3 | 3.15 / 3.70 | 1.16 / 1.31 | 312 |
| Q21 | 5.42 / 6.13 | 1.17 / 1.32 | 474 |
| All | 3.25 / 5.95 | 0.94 / 1.29 | 360 |

400/400 valid, no failures, 6 function cold starts, no BigQuery cache hits. BigQuery averaged
0.64 s execution and 0.04 s queueing, plus 0.26 s of client overhead.

Estimated cost per 100 queries: function $0.0194 ($0.0088 compute, $0.0105 storage requests),
BigQuery $0.21 at $6.25 per TiB, before the free TiB per month. BigQuery bills about 360 MB per
query, against 45–108 MB read by DuckDB from the same files: it bills uncompressed column sizes.
So BigQuery was 3.5x faster and about 11x more expensive per query here.

The BigQuery runs were on 2026-11-10 around 23:45 and the function runs about 35 minutes later; the
first function attempts were refused (403) before any query ran, so the order does not alternate.

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
