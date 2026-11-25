"""Driver-container readiness: exact sources, TLS and SF1 metadata, no TPC-H query."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys


def check(root):
    import psycopg
    from psycopg import sql
    from driver_function.sql import sql as SQL
    plan=json.loads((root/'comparison/plan.json').read_text())
    if sys.version_info[:2]!=(3,11) or psycopg.__version__!='3.3.9':
        raise ValueError('Driver must use Python3.11 and Psycopg3.3.9')
    for member,digest in plan['source_sha256'].items():
        path=root/'driver_function'/Path(member).name
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('Driver source differs from native Function package: '+member)
    # Explicit client ID requests the same read-only SQL user-assigned identity
    # attached to the Function and this driver. Artifact access uses System MI.
    os.environ['SEBS_SQL_CONFIG']=json.dumps({'type':'postgresql','provider':'azure',
        'secret_uri':plan['secret_uri'],'identity_client_id':plan['function_identity_client_id']})
    client=SQL();settings,_=client.credentials()
    if settings['sslmode']!='verify-full' or settings['host']!=plan['server_private_ip'] or settings['user']!='sebs_reader':
        raise ValueError('Driver SQL credentials are not the expected private verified-TLS reader')
    with socket.create_connection((settings['host'],settings['port']),timeout=10):pass
    with psycopg.connect(**settings,autocommit=True,prepare_threshold=None,
                         options='-c default_transaction_read_only=on') as connection:
        read_only=connection.execute('SHOW default_transaction_read_only').fetchone()[0]
        fingerprint=connection.execute(sql.SQL('SELECT fingerprint FROM {}._sebs_metadata').format(
            sql.Identifier(plan['namespace']))).fetchone()
        if read_only!='on' or not fingerprint or fingerprint[0]!=plan['input_fingerprint']:
            raise ValueError('Driver read-only policy or SF1 fingerprint differs')
    return {'status':'DRIVER_READY','python':sys.version.split()[0],'psycopg':psycopg.__version__,
        'source_package_matches':True,'private_postgres_tcp':True,'tls_verified':True,
        'read_only':True,'sf1_fingerprint_matches':True,'benchmark_queries_submitted':0,
        'note':'Readiness uses a separate process; measured worker credential caches start separately'}


if __name__=='__main__':
    print(json.dumps(check(Path(__file__).resolve().parents[2])))
