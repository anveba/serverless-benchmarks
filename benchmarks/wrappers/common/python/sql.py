# Copyright 2020-2026 ETH Zurich and the SeBS authors. All rights reserved.
"""Shared PostgreSQL runtime wrapper, packaged only for the SQL benchmark module."""
import datetime
import decimal
import hashlib
import json
import os
from pathlib import Path
import re
from threading import Lock
import tempfile
import time

import psycopg

NAMESPACE = re.compile(r"sebs_[a-z0-9_]{1,58}")


def _azure_secret(config):
    """Read Key Vault with the worker/VM managed identity; no SDK or query retry."""
    import urllib.parse
    import urllib.request
    uri = config.get('secret_uri', '')
    if not re.fullmatch(r'https://[a-z0-9-]{3,24}\.vault\.azure\.net/secrets/[A-Za-z0-9-]+(?:/[a-f0-9]+)?',uri):
        raise ValueError('Invalid Azure SQL secret URI')
    client_id = config.get('identity_client_id')
    if client_id and not re.fullmatch(r'[a-fA-F0-9-]{36}',client_id):
        raise ValueError('Invalid Azure SQL managed identity client ID')
    endpoint = os.environ.get('IDENTITY_ENDPOINT')
    if endpoint:
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in ('http','https') or not parsed.hostname or not (
            parsed.hostname in ('localhost','127.0.0.1') or parsed.hostname.startswith('169.254.')):
            raise ValueError('Unexpected Azure managed identity endpoint')
        header = os.environ.get('IDENTITY_HEADER')
        if not header:raise RuntimeError('Azure worker managed identity header is missing')
        params={'api-version':'2019-08-01','resource':'https://vault.azure.net'}
        headers={'X-IDENTITY-HEADER':header}
    else:
        endpoint='http://169.254.169.254/metadata/identity/oauth2/token'
        params={'api-version':'2018-02-01','resource':'https://vault.azure.net'}
        headers={'Metadata':'true'}
    if client_id:params['client_id']=client_id
    url=endpoint+('&' if '?' in endpoint else '?')+urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=10) as response:
        token=json.loads(response.read())['access_token']
    with urllib.request.urlopen(urllib.request.Request(uri+'?api-version=7.4',
            headers={'Authorization':'Bearer '+token}),timeout=10) as response:
        return json.loads(json.loads(response.read())['value'])


def _tls_settings(value, raw):
    if not all(key in value for key in ('host','port','dbname','user','password','ca_pem')):
        raise RuntimeError('PostgreSQL bootstrap is not ready')
    cert=Path('/tmp')/('sebs-sql-'+hashlib.sha256(raw.encode()).hexdigest()+'.crt')
    _publish_ca(cert,value['ca_pem'])
    settings={k:value[k] for k in ('host','port','dbname','user','password')}
    settings.update(sslmode='verify-full',sslrootcert=str(cert))
    return settings


