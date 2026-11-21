"""Prepare/remove a recorded covering-index treatment without changing Q21 SQL."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re

import boto3

from sebs.storage.aws_postgresql import AWSPostgreSQL
from sebs.storage.sql_config import sql_config


INDEXES = [
    {"name": "sebs_eval_q21_lineitem_cover", "table": "lineitem",
     "keys": ["l_orderkey"], "include": ["l_suppkey", "l_receiptdate", "l_commitdate"]},
    {"name": "sebs_eval_q21_orders_final", "table": "orders",
     "keys": ["o_orderkey"], "include": [], "predicate": "o_orderstatus = 'F'"},
]


REMOTE = r'''
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time
import boto3
import psycopg
from psycopg import sql

request = __PAYLOAD__
namespace = request["namespace"]
folder = Path("/opt/sebs-postgres/evaluation")
folder.mkdir(exist_ok=True)
owner_path = folder / ("q21-indexes-" + hashlib.sha256(namespace.encode()).hexdigest()[:16] + ".json")
owner = json.loads(owner_path.read_text()) if owner_path.exists() else {
    "namespace": namespace, "fingerprint": request["fingerprint"], "indexes": {}}
if owner["namespace"] != namespace or owner["fingerprint"] != request["fingerprint"]:
    raise RuntimeError("Index ownership record differs from dataset")
report = {"action": request["action"], "profile": "q21-covering-v1", "namespace": namespace,
          "fingerprint": request["fingerprint"], "benchmark_queries_executed": 0,
          "started_at": datetime.now(timezone.utc).isoformat(), "steps": []}
started = time.perf_counter()
settings = json.loads(Path("/opt/sebs-postgres/admin.json").read_text())
settings.update(connect_timeout=10, application_name="sebs-q21-index-evaluation")

def persist_owner():
    owner_path.write_text(json.dumps(owner, indent=2) + "\n")

with psycopg.connect(**settings, autocommit=True) as con:
    con.execute("SET statement_timeout = '15min'")
    con.execute("SET lock_timeout = '5s'")
    metadata = sql.SQL("SELECT fingerprint FROM {}._sebs_metadata").format(sql.Identifier(namespace))
    actual = con.execute(metadata).fetchone()
    if not actual or actual[0] != request["fingerprint"]:
        raise RuntimeError("Prepared dataset fingerprint differs")
    if con.execute("SELECT count(*) FROM pg_stat_activity WHERE usename = 'sebs_reader' AND state = 'active'").fetchone()[0]:
        raise RuntimeError("A benchmark query is active; run maintenance after it finishes")
    def sizes():
        return {table: con.execute("SELECT pg_total_relation_size(%s::regclass)",
                    (namespace + "." + table,)).fetchone()[0] for table in ("lineitem", "orders")}
    report["before_table_bytes"] = sizes()
    if request["action"] == "prepare" and shutil.disk_usage("/var/lib/docker").free < 8 * 1024**3:
        raise RuntimeError("Covering-index preparation requires at least 8 GiB free disk space")
    for item in request["indexes"]:
        name = item["name"]
        qualified = sql.Identifier(namespace, name)
        existing = con.execute("SELECT pg_get_indexdef(to_regclass(%s))", (namespace + "." + name,)).fetchone()[0]
        recorded = owner["indexes"].get(name)
        if existing and (not recorded or recorded["definition"] != existing):
            raise RuntimeError("Existing index is not owned with the expected definition: " + name)
        step_begin = time.perf_counter()
        if request["action"] == "remove":
            if existing:
                con.execute(sql.SQL("DROP INDEX {}").format(qualified))
            report["steps"].append({"index": name, "action": "removed", "seconds": time.perf_counter()-step_begin})
            continue
        if not existing:
            # CREATE INDEX's name is unqualified; its table determines the schema.
            statement = sql.SQL("CREATE INDEX {} ON {} ({})").format(
                sql.Identifier(name), sql.Identifier(namespace, item["table"]),
                sql.SQL(", ").join(map(sql.Identifier, item["keys"])))
            if item["include"]:
                statement += sql.SQL(" INCLUDE ({})").format(sql.SQL(", ").join(map(sql.Identifier, item["include"])))
            if item.get("predicate"):
                # Predicate comes only from the fixed profile in this source file.
                statement += sql.SQL(" WHERE ") + sql.SQL(item["predicate"])
            con.execute(statement)
            existing = con.execute("SELECT pg_get_indexdef(to_regclass(%s))", (namespace + "." + name,)).fetchone()[0]
            owner["indexes"][name] = {"definition": existing}
            persist_owner()
        report["steps"].append({"index": name, "action": "present", "definition": existing,
                               "seconds": time.perf_counter()-step_begin,
                               "bytes": con.execute("SELECT pg_relation_size(%s::regclass)",
                                   (namespace + "." + name,)).fetchone()[0]})
    if request["action"] == "prepare":
        for table in ("lineitem", "orders"):
            step_begin = time.perf_counter()
            # Standard VACUUM, never FULL; enables visibility-map checks for index-only scans.
            con.execute(sql.SQL("VACUUM (ANALYZE) {}").format(sql.Identifier(namespace, table)))
            report["steps"].append({"table": table, "action": "vacuum_analyze",
                                   "seconds": time.perf_counter()-step_begin})
    else:
        owner["removed_at"] = datetime.now(timezone.utc).isoformat()
        persist_owner()
    report["after_table_bytes"] = sizes()
    report["visibility"] = [dict(zip(("table", "pages", "all_visible_pages"), row))
        for row in con.execute("""SELECT c.relname, c.relpages, c.relallvisible FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s
            AND c.relname IN ('lineitem','orders')""", (namespace,)).fetchall()]
    with con.transaction():
        con.execute("SET TRANSACTION READ ONLY")
        con.execute("SET LOCAL ROLE sebs_reader")
        con.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(namespace)))
        report["estimated_plan"] = con.execute("EXPLAIN (FORMAT JSON) " + request["query"]).fetchone()[0]
report["elapsed_seconds"] = time.perf_counter()-started
report["finished_at"] = datetime.now(timezone.utc).isoformat()
boto3.client("s3", region_name=request["region"]).put_object(
    Bucket=request["bucket"], Key=request["key"],
    Body=json.dumps(report, default=str).encode(), ContentType="application/json")
print(json.dumps({"bucket": request["bucket"], "key": request["key"]}))
'''


def remote_script(payload):
    if not re.fullmatch(r"sebs_[a-z0-9_]{1,58}", payload["namespace"]):
        raise ValueError("Invalid prepared namespace")
    if not re.fullmatch(r"[a-f0-9]{64}", payload["fingerprint"]):
        raise ValueError("Invalid prepared fingerprint")
    if payload["action"] not in ("prepare", "remove") or payload["indexes"] != INDEXES:
        raise ValueError("Only the fixed Q21 index profile is supported")
    return "set -eu\n/opt/sebs-postgres/venv/bin/python - <<'SEBS_Q21_INDEXES'\n" + REMOTE.replace("__PAYLOAD__", repr(payload)) + "\nSEBS_Q21_INDEXES\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "remove"])
    parser.add_argument("--storage-configuration", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("results/postgresql-aws-sf10-q21-indexes"))
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--fingerprint", required=True)
    args = parser.parse_args()
    config = sql_config(json.loads(args.storage_configuration.read_text())["sql"])
    if config.provider != "aws":
        parser.error("Existing AWS PostgreSQL storage is required")
    if args.action == "prepare" and (args.state_dir / "remove-report.json").exists():
        parser.error("This experiment was removed; use another state directory for a new preparation")
    session = boto3.Session()
    backend = AWSPostgreSQL(config, session)
    _, _, outputs = backend.info()
    source = Path(__file__).resolve().parents[2] / "benchmarks/700.analytics/720.sql-postgresql/python/queries.py"
    spec = importlib.util.spec_from_file_location("q21_index_profile_queries", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = {"action": args.action, "namespace": args.namespace, "fingerprint": args.fingerprint,
               "indexes": INDEXES, "query": module.QUERIES["q21"], "region": config.region,
               "bucket": outputs["StagingBucket"]}
    digest = hashlib.sha256((REMOTE + json.dumps(payload, sort_keys=True) + str(args.state_dir.resolve())).encode()).hexdigest()[:20]
    operation = "q21-index-" + args.action + "-" + digest
    payload["key"] = "reports/" + operation + ".json"
    script = remote_script(payload)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    (args.state_dir / (args.action + "-plan.json")).write_text(json.dumps(payload, indent=2) + "\n")
    print("Running recorded index maintenance; data values and Q21 SQL stay unchanged.", flush=True)
    pointer = json.loads(backend.command(operation, script, timeout=3600))
    stream = session.client("s3", region_name=config.region).get_object(Bucket=pointer["bucket"], Key=pointer["key"])["Body"]
    try:
        report = json.loads(stream.read())
    finally:
        stream.close()
    target = args.state_dir / (args.action + "-report.json")
    target.write_text(json.dumps(report, indent=2) + "\n")
    for key in ("action", "steps", "before_table_bytes", "after_table_bytes", "visibility", "elapsed_seconds"):
        print(key + ": " + json.dumps(report[key]))
    def show(node, depth=0):
        print(" " * depth + node["Node Type"] + " " + node.get("Relation Name", "") + " " + node.get("Index Name", ""))
        for child in node.get("Plans", []):
            show(child, depth+1)
    show(report["estimated_plan"][0]["Plan"])
    print("Saved:", target)
    print("No benchmark query executed. Preparation time and index storage are recorded separately.")


if __name__ == "__main__":
    main()
