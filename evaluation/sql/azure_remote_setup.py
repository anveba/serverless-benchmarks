"""VM-side setup only: install service and Synapse metadata, never replay queries."""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from evaluation.sql.service import manifest_fingerprint
from evaluation.sql.synapse_client import COLLATION, connect, identifier, literal, view_sql

BASE = Path('/opt/sebs-azure')
REPO = BASE / 'repo'


def save_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, indent=2)


def setup_synapse(state, private, trace):
    database = state['sql_database']
    identifier(database)
    con = connect(state, private, 'master')
    try:
        cursor = con.cursor()
        cursor.execute('SELECT name FROM sys.databases WHERE name = ?', database)
        if cursor.fetchone() is None:
            cursor.execute('CREATE DATABASE ' + identifier(database) + ' COLLATE ' + COLLATION)
    finally:
        con.close()
    con = connect(state, private)
    try:
        cursor = con.cursor()
        cursor.execute("SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS varchar(128))")
        if cursor.fetchone()[0] != COLLATION:
            raise RuntimeError('Existing SQL database has a different collation')
        cursor.execute("SELECT name FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##'")
        if cursor.fetchone() is None:
            cursor.execute('CREATE MASTER KEY ENCRYPTION BY PASSWORD = ' + literal(private['sql_password']))
        cursor.execute("SELECT name FROM sys.database_scoped_credentials WHERE name = 'sebs_blob'")
        operation = 'ALTER' if cursor.fetchone() is not None else 'CREATE'
        cursor.execute(operation + " DATABASE SCOPED CREDENTIAL sebs_blob WITH IDENTITY = "
                       "'SHARED ACCESS SIGNATURE', SECRET = " + literal(private['input_sas'].lstrip('?')))
        cursor.execute("SELECT location FROM sys.external_data_sources WHERE name = 'sebs_input'")
        row = cursor.fetchone()
        if row is None:
            cursor.execute('CREATE EXTERNAL DATA SOURCE sebs_input WITH (LOCATION = ' +
                           literal(state['input_container_url']) + ', CREDENTIAL = sebs_blob)')
        elif row[0].rstrip('/') != state['input_container_url'].rstrip('/'):
            raise RuntimeError('Existing external data source uses a different container')
        for table, keys in trace['input']['tables'].items():
            cursor.execute(view_sql(table, keys))
            print('Created Synapse view:', table, flush=True)
    finally:
        con.close()
    print('Synapse database, credential and five views configured. No TPC-H query executed.', flush=True)


def configure_server(plan, private):
    service_env = ('STORAGE_CONNECTION_STRING=' + private['storage_connection_string'] + '\n'
                   'SEBS_SQL_SERVICE_TOKEN=' + private['service_token'] + '\n')
    env_file = BASE / 'service.env'
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(service_env)
    # Explicit HOME gives the service's Azure extension a stable installation path.
    unit = '''[Unit]
Description=SeBS Azure persistent DuckDB comparison service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/sebs-azure/repo
Environment=HOME=/root
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/opt/sebs-azure/service.env
ExecStart=/opt/sebs-azure/venv/bin/python -m evaluation.sql.service --manifest /opt/sebs-azure/repo/comparison/trace.json --host 0.0.0.0 --workers 2 --threads 2 --memory-limit 1536MB
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
'''
    Path('/etc/systemd/system/sebs-sql.service').write_text(unit)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'sebs-sql.service'], check=True)
    subprocess.run(['systemctl', 'restart', 'sebs-sql.service'], check=True)
    from evaluation.sql.replay_service import ServiceClient, verify_snapshot
    client = ServiceClient('http://127.0.0.1:8080', private['service_token'])
    trace = json.loads((REPO / 'comparison/trace.json').read_text())
    for attempt in range(30):
        try:
            health = client.health()
            verify_snapshot(health, trace)
            return {'service': health}
        except OSError:
            if attempt == 29:
                raise
            time.sleep(1)


def configure_driver(plan, private):
    import pyodbc
    if 'ODBC Driver 18 for SQL Server' not in pyodbc.drivers():
        raise RuntimeError('ODBC Driver 18 is missing')
    trace = json.loads((REPO / 'comparison/trace.json').read_text())
    from evaluation.sql.replay_service import ServiceClient, verify_snapshot
    from evaluation.sql.synapse_client import SynapseClient
    client = ServiceClient(plan['server_endpoint'], private['service_token'])
    for attempt in range(60):
        try:
            service_health = client.health()
            break
        except OSError:
            if attempt == 59:
                raise
            time.sleep(2)
    verify_snapshot(service_health, trace)
    setup_synapse(plan, private, trace)
    synapse = SynapseClient(plan, private)
    health = synapse.health()
    synapse.verify(health, trace)
    # Reuse SeBS reference-answer validation; install tpch before timed replay.
    import duckdb
    # Match the validator's INSTALL tpch repository, including cached-origin metadata.
    duckdb.install_extension('tpch')
    duckdb.load_extension('tpch')
    # This is recorded public metadata, never a secret-bearing connection string.
    return {'private_service': service_health, 'synapse': health, 'odbc_drivers': pyodbc.drivers()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', required=True, choices=['server', 'driver'])
    args = parser.parse_args()
    private = json.loads((BASE / 'private.json').read_text())
    plan = json.loads((REPO / 'comparison/private-driver.json').read_text())
    try:
        outcome = configure_server(plan, private) if args.role == 'server' else configure_driver(plan, private)
        ready = {'role': args.role, 'configured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 'region': plan['region'], 'input_fingerprint': plan['input_fingerprint'],
                 'benchmark_queries_submitted': 0, **outcome}
        (BASE / 'configure-ready.json').write_text(json.dumps(ready, indent=2) + '\n')
        print('CONFIGURE_READY ' + json.dumps(ready), flush=True)
    except Exception as error:
        # ODBC/SQL errors can echo the failing statement and embedded SAS token.
        message = str(error)
        for value in private.values():
            if isinstance(value, str) and value:
                message = message.replace(value, '[REDACTED]')
        print('SETUP_FAILED ' + type(error).__name__ + ': ' + message[:2500], flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
