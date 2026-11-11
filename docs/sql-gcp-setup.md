# Run the SQL benchmark on Google Cloud

Status: validated at SF0.01 and SF1 on Cloud Functions gen2, Python 3.12, x64, in europe-west1.
Results and a comparison with AWS are in [handoff/GCP_HANDOFF.md](../handoff/GCP_HANDOFF.md).

## Account

1. Create a project with billing enabled, and set a budget alert.
2. Enable Cloud Functions, Cloud Build, Cloud Run, Artifact Registry, Cloud Storage and Cloud Logging.
3. Create a service account with Cloud Functions Admin, Cloud Run Admin, Storage Admin,
   Service Account User and Logs Viewer. Download a JSON key.
4. In Cloud Storage → Settings → Interoperability, create an HMAC key for the service account.
   DuckDB reads Cloud Storage with this key; without it, use `access_mode: download`.

```bash
export GOOGLE_APPLICATION_CREDENTIALS=~/.sebs-gcp.json
export GCS_HMAC_KEY_ID=... GCS_HMAC_SECRET=...
```

SeBS passes the HMAC key to functions that use storage, as environment variables of the function.

## Config

```bash
cp configs/sql-gcp.example.json configs/sql-gcp.json
```

Set `deployment.gcp.project_name` and `region`. The template uses gen2 with 1 vCPU and 2048 MB,
and one request per instance (`gcp-concurrency: 1`), to match a 2048 MB Lambda processing one
request per environment. SeBS makes GCP functions publicly invocable; clean up after experiments.

## Run

```bash
python -m sebs.cli benchmark invoke 710.sql-query test --config configs/sql-gcp.json \
  --deployment gcp --trigger http --repetitions 1 --validate \
  --cache cache-sql-gcp --output-dir results/gcp-smoke
python -m sebs.cli experiment invoke sql-workload --config configs/sql-gcp.json \
  --deployment gcp --cache cache-sql-gcp --output-dir results/gcp-workload-01
```

Cleanup: `python -m sebs.cli resources cleanup --config configs/sql-gcp.json --deployment gcp --cache cache-sql-gcp`
(preview with `--dry-run`).
