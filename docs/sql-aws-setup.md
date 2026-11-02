# Run the SQL benchmark on AWS

These instructions use SeBS's standard deployment and experiment commands for `710.sql-query`. Tested: Python 3.12, x64, DuckDB 2.0.0, `us-east-1`, and `httpfs`. Each invocation runs Q1, Q3, Q6, Q12, and Q21 sequentially. Parallel cloud execution is not implemented.

## 1. Prerequisites and checkout

Use Linux/WSL, Git, Python 3.10+ with venv support, and Docker accessible without sudo. The tested host uses Python 3.12. Internet access is needed for packages, images, DuckDB extensions, and AWS.

```bash
git clone --branch sql-benchmark https://github.com/anveba/serverless-benchmarks.git
cd serverless-benchmarks
git submodule update --init --recursive
```

Skip cloning if already in this branch's checkout. The SQL dataset is generated; it does not use files from `benchmarks-data`.

```bash
docker info
```

**Optional: WSL-specific Docker PATH fix.** On the tested WSL machine, `docker` selected a Windows Docker Desktop launcher even though a working Linux Docker daemon existed. This is not a general deployment step. Skip it when `docker info` already works. The fix for that machine was:

```bash
/snap/bin/docker info
export PATH="/snap/bin:$PATH"
hash -r
command -v docker
docker info
```

Use this fix only if `/snap/bin/docker info` works. Keep using that terminal; the PATH change is session-local. Otherwise fix Docker installation/integration first. For permission errors, check `id` and `ls -l /var/run/docker.sock`; the user must have access to the daemon socket.

## 2. Install this checkout

```bash
python3 -m venv python-venv
source python-venv/bin/activate
python -m pip install -e .
sebs --help
python -c "import duckdb; print(duckdb.__version__)"
```

Expected DuckDB: `2.0.0`. If pip cannot build `pycurl` on Ubuntu, install build prerequisites, then retry the pip command:

```bash
sudo apt-get update
sudo apt-get install -y build-essential python3-dev libcurl4-openssl-dev libssl-dev
```

Reactivate the venv in every new terminal. This run used the installed `sebs` CLI, not the older tutorial's `./sebs.py`.

## 3. AWS credentials, region, and execution role

