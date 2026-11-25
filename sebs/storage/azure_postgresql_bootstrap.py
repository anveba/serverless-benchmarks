"""Managed Run Command bootstrap; credentials are generated only on the VM."""
import json


def bootstrap(config, plan):
    settings={'image':config.image,'cpus':config.cpus,'memory_mb':config.memory_mb,
              'private_ip':plan['server_ip'],'secret_uri':'https://'+plan['vault']+'.vault.azure.net/secrets/sebs-reader'}
    return r'''#!/bin/bash
set -euo pipefail
export HOME=/root
export DEBIAN_FRONTEND=noninteractive
umask 077
mkdir -p /opt/sebs-postgres
if test -f /opt/sebs-postgres/bootstrap.complete; then echo POSTGRES_READY; exit 0; fi
exec >>/opt/sebs-postgres/bootstrap.log 2>&1
trap 'touch /opt/sebs-postgres/bootstrap.failed' ERR
apt-get update
apt-get install -y docker.io python3-venv openssl
systemctl enable --now docker
python3 -m venv /opt/sebs-postgres/venv
/opt/sebs-postgres/venv/bin/pip install 'psycopg[binary]==3.3.9' duckdb==2.0.0 azure-identity azure-storage-blob
/opt/sebs-postgres/venv/bin/python - <<'BOOTSTRAP'
import json, os, secrets, subprocess, time, urllib.request
from pathlib import Path
from azure.identity import ManagedIdentityCredential
import psycopg
from psycopg import sql
request=__SETTINGS__
folder=Path('/opt/sebs-postgres')
certs=folder/'certs'
certs.mkdir(exist_ok=True)
if not (certs/'server.crt').exists():
    subprocess.run(['openssl','req','-x509','-nodes','-newkey','rsa:2048','-days','30',
        '-keyout',str(certs/'server.key'),'-out',str(certs/'server.crt'),
        '-subj','/CN=sebs-postgresql','-addext','subjectAltName=IP:'+request['private_ip']],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
for name in ['server.key','server.crt']:
    os.chown(certs/name,999,999)
    os.chmod(certs/name,0o600 if name.endswith('key') else 0o644)
os.chown(certs,999,999);os.chmod(certs,0o755)
(certs/'pg_hba.conf').write_text('local all all trust\nhostssl all all 0.0.0.0/0 scram-sha-256\n')
os.chmod(certs/'pg_hba.conf',0o644)
passwords=folder/'passwords.json'
if passwords.exists():
    values=json.loads(passwords.read_text())
else:
    values={'admin':secrets.token_urlsafe(40),'reader':secrets.token_urlsafe(40)}
    passwords.write_text(json.dumps(values));os.chmod(passwords,0o600)
(folder/'postgres.env').write_text('POSTGRES_DB=sebs\nPOSTGRES_USER=postgres\nPOSTGRES_PASSWORD='+values['admin']+'\n')
os.chmod(folder/'postgres.env',0o600)
exists=subprocess.run(['docker','inspect','sebs-postgresql'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
if not exists:
    subprocess.run(['docker','run','-d','--name','sebs-postgresql','--restart','unless-stopped',
        '--cpus',str(request['cpus']),'--memory',str(request['memory_mb'])+'m','--shm-size','256m',
        '--env-file',str(folder/'postgres.env'),'-p',request['private_ip']+':5432:5432',
        '-v','sebs-postgresql-data:/var/lib/postgresql/data','-v',str(certs)+':/certs:ro',request['image'],
        '-c','ssl=on','-c','ssl_cert_file=/certs/server.crt','-c','ssl_key_file=/certs/server.key',
        '-c','hba_file=/certs/pg_hba.conf','-c','shared_buffers='+str(min(1024,request['memory_mb']//4))+'MB',
        '-c','work_mem=32MB','-c','max_connections=32','-c','max_parallel_workers_per_gather=1',
        '-c','password_encryption=scram-sha-256'],check=True,stdout=subprocess.DEVNULL)
else:
    subprocess.run(['docker','start','sebs-postgresql'],check=True,stdout=subprocess.DEVNULL)
admin=dict(host=request['private_ip'],port=5432,dbname='sebs',user='postgres',password=values['admin'],
           sslmode='verify-full',sslrootcert=str(certs/'server.crt'),connect_timeout=5)
for attempt in range(60):
    try:
        con=psycopg.connect(**admin,autocommit=True);break
    except psycopg.OperationalError:
        if attempt==59:raise RuntimeError('PostgreSQL not ready') from None
        time.sleep(2)
with con:
    if not con.execute("SELECT 1 FROM pg_roles WHERE rolname='sebs_reader'").fetchone():
        con.execute(sql.SQL('CREATE ROLE sebs_reader LOGIN PASSWORD {}').format(sql.Literal(values['reader'])))
    con.execute('ALTER ROLE sebs_reader SET default_transaction_read_only=on')
    con.execute('ALTER ROLE sebs_reader SET statement_timeout=240000')
(folder/'admin.json').write_text(json.dumps(admin));os.chmod(folder/'admin.json',0o600)
reader={k:admin[k] for k in ('host','port','dbname')}
reader.update(user='sebs_reader',password=values['reader'],ca_pem=(certs/'server.crt').read_text())
credential=ManagedIdentityCredential()
# Role propagation can lag deployment completion. Only secret publication is
# retried; no analytical query is executed or retried by this bootstrap.
for attempt in range(60):
    try:
        token=credential.get_token('https://vault.azure.net/.default').token
        body=json.dumps({'value':json.dumps(reader)}).encode()
        req=urllib.request.Request(request['secret_uri']+'?api-version=7.4',data=body,method='PUT',
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=15) as response:response.read()
        break
    except Exception:
        if attempt==59:raise RuntimeError('Key Vault secret publication failed; inspect managed identity permissions') from None
        time.sleep(5)
(folder/'bootstrap.complete').write_text(json.dumps({'configured_image':request['image'],
    'private_ip':request['private_ip'],'benchmark_queries_submitted':0}))
(folder/'bootstrap.failed').unlink(missing_ok=True)
print('POSTGRES_READY; no benchmark query executed')
BOOTSTRAP
'''.replace('__SETTINGS__',repr(settings))
