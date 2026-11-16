"""User-data script for the self-hosted PostgreSQL EC2 server (no embedded secrets)."""
POSTGRES_IMAGE = "postgres:16.16-bookworm@sha256:a49f7438ba03589f9b1ca062f2da82e899b48ea1dc50d2449fa09a70be83ebca"
DRIVER_VERSION = "3.3.9"


def user_data(image=POSTGRES_IMAGE, cpus=2, memory_mb=6144):
    script = """#!/bin/bash
set -euo pipefail
export HOME=/root
umask 077
mkdir -p /opt/sebs-postgres
exec >>/opt/sebs-postgres/bootstrap.log 2>&1
trap 'touch /opt/sebs-postgres/bootstrap.failed' ERR
apt-get update
apt-get install -y docker.io python3-venv openssl curl
systemctl enable --now docker
if ! systemctl is-active --quiet snap.amazon-ssm-agent.amazon-ssm-agent.service; then
    snap install amazon-ssm-agent --classic || snap list amazon-ssm-agent
    systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service
fi
python3 -m venv /opt/sebs-postgres/venv
/opt/sebs-postgres/venv/bin/pip install 'psycopg[binary]==3.3.9' boto3 duckdb==2.0.0
/opt/sebs-postgres/venv/bin/python - <<'BOOTSTRAP'
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.request
import boto3
import psycopg
from psycopg import sql

folder = Path('/opt/sebs-postgres')
request = urllib.request.Request('http://169.254.169.254/latest/api/token',
    method='PUT', headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'})
token = urllib.request.urlopen(request, timeout=5).read().decode()
request = urllib.request.Request('http://169.254.169.254/latest/meta-data/local-ipv4',
    headers={'X-aws-ec2-metadata-token': token})
private_ip = urllib.request.urlopen(request, timeout=5).read().decode()
certs = folder / 'certs'
certs.mkdir(exist_ok=True)
subprocess.run(['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:2048', '-days', '30',
    '-keyout', str(certs/'server.key'), '-out', str(certs/'server.crt'),
    '-subj', '/CN=sebs-postgresql', '-addext', 'subjectAltName=IP:' + private_ip],
    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
# postgres:16-bookworm runs as UID/GID 999.
for name in ['server.key', 'server.crt']:
    os.chown(certs/name, 999, 999)
    os.chmod(certs/name, 0o600 if name.endswith('key') else 0o644)
os.chown(certs, 999, 999)
os.chmod(certs, 0o755)
(certs/'pg_hba.conf').write_text('local all all trust\\nhostssl all all 0.0.0.0/0 scram-sha-256\\n')
os.chmod(certs/'pg_hba.conf', 0o644)
admin_password, reader_password = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
(folder/'postgres.env').write_text('POSTGRES_DB=sebs\\nPOSTGRES_USER=postgres\\nPOSTGRES_PASSWORD=' + admin_password + '\\n')
os.chmod(folder/'postgres.env', 0o600)
subprocess.run(['docker', 'run', '-d', '--name', 'sebs-postgresql', '--restart', 'unless-stopped',
    '--cpus', '2', '--memory', '6g', '--shm-size', '256m',
    '--env-file', str(folder/'postgres.env'), '-p', private_ip + ':5432:5432',
    '-v', 'sebs-postgresql-data:/var/lib/postgresql/data', '-v', str(certs) + ':/certs:ro',
    'postgres:16.16-bookworm@sha256:a49f7438ba03589f9b1ca062f2da82e899b48ea1dc50d2449fa09a70be83ebca', '-c', 'ssl=on', '-c', 'ssl_cert_file=/certs/server.crt',
    '-c', 'ssl_key_file=/certs/server.key', '-c', 'hba_file=/certs/pg_hba.conf',
    '-c', 'shared_buffers=1GB', '-c', 'work_mem=32MB', '-c', 'max_connections=32',
    '-c', 'max_parallel_workers_per_gather=1', '-c', 'password_encryption=scram-sha-256'], check=True)
settings = dict(host=private_ip, port=5432, dbname='sebs', user='postgres', password=admin_password,
                sslmode='verify-full', sslrootcert=str(certs/'server.crt'), connect_timeout=5)
for attempt in range(60):
    try:
        con = psycopg.connect(**settings, autocommit=True)
        break
    except psycopg.OperationalError:
        if attempt == 59:
            raise RuntimeError('PostgreSQL did not become ready') from None
        time.sleep(2)
with con:
    # No login with the superuser is given to the benchmark function.
    con.execute(sql.SQL('CREATE ROLE sebs_reader LOGIN PASSWORD {}').format(sql.Literal(reader_password)))
    con.execute('ALTER ROLE sebs_reader SET default_transaction_read_only=on')
    con.execute('ALTER ROLE sebs_reader SET statement_timeout=240000')
(folder/'admin.json').write_text(json.dumps(settings))
os.chmod(folder/'admin.json', 0o600)
reader = {key: settings[key] for key in ('host', 'port', 'dbname')}
reader.update(user='sebs_reader', password=reader_password, ca_pem=(certs/'server.crt').read_text())
boto3.client('secretsmanager', region_name='${Region}').put_secret_value(
    SecretId='${SecretArn}', SecretString=json.dumps(reader))
image = json.loads(subprocess.check_output(['docker', 'inspect', 'sebs-postgresql']))[0]
(folder/'bootstrap.complete').write_text(json.dumps({'image': image['Image'],
    'configured_image': 'postgres:16.16-bookworm@sha256:a49f7438ba03589f9b1ca062f2da82e899b48ea1dc50d2449fa09a70be83ebca', 'private_ip': private_ip, 'sql_queries_submitted': 0}))
print('POSTGRES_READY; dataset not loaded; no benchmark query executed')
BOOTSTRAP
"""

    script = script.replace(
        "'shared_buffers=1GB'",
        repr("shared_buffers=" + str(min(1024, memory_mb // 4)) + "MB"),
    )
    return script.replace(POSTGRES_IMAGE, image).replace(
        "'--cpus', '2', '--memory', '6g'",
        "'--cpus', " + repr(str(cpus)) + ", '--memory', " + repr(str(memory_mb) + "m"),
    )
