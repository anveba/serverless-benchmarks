"""Create BigQuery external tables over the benchmark's Parquet files in Cloud Storage."""
import argparse
import json
import re
from pathlib import Path

import google.auth
from google.auth.transport.requests import AuthorizedSession

from evaluation.sql.service import manifest_fingerprint

API = "https://bigquery.googleapis.com/bigquery/v2"
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def table_uri(bucket, keys):
    # an external table reads every file matching the wildcard
    prefixes = {key.rsplit("/", 1)[0] for key in keys}
    if len(prefixes) != 1:
        raise ValueError(f"Table files are not in a single folder: {sorted(prefixes)}")
    return f"gs://{bucket}/{prefixes.pop()}/*.parquet"


def table_definition(project, dataset, table, uri):
    if not IDENTIFIER.fullmatch(dataset) or not IDENTIFIER.fullmatch(table):
        raise ValueError(f"Invalid identifier: {dataset}.{table}")
    return {
        "tableReference": {"projectId": project, "datasetId": dataset, "tableId": table},
        "externalDataConfiguration": {"sourceFormat": "PARQUET", "sourceUris": [uri]},
    }


def check(response, allowed=()):
    if not response.ok and response.status_code not in allowed:
        raise RuntimeError(f"BigQuery request failed ({response.status_code}): {response.text[:500]}")
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path,
                        help="trace.json of a GCP sql-workload run, which records the input files")
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", required=True, help="the bucket's region, e.g. europe-west1")
    parser.add_argument("--dataset", default="sebs_sql_comparison")
    parser.add_argument("--state", type=Path, default=Path("results/sql-bigquery/bigquery.json"))
    args = parser.parse_args()

    input_config = json.loads(args.trace.read_text())["input"]
    bucket = input_config["bucket"]["bucket"]
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    base = f"{API}/projects/{args.project}/datasets"

    check(session.post(base, json={
        "datasetReference": {"projectId": args.project, "datasetId": args.dataset},
        "location": args.location,
    }), allowed=(409,))
    dataset = check(session.get(f"{base}/{args.dataset}")).json()
    if dataset["location"].lower() != args.location.lower():
        raise ValueError(f"Dataset is in {dataset['location']}, not {args.location}")

    tables = {}
    for table, keys in input_config["tables"].items():
        uri = table_uri(bucket, keys)
        check(session.delete(f"{base}/{args.dataset}/tables/{table}"), allowed=(404,))
        check(session.post(f"{base}/{args.dataset}/tables",
                           json=table_definition(args.project, args.dataset, table, uri)))
        tables[table] = {"uri": uri, "files": len(keys)}
        print(f"{table}: {len(keys)} files", flush=True)

    state = {
        "project": args.project,
        "location": args.location,
        "dataset": args.dataset,
        "input_fingerprint": manifest_fingerprint(input_config),
        "scale_factor": input_config["scale_factor"],
        "tables": tables,
    }
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, indent=2) + "\n")
    print(f"Saved {args.state}", flush=True)


if __name__ == "__main__":
    main()
