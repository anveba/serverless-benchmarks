"""Collect a PostgreSQL resource snapshot and non-executing query plans via SSM."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re

import boto3

from sebs.storage.aws_postgresql import AWSPostgreSQL
from sebs.storage.sql_config import sql_config


REMOTE = r'''
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import boto3
import psycopg
from psycopg import sql

request = __PAYLOAD__

def command(args):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=15)
        return {"returncode": result.returncode, "stdout": result.stdout[:12000],
                "stderr": result.stderr[:2000]}
    except Exception as error:
        return {"error_type": type(error).__name__}

report = {"captured_at": datetime.now(timezone.utc).isoformat(),
          "benchmark_queries_executed": 0, "plans_execute_queries": False,
          "fingerprint": request["fingerprint"],
          "note": "Current resource snapshot; database counters are cumulative, not per-run.",
          "namespace": request["namespace"],
          "disk": command(["df", "-h", "/", "/var/lib/docker"]),
          "memory": command(["free", "-m"]),
          "vmstat": command(["vmstat", "1", "3"]),
          "container_stats": command(["docker", "stats", "--no-stream", "--format",
                                      "{{json .}}", "sebs-postgresql"])}
inspect = json.loads(subprocess.check_output(["docker", "inspect", "sebs-postgresql"]))[0]
report["container"] = {
    "oom_killed": inspect["State"]["OOMKilled"], "restart_count": inspect["RestartCount"],
    "memory_limit_bytes": inspect["HostConfig"]["Memory"],
    "memory_swap_limit_bytes": inspect["HostConfig"]["MemorySwap"],
    "cpu_limit_nano": inspect["HostConfig"]["NanoCpus"]}
report["cgroups"] = command(["docker", "exec", "sebs-postgresql", "sh", "-c",
    "for f in cpu.stat memory.events memory.current memory.swap.current io.stat; "
    "do echo $f; cat /sys/fs/cgroup/$f 2>/dev/null || true; done"])

# Read credentials locally on EC2; never include them in the report or stdout.
settings = json.loads(Path("/opt/sebs-postgres/admin.json").read_text())
settings["connect_timeout"] = 10
with psycopg.connect(**settings) as con:
    con.execute("SET TRANSACTION READ ONLY")
    con.execute("SET LOCAL statement_timeout = '10s'")
    con.execute("SET LOCAL lock_timeout = '2s'")
    report["settings"] = {name: {"setting": setting, "unit": unit} for name, setting, unit in con.execute("""SELECT name, setting, unit
        FROM pg_settings WHERE name IN ('shared_buffers', 'work_mem', 'effective_cache_size',
        'max_parallel_workers_per_gather', 'max_connections', 'jit', 'random_page_cost',
        'seq_page_cost', 'effective_io_concurrency')""").fetchall()}
    report["database_bytes"] = con.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    report["database_counters"] = con.execute("""SELECT json_build_object(
        'blocks_read', blks_read, 'blocks_hit', blks_hit, 'temp_files', temp_files,
        'temp_bytes', temp_bytes, 'deadlocks', deadlocks, 'stats_reset', stats_reset)
        FROM pg_stat_database WHERE datname = current_database()""").fetchone()[0]
    report["tables"] = [dict(zip(("table", "estimated_rows", "heap_bytes", "total_bytes"), row))
        for row in con.execute("""SELECT c.relname, c.reltuples,
            pg_relation_size(c.oid), pg_total_relation_size(c.oid)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind = 'r' ORDER BY c.relname""",
            (request["namespace"],)).fetchall()]
    con.execute("SET LOCAL ROLE sebs_reader")
    con.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
        sql.Identifier(request["namespace"])))
    actual = con.execute("SELECT fingerprint FROM _sebs_metadata").fetchone()
    if actual is None or actual[0] != request["fingerprint"]:
        raise RuntimeError("Diagnostic dataset fingerprint differs")
    report["plans"] = {}
    for name, statement in request["queries"].items():
        # EXPLAIN without ANALYZE plans but does not execute these SELECT statements.
        try:
            with con.transaction():
                report["plans"][name] = con.execute(
                    "EXPLAIN (FORMAT JSON) " + statement).fetchone()[0]
        except Exception as error:
            report["plans"][name] = {"error_type": type(error).__name__}

boto3.client("s3", region_name=request["region"]).put_object(
    Bucket=request["bucket"], Key=request["key"],
    Body=json.dumps(report, default=str).encode(), ContentType="application/json")
print(json.dumps({"bucket": request["bucket"], "key": request["key"],
                  "benchmark_queries_executed": 0}))
'''


def probe_script(payload):
    if not re.fullmatch(r"sebs_[a-z0-9_]+", payload["namespace"]):
        raise ValueError("Invalid prepared namespace")
    if not re.fullmatch(r"[a-f0-9]{64}", payload["fingerprint"]):
        raise ValueError("Invalid prepared fingerprint")
    program = REMOTE.replace("__PAYLOAD__", repr(payload))
    return "set -eu\n/opt/sebs-postgres/venv/bin/python - <<'SEBS_DIAGNOSTICS'\n" + program + "\nSEBS_DIAGNOSTICS\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-configuration", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--label", default="sf10-diagnostic-v1")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.label):
        parser.error("label must contain only letters, numbers, underscores and hyphens")
    config = sql_config(json.loads(args.storage_configuration.read_text())["sql"])
    if config.provider != "aws":
        parser.error("This diagnostic targets an existing AWS SQL storage attachment")
    session = boto3.Session()
    backend = AWSPostgreSQL(config, session)
    _, _, outputs = backend.info()
    path = Path(__file__).resolve().parents[2] / "benchmarks/700.analytics/720.sql-postgresql/python/queries.py"
    spec = importlib.util.spec_from_file_location("postgres_diagnostic_queries", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = {"namespace": args.namespace, "fingerprint": args.fingerprint,
               "queries": module.QUERIES, "region": config.region,
               "bucket": outputs["StagingBucket"], "key": "reports/" + args.label + ".json"}
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
    operation = args.label + "-" + identity
    payload["key"] = "reports/" + operation + ".json"
    script = probe_script(payload)
    print("Collecting resource snapshot and estimated plans; no benchmark queries will execute.", flush=True)
    pointer = json.loads(backend.command(operation, script, timeout=180))
    body = session.client("s3", region_name=config.region).get_object(
        Bucket=pointer["bucket"], Key=pointer["key"])["Body"]
    try:
        report = json.loads(body.read())
    finally:
        body.close()
    if report["namespace"] != args.namespace or report["fingerprint"] != args.fingerprint:
        raise RuntimeError("Saved diagnostic belongs to another dataset")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / (args.label + ".json")
    target.write_text(json.dumps(report, indent=2) + "\n")
    for key in ("disk", "memory", "vmstat", "container_stats", "cgroups"):
        print("\n" + key.upper() + "\n" + report[key].get("stdout", "Unavailable"))
    for key in ("container", "settings", "database_bytes", "database_counters", "tables"):
        print(key + ": " + json.dumps(report[key]))
    for name, plan in report["plans"].items():
        print("PLAN " + name + ": " + json.dumps(plan)[:2500])
    print("Saved full report:", target)
    print("No benchmark queries executed; database configuration and data unchanged.")


if __name__ == "__main__":
    main()