Use a deployment IAM identity allowed to manage the Lambda/S3/logging resources and pass the Lambda execution role. If SeBS creates the role, it also needs IAM role/policy management. `perf-cost` creates a Function URL and its resource policy. See [AWS platform configuration](platforms.md#aws-lambda) for the framework's AWS permission model.

For an IAM user, obtain an access key via AWS Console → IAM → Users → deployment user → Security credentials → Create access key → CLI. Save the secret when created; AWS cannot show it again. Do not create root keys for this setup.

Enter credentials without placing values in shell history:

```bash
read -rsp "AWS access key ID: " AWS_ACCESS_KEY_ID; echo
read -rsp "AWS secret access key: " AWS_SECRET_ACCESS_KEY; echo
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
export AWS_DEFAULT_REGION=us-east-1
export AWS_REGION=us-east-1
python - <<'PY'
import boto3
identity = boto3.client('sts').get_caller_identity()
print('Account:', identity['Account'])
print('Identity:', identity['Arn'])
print('Region:', boto3.Session().region_name)
PY
```

This branch explicitly consumes the access-key environment variables. AWS CLI profiles alone are insufficient. Temporary/SSO deployment credentials are not covered by this runbook: the branch's explicit AWS client construction does not forward a session token.

Check the execution role separately:

```bash
python - <<'PY'
import boto3
iam = boto3.client('iam')
try:
    print(iam.get_role(RoleName='sebs-lambda-role')['Role']['Arn'])
except iam.exceptions.NoSuchEntityException:
    print('Role absent; SeBS can create it if the deployment identity permits this.')
PY
```

The role must trust `lambda.amazonaws.com` and permit reading benchmark inputs, writing outputs, and CloudWatch logging. With an empty `lambda-role` setting, SeBS finds/creates `sebs-lambda-role` and attaches its standard policies (`AWSLambdaBasicExecutionRole`, `AmazonS3FullAccess`, `AmazonDynamoDBFullAccess`). The SQL benchmark does not itself use DynamoDB. For a preconfigured role, put its ARN in the local config and provision its permissions separately.

## 4. Initial config

```bash
cp configs/sql-aws.example.json configs/sql-aws.json
```

The template selects AWS, `us-east-1`, Python 3.12, x64, ZIP packaging, and the SF1 warm memory sweep. Optionally edit `deployment.aws.lambda-role` to use your existing role ARN. Leave credentials out of JSON. This local config is gitignored.

SeBS generates `cache-sql-aws/aws.json` after initializing resources. It records the region, account ID, bucket, and role. Keep that cache to reuse this deployment; choose a different cache for a different account/region.

## 5. Smallest validated AWS run

```bash
sebs benchmark invoke 710.sql-query test \
  --config configs/sql-aws.json --deployment aws \
  --architecture x64 --language python --language-version 3.12 \
  --trigger library --repetitions 1 --validate \
  --cache cache-sql-aws --output-dir results/sql-aws-smoke-py312 --verbose
```

SeBS generates SF0.01 locally, uploads its Parquet files to S3, installs function dependencies in its Docker build image, deploys Lambda, invokes through the AWS SDK, and validates all five answers. No manual bucket/Lambda creation, MinIO, DockerHub account, or ECR container deployment is needed.

```bash
sebs benchmark process --config configs/sql-aws.json --deployment aws \
  --cache cache-sql-aws --output-dir results/sql-aws-smoke-py312
sebs benchmark statistics results/sql-aws-smoke-py312/results.json
```

`experiments.json` is saved by invocation; `results.json` includes AWS metrics after processing. If logs are not yet available, retry processing later; do not rerun the query just to fetch metrics.

## 6. Validate SF1 before measuring

```bash
sebs benchmark invoke 710.sql-query small \
  --config configs/sql-aws.json --deployment aws \
  --architecture x64 --language python --language-version 3.12 \
  --trigger library --repetitions 1 --validate --update-storage \
  --cache cache-sql-aws --output-dir results/sql-aws-sf1-check --verbose
sebs benchmark process --config configs/sql-aws.json --deployment aws \
  --cache cache-sql-aws --output-dir results/sql-aws-sf1-check
sebs benchmark statistics results/sql-aws-sf1-check/results.json
```

`test` selects SF0.01; `small` selects SF1; `large` selects SF10. Data generation/upload is outside invocation timing. `httpfs` reads remote ranges and columns; it does not first download whole tables. DuckDB can spill intermediate data to Lambda's temporary disk.

**Required workaround:** use `--update-storage` whenever changing size or generated input layout. The storage cache marks the benchmark uploaded without tracking size, so switching `test` → `small` otherwise skips new uploads and produces S3 404 errors. This remains unfixed. Repeating the same already-uploaded size does not require reuploading.

For an optional repeated correctness check, repeat the SF1 invocation command with `--repetitions 5`, omit `--update-storage`, and use a fresh output directory. `benchmark invoke` records but does not enforce warm starts.

## 7. Controlled warm experiment and memory sweep

`perf-cost` invokes the same complete five-query workload. It is not distributed query execution. `concurrent-invocations: 1` means one full workload at a time. It discards warm-up and rejects cold samples. Unlike `benchmark invoke --validate`, this experiment does not independently validate SQL answers; validate the workload first.

The committed template uses five warm samples each at 1024, 2048, and 3008 MB. Adjust `experiments.perf-cost.memory-sizes` to allocations accepted by your account. More configured memory also changes CPU capacity; measure billed GB-seconds as well as latency.

```bash
sebs experiment invoke perf-cost \
  --config configs/sql-aws.json --deployment aws \
  --cache cache-sql-aws --output-dir results/sql-aws-sf1-memory-sweep --verbose
sebs experiment process perf-cost \
  --config configs/sql-aws.json --deployment aws \
  --cache cache-sql-aws --output-dir results/sql-aws-sf1-memory-sweep
```

For each `M` in `1024`, `2048`, `3008`:

```bash
sebs benchmark statistics results/sql-aws-sf1-memory-sweep/perf-cost/warm_results_1024-processed.json
sebs benchmark statistics results/sql-aws-sf1-memory-sweep/perf-cost/warm_results_2048-processed.json
sebs benchmark statistics results/sql-aws-sf1-memory-sweep/perf-cost/warm_results_3008-processed.json
```

Raw samples are `warm_results_M.json`; processed samples are `warm_results_M-processed.json`. Processing also produces `result.csv`. Preserve raw samples and failures.

This implementation of `perf-cost` uses HTTP and creates a **public Function URL (`NONE` authentication)**. The earlier correctness checks use the SDK. Compare provider execution timings across them, not client timings as if the invocation paths were identical. Do not leave the public endpoint available after testing.

## 8. Validate SF10 and measure cold/warm behavior

SF10 generation/upload require more host disk space and time than SF1. First run its correctness check:

```bash
sebs benchmark invoke 710.sql-query large \
  --config configs/sql-aws.json --deployment aws \
  --architecture x64 --language python --language-version 3.12 \
  --memory 2048 --trigger library --repetitions 1 --validate --update-storage \
  --cache cache-sql-aws --output-dir results/sql-aws-sf10-check --verbose
sebs benchmark process --config configs/sql-aws.json --deployment aws \
  --cache cache-sql-aws --output-dir results/sql-aws-sf10-check
sebs benchmark statistics results/sql-aws-sf10-check/results.json
```

Require `output validation passed` in the invocation output before claiming correctness. The result JSON does not store a separate validation flag. SF10 is checked against host-side DuckDB execution, not independent published answers. The recorded warm invocation took approximately 102 seconds at 2048 MB.

Copy the portable configs:

```bash
cp configs/sql-aws-sf1-cold-warm.example.json configs/sql-aws-sf1-cold-warm.json
cp configs/sql-aws-sf10-cold-warm.example.json configs/sql-aws-sf10-cold-warm.json
```

If using an explicit role, set `deployment.aws.lambda-role` in both copies to match `sql-aws.json`; otherwise leave it empty. Both templates use Python 3.12, 2048 MB, five cold plus five warm samples, and one invocation at a time. Both modes use HTTP. SeBS enforces cold starts, discards warm-up, and filters cold invocations from the warm samples.

Run SF1, then SF10 (allow roughly 20 minutes or more for SF10):

```bash
sebs experiment invoke perf-cost \
  --config configs/sql-aws-sf1-cold-warm.json --deployment aws \
  --cache cache-sql-aws --output-dir results/sql-aws-sf1-cold-warm --verbose
sebs experiment process perf-cost \
  --config configs/sql-aws-sf1-cold-warm.json --deployment aws \
  --cache cache-sql-aws --output-dir results/sql-aws-sf1-cold-warm
sebs benchmark statistics results/sql-aws-sf1-cold-warm/perf-cost/cold_results_2048-processed.json
sebs benchmark statistics results/sql-aws-sf1-cold-warm/perf-cost/warm_results_2048-processed.json

sebs experiment invoke perf-cost \
  --config configs/sql-aws-sf10-cold-warm.json --deployment aws \
  --cache cache-sql-aws --output-dir results/sql-aws-sf10-cold-warm --verbose
sebs experiment process perf-cost \
  --config configs/sql-aws-sf10-cold-warm.json --deployment aws \
  --cache cache-sql-aws --output-dir results/sql-aws-sf10-cold-warm
sebs benchmark statistics results/sql-aws-sf10-cold-warm/perf-cost/cold_results_2048-processed.json
sebs benchmark statistics results/sql-aws-sf10-cold-warm/perf-cost/warm_results_2048-processed.json

```

Processing must find five measured invocations per group. `process` retrieves metrics; it does not run an experiment. A directory alone does not establish completion: check for raw `cold_results_2048.json` and `warm_results_2048.json` before processing. Use a new output directory when repeating an experiment.

## 9. Troubleshooting

- **SDK read timeout at 60 seconds:** this branch gives synchronous SDK invocations a 960-second read timeout, 10-second connection timeout, and one total attempt. This covers standard Lambda's 900-second maximum without automatic duplicate requests. It does not change the configured 300-second function timeout. Inspect CloudWatch before retrying a failed client request: execution may have continued.
- **Python 3.11 package build fails:** the tested build attempted to compile DuckDB and a NumPy dependency without a compiler. Use the tested Python 3.12 runtime in the template and commands.
- **S3 404 after changing size:** rerun the invocation with `--update-storage`. The current cache tracks upload status per benchmark, not per size.
- **MemorySize rejected:** use allocations allowed by the account. The tested account rejected 4096 MB and accepted up to 3008 MB. This is an allocation limit, not a query out-of-memory error. Preserve result files for allocations completed before the failure; run only the remaining allocation in a new directory and process both directories.
- **Invocation fails without an exception in SeBS output:** inspect the function's CloudWatch log group (`/aws/lambda/<function-name>`). Keep the failed `experiments.json` to match request IDs.
- **Temporary disk fills:** download mode fetches whole input files, and DuckDB can spill in either mode. Lambda defaults to 512 MB of temporary storage; SeBS does not currently configure `EphemeralStorage`. Start with `httpfs` and the validated sizes.

## 10. Cleanup

Keep the cache while continuing experiments. When finished, preview cleanup using this branch's actual CLI (older docs use different command names):

```bash
sebs resources cleanup --config configs/sql-aws.json --deployment aws \
  --cache cache-sql-aws --dry-run
```

Inspect the proposed resources. To actually delete the deployment resources, including S3 inputs/results and the function/public endpoint, run the same command without `--dry-run`. Cleanup is destructive; do it only when finished. Do not delete the cache before cleanup. Verify in AWS afterward; review retained IAM roles separately.
