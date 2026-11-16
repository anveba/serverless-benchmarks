# Copyright 2020-2026 ETH Zurich and the SeBS authors. All rights reserved.
"""Bulk load a generic Parquet dataset into its owned PostgreSQL namespace."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile

NAMESPACE = re.compile(r"sebs_[a-z0-9_]{1,58}")
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while block := source.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def postgres_type(value):
    if value in ("BIGINT", "INTEGER", "DATE", "VARCHAR", "DOUBLE", "SMALLINT"):
        return {"VARCHAR": "text", "DOUBLE": "double precision"}.get(
            value, value.lower()
        )
    if re.fullmatch(r"DECIMAL\(\d+,\d+\)", value):
        return value.lower()
    raise ValueError("Unsupported Parquet type: " + value)


def load_dataset(dataset, admin, fetch, reader_user="sebs_reader", replace=False):
    """Import atomically; fetch(file_descriptor, destination) supplies local/S3 data."""
    import duckdb
    import psycopg
    from psycopg import sql

    namespace = dataset["namespace"]
    if not isinstance(namespace, str) or not NAMESPACE.fullmatch(namespace):
        raise ValueError("Refusing a namespace not owned by SeBS")
    if not dataset.get("tables") or any(
        not IDENTIFIER.fullmatch(t) for t in dataset["tables"]
    ):
        raise ValueError("Invalid SQL table names")
    if not re.fullmatch(r"[a-f0-9]{64}", dataset["fingerprint"]):
        raise ValueError("Invalid dataset fingerprint")
    with psycopg.connect(
        **admin, autocommit=True, prepare_threshold=None
    ) as con, con.transaction():
        # Serializes first-time preparation and repeated/unknown SSM requests.
        con.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (namespace,)
        )
        exists = con.execute(
            "SELECT to_regclass(%s)", (namespace + "._sebs_metadata",)
        ).fetchone()[0]
        if exists:
            old = con.execute(
                sql.SQL("SELECT fingerprint, report FROM {}._sebs_metadata").format(
                    sql.Identifier(namespace)
                )
            ).fetchone()
            if old and old[0] == dataset["fingerprint"]:
                return old[1]
            if not replace:
                raise ValueError(
                    "Dataset changed; use --update-storage to replace only this SeBS namespace"
                )
        con.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(namespace)
            )
        )
        con.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
        report = {
            "namespace": namespace,
            "fingerprint": dataset["fingerprint"],
            "metadata": dataset.get("metadata", {}),
            "tables": {},
            "indexes": "declared primary keys only",
            "format": "native PostgreSQL heap tables",
            "server_version": con.info.server_version,
        }
        with tempfile.TemporaryDirectory() as directory, duckdb.connect() as duck:
            for table, spec in dataset["tables"].items():
                qualified = sql.SQL("{}.{}").format(
                    sql.Identifier(namespace), sql.Identifier(table)
                )
                count, hashes, previous_columns = 0, [], None
                if not spec["files"]:
                    raise ValueError("A SQL table must have at least one input file")
                for item in spec["files"]:
                    path, csv_path = (
                        Path(directory) / "input.parquet",
                        Path(directory) / "input.csv",
                    )
                    fetch(item, path)
                    digest = file_sha256(path)
                    if digest != item["sha256"]:
                        raise ValueError(
                            "Parquet content differs from the prepared manifest"
                        )
                    hashes.append(digest)
                    columns = [
                        (name, kind)
                        for name, kind, *_ in duck.execute(
                            "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
                        ).fetchall()
                    ]
                    if previous_columns is None:
                        definitions = sql.SQL(", ").join(
                            sql.SQL("{} {}").format(
                                sql.Identifier(name), sql.SQL(postgres_type(kind))
                            )
                            for name, kind in columns
                        )
                        con.execute(
                            sql.SQL("CREATE TABLE {} ({})").format(
                                qualified, definitions
                            )
                        )
                        previous_columns = columns
                    elif columns != previous_columns:
                        raise ValueError("Parquet fragments have inconsistent schemas")
                    duck.execute(
                        "COPY (SELECT * FROM read_parquet(?)) TO ? (FORMAT CSV, HEADER false)",
                        [str(path), str(csv_path)],
                    )
                    count += duck.execute(
                        "SELECT count(*) FROM read_parquet(?)", [str(path)]
                    ).fetchone()[0]
                    with con.cursor() as cursor, cursor.copy(
                        sql.SQL("COPY {} FROM STDIN WITH (FORMAT CSV)").format(
                            qualified
                        )
                    ) as copy:
                        with csv_path.open("rb") as source:
                            while block := source.read(4 * 1024 * 1024):
                                copy.write(block)
                    csv_path.unlink()
                keys = spec.get("primary_key", [])
                if keys:
                    con.execute(
                        sql.SQL("ALTER TABLE {} ADD PRIMARY KEY ({})").format(
                            qualified, sql.SQL(", ").join(map(sql.Identifier, keys))
                        )
                    )
                actual = con.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(qualified)
                ).fetchone()[0]
                if actual != count:
                    raise RuntimeError("SQL row count differs from source data")
                con.execute(sql.SQL("ANALYZE {}").format(qualified))
                report["tables"][table] = {"rows": count, "sha256": hashes, "primary_key": keys}
        report["database_bytes"] = con.execute(
            "SELECT pg_database_size(current_database())"
        ).fetchone()[0]
        report["settings"] = dict(
            con.execute(
                "SELECT name, setting FROM pg_settings WHERE name IN "
                "('shared_buffers','work_mem','max_connections','max_parallel_workers_per_gather','block_size')"
            ).fetchall()
        )
        con.execute(
            sql.SQL(
                "CREATE TABLE {}._sebs_metadata (fingerprint text NOT NULL, report jsonb NOT NULL)"
            ).format(sql.Identifier(namespace))
        )
        con.execute(
            sql.SQL("INSERT INTO {}._sebs_metadata VALUES (%s,%s::jsonb)").format(
                sql.Identifier(namespace)
            ),
            (dataset["fingerprint"], json.dumps(report)),
        )
        con.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(namespace), sql.Identifier(reader_user)
            )
        )
        con.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                sql.Identifier(namespace), sql.Identifier(reader_user)
            )
        )
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument(
        "--admin", type=Path, default=Path("/opt/sebs-postgres/admin.json")
    )
    parser.add_argument("--region", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    import boto3

    dataset = json.loads(args.dataset.read_text())
    client = boto3.client("s3", region_name=args.region)
    report = load_dataset(
        dataset,
        json.loads(args.admin.read_text()),
        lambda item, target: client.download_file(
            dataset["bucket"], item["key"], str(target)
        ),
        replace=args.replace,
    )
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print("DATASET_READY", report["namespace"], "no analytical query executed")


if __name__ == "__main__":
    main()