def _publish_ca(path, pem):
    """Publish complete CA files atomically for concurrent clients on one host."""
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(pem)
        # The temporary file is mode 0600, complete and closed before publication.
        # A reader sees either the previous certificate or the complete new one.
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def json_value(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


class sql:
    instance = None

    def __init__(self):
        self._credential_cache = None
        self._connection = None
        self._connection_key = None
        self._lock = Lock()

    @staticmethod
    def get_instance():
        if sql.instance is None:
            sql.instance = sql()
        return sql.instance

    def credentials(self):
        raw = os.environ.get("SEBS_SQL_CONFIG", "")
        if not raw:
            raise RuntimeError(
                "SQL storage is not configured; pass --storage-configuration"
            )
        if self._credential_cache is not None and self._credential_cache[0] == raw:
            return self._credential_cache[1], True
        config = json.loads(raw)
        if config.get("type") != "postgresql":
            raise ValueError("Unsupported SQL runtime backend")
        if config.get('provider') == 'azure':
            settings = _tls_settings(_azure_secret(config), raw)
        elif config.get("secret_arn"):
            # Lazy AWS imports keep the wrapper usable on local and other FaaS systems.
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "secretsmanager",
                region_name=config["region"],
                config=Config(
                    connect_timeout=5,
                    read_timeout=10,
                    retries={"total_max_attempts": 1},
                ),
            )
            value = json.loads(
                client.get_secret_value(SecretId=config["secret_arn"])["SecretString"]
            )
            settings = _tls_settings(value, raw)
        else:
            settings = {
                k: config[k]
                for k in ("host", "port", "dbname", "user", "password", "sslmode")
            }
            if settings["sslmode"] != "disable":
                raise ValueError(
                    "Direct Docker transport requires its explicitly private, non-TLS configuration"
                )
        settings.update(connect_timeout=10, application_name="sebs-sql")
        self._credential_cache = (raw, settings)
        return settings, False

    def close(self):
        if self._connection is not None:
            self._connection.close()
        self._connection = self._connection_key = None

    def query(
        self,
        statement,
        namespace,
        fingerprint,
        policy="fresh",
        statement_timeout_ms=120000,
    ):
        """Execute trusted benchmark SQL read-only, without retrying execution.

        query_time includes protocol time and fetching rows. It is not isolated
        database CPU time. The benchmark, not this wrapper, chooses approved SQL.
        """
        if not isinstance(namespace, str) or not NAMESPACE.fullmatch(namespace):
            raise ValueError("Invalid SQL benchmark namespace")
        if not isinstance(fingerprint, str) or not re.fullmatch(
            r"[a-f0-9]{64}", fingerprint
        ):
            raise ValueError("Invalid prepared dataset fingerprint")
        if (
            policy not in ("fresh", "reuse")
            or type(statement_timeout_ms) is not int
            or not 1 <= statement_timeout_ms <= 240000
        ):
            raise ValueError("Invalid SQL connection policy or statement timeout")
        begin = time.perf_counter()
        con = None
        stage = "credentials"
        with self._lock:
            try:
                start = time.perf_counter()
                settings, hit = self.credentials()
                credential_time = (time.perf_counter() - start) * 1e6
                key = (os.environ.get("SEBS_SQL_CONFIG"), namespace, fingerprint)
                start = time.perf_counter()
                stage = "connect"
                reused = (
                    policy == "reuse"
                    and self._connection is not None
                    and not self._connection.closed
                    and self._connection_key == key
                )
                if reused:
                    con = self._connection
                else:
                    self.close()
                    con = psycopg.connect(
                        **settings,
                        autocommit=True,
                        prepare_threshold=None,
                        options="-c default_transaction_read_only=on -c search_path="
                        + namespace
                        + ",pg_catalog",
                    )
                connect_time = (time.perf_counter() - start) * 1e6
                start = time.perf_counter()
                stage = "dataset check"
                with con.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, false)",
                        (str(statement_timeout_ms),),
                    )
                    cursor.execute("SELECT fingerprint FROM _sebs_metadata")
                    row = cursor.fetchone()
                    if row is None or row[0] != fingerprint:
                        raise ValueError("Prepared SQL dataset differs")
                    setup_time = (time.perf_counter() - start) * 1e6
                    start = time.perf_counter()
                    stage = "query"
                    cursor.execute(statement)
                    rows = cursor.fetchall()
                    query_time = (time.perf_counter() - start) * 1e6
                result = [[json_value(value) for value in row] for row in rows]
                version = con.info.server_version
                if policy == "reuse":
                    self._connection, self._connection_key = con, key
                else:
                    con.close()
                return {
                    "rows": result,
                    "measurement": {
                        "credential_time": credential_time,
                        "connect_time": connect_time,
                        "setup_time": setup_time,
                        "query_time": query_time,
                        "handler_time": (time.perf_counter() - begin) * 1e6,
                    },
                    "engine": "postgresql",
                    "server_version": version,
                    "driver_version": psycopg.__version__,
                    "connection_policy": policy,
                    "connection_reused": reused,
                    "credential_cache_hit": hit,
                }
            except Exception as error:
                if con is not None:
                    con.close()
                self._connection = self._connection_key = self._credential_cache = None
                raise RuntimeError(
                    "SQL request failed at "
                    + stage
                    + " ("
                    + type(error).__name__
                    + "); no query was retried"
                ) from None
