"""Persistent DuckDB query service reusing the SeBS benchmark's SQL.

Run from the repository root: python -m evaluation.sql.service --help
"""
import argparse
import datetime
import decimal
import hmac
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
QUERY_FILE = ROOT / "benchmarks/700.analytics/710.sql-query/python/queries.py"
spec = importlib.util.spec_from_file_location("sebs_baseline_queries", QUERY_FILE)
queries = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queries)


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def json_value(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


def manifest_fingerprint(input_config):
    """Identify the file manifest and scale, excluding irrelevant query selection."""
    identity = {key: input_config.get(key) for key in ("bucket", "tables", "scale_factor")}
    identity["access_mode"] = input_config.get("access_mode", "httpfs")
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class QueryService:
    """Persistent process, fresh DuckDB connection per request.

    Memory limit is per connection, not a total process/container memory cap.
    Local file mode exists for offline smoke tests; AWS comparisons use S3 mode.
    """
    def __init__(self, manifest, workers=2, threads=2, memory_limit="768MB", region="us-east-1"):
        if type(workers) is not int or workers < 1 or type(threads) is not int or threads < 1:
            raise ValueError("workers and threads must be positive integers")
        if not re.fullmatch(r"[1-9][0-9]*(?:MB|GB)", memory_limit):
            raise ValueError("memory-limit must be a positive integer followed by MB or GB")
        self.input = manifest.get("input", manifest)
        if self.input.get("access_mode", "httpfs") not in ("httpfs", "local"):
            raise ValueError("Supported modes are httpfs (S3) and local (offline tests)")
        if not isinstance(self.input.get("tables"), dict) or not self.input["tables"]:
            raise ValueError("Manifest must contain a non-empty tables object")
        for name, files in self.input["tables"].items():
            if name not in {t for tables in queries.TABLES.values() for t in tables}:
                raise ValueError(f"Unknown table: {name}")
            if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
                raise ValueError(f"Expected a non-empty file list for {name}")
        self.extension_directory = str(Path.home() / ".duckdb" / "extensions")
        self.workers, self.threads = workers, threads
        self.memory_limit, self.region = memory_limit, region
        self.slots = threading.BoundedSemaphore(workers)
        self.started = time.time()

    def health(self):
        return {"status": "ok", "duckdb": duckdb.__version__,
                "input_fingerprint": manifest_fingerprint(self.input),
                "workers": self.workers, "threads_per_query": self.threads,
                "memory_limit_per_query": self.memory_limit,
                "connection_policy": "fresh_per_request", "external_file_cache": False,
                "access_mode": self.input.get("access_mode", "httpfs"),
                "scale_factor": self.input.get("scale_factor"),
                "started_at": self.started}

    def check_extensions(self, install=False):
        if self.input.get("access_mode", "httpfs") == "local":
            return
        with duckdb.connect(config={"extension_directory": self.extension_directory}) as con:
            if install:
                con.install_extension("httpfs")
            con.load_extension("httpfs")

    def _storage(self, con):
        import boto3
        # A separate session per request supports EC2 instance-role credentials
        # and avoids sharing a boto3 Session across request threads.
        credentials = boto3.Session().get_credentials()
        if credentials is None:
            raise RuntimeError("AWS credentials unavailable to the query service")
        credentials = credentials.get_frozen_credentials()
        fields = ["TYPE s3", f"KEY_ID {literal(credentials.access_key)}",
                  f"SECRET {literal(credentials.secret_key)}", f"REGION {literal(self.region)}"]
        if credentials.token:
            fields.append(f"SESSION_TOKEN {literal(credentials.token)}")
        con.execute("CREATE SECRET storage (" + ", ".join(fields) + ")")

    def execute(self, query):
        if query not in queries.QUERIES:
            raise ValueError("Unsupported query ID")
        arrived = time.perf_counter()
        with self.slots:
            admitted = time.perf_counter()
            with tempfile.TemporaryDirectory(prefix="sebs-ec2-sql-") as directory:
                begin = time.perf_counter()
                with duckdb.connect(config={
                    "extension_directory": self.extension_directory,
                    "threads": self.threads, "memory_limit": self.memory_limit,
                    "temp_directory": directory, "home_directory": directory,
                    "enable_external_file_cache": False,
                    "autoinstall_known_extensions": False,
                    "autoload_known_extensions": False,
                }) as con:
                    if self.input.get("access_mode", "httpfs") != "local":
                        con.load_extension("httpfs")
                        self._storage(con)
                    connected = time.perf_counter()
                    for table in queries.TABLES[query]:
                        paths = self.input["tables"][table]
                        if self.input.get("access_mode", "httpfs") != "local":
                            bucket = self.input["bucket"]["bucket"]
                            paths = [f"s3://{bucket}/{key}" for key in paths]
                        expression = "[" + ", ".join(literal(path) for path in paths) + "]"
                        con.execute(f"CREATE VIEW {table} AS SELECT * FROM read_parquet({expression})")
                    ready = time.perf_counter()
                    rows = [[json_value(v) for v in row]
                            for row in con.execute(queries.QUERIES[query]).fetchall()]
                    done = time.perf_counter()
                return {"request_id": str(uuid.uuid4()), "query": query,
                        "result": {"queries": {query: rows}},
                        "measurement": {"server_queue_s": admitted-arrived,
                                        "connect_s": connected-begin,
                                        "setup_s": ready-connected, "query_s": done-ready,
                                        "handler_s": time.perf_counter()-arrived}}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service, token=""):
        self.service, self.token = service, token
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Avoid logging request bodies, credentials, and token-bearing headers.
        pass

    def respond(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.server.token and not hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + self.server.token
        ):
            self.respond(401, {"error": "Unauthorized"})
            return False
        return True

    def do_GET(self):
        if self.authorized():
            if self.path == "/health":
                self.respond(200, self.server.service.health())
            else:
                self.respond(404, {"error": "Unknown endpoint"})

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/query":
            self.respond(404, {"error": "Unknown endpoint"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Expected a JSON body of at most 4096 bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {"query"}:
                raise ValueError("Expected exactly one query field")
            query = payload["query"]
            if not isinstance(query, str) or query not in queries.QUERIES:
                raise ValueError("Supported IDs: q1, q3, q6, q12, q21")
        except (ValueError, TypeError):
            self.respond(400, {"error": "Expected {query: <supported query ID>}"})
            return
        try:
            self.respond(200, self.server.service.execute(query))
        except Exception as error:
            # DuckDB errors may embed CREATE SECRET SQL. Do not return the
            # exception message (or log it) to callers.
            self.respond(500, {"error": "Query failed", "error_type": type(error).__name__})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--workers", default=2, type=int)
    parser.add_argument("--threads", default=2, type=int)
    parser.add_argument("--memory-limit", default="768MB")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--install-extensions", action="store_true",
                        help="Install httpfs before serving; do not include this setup in timings")
    args = parser.parse_args()
    token = os.environ.get("SEBS_SQL_SERVICE_TOKEN", "")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        parser.error("Set SEBS_SQL_SERVICE_TOKEN before binding beyond loopback")
    if duckdb.__version__ != "2.0.0":
        parser.error("This baseline requires duckdb==2.0.0")
    service = QueryService(json.loads(args.manifest.read_text()), args.workers,
                           args.threads, args.memory_limit, args.region)
    service.check_extensions(args.install_extensions)
    server = Server((args.host, args.port), service, token)
    print(json.dumps({"listening": f"{args.host}:{args.port}", **service.health()}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
