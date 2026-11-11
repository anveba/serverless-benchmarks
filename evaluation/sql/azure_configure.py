"""Configure the existing Azure VM pair asynchronously; never replay benchmark SQL."""
import argparse
import base64
import datetime
import ipaddress
import json
from pathlib import Path
import shlex

from azure.storage.blob import BlobServiceClient, ContainerSasPermissions, generate_container_sas

from evaluation.sql.azure_control import AzureControl
from evaluation.sql.azure_infra import write
from evaluation.sql.launch_driver import make_bundle
from evaluation.sql.service import manifest_fingerprint
from evaluation.sql.synapse_client import table_pattern

BASE = '/opt/sebs-azure'

# Run as root by the VM agent; no credential values are written into source bundles.
INSTALL_PYTHON = r'''import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
from azure.storage.blob import BlobClient

base = Path('/opt/sebs-azure')
payload = json.loads((base / 'configure-payload.json').read_text())
try:
    if not (base / 'bootstrap.complete').exists():
        raise RuntimeError('Initial VM bootstrap has not completed')
    archive = base / 'source.tar.gz'
    blob = BlobClient.from_blob_url(payload['bundle_url'])
    with archive.open('wb') as output:
        blob.download_blob().readinto(output)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != payload['sha256']:
        raise RuntimeError('Source bundle checksum mismatch')
    repo = base / 'repo'
    repo.mkdir(exist_ok=True)
    with tarfile.open(archive, 'r:gz') as source:
        source.extractall(repo, filter='data')
    python = str(base / 'venv/bin/python')
    subprocess.run([python, '-m', 'pip', 'install', '-e', str(repo)], check=True)
    subprocess.run([python, '-c', "import duckdb; duckdb.install_extension('azure', repository_url='https://extensions.duckdb.org'); duckdb.load_extension('azure'); print('Azure extension ready')"], check=True)
    if payload['role'] == 'driver':
        subprocess.run(['curl', '-fsSL', 'https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb', '-o', str(base / 'packages-microsoft-prod.deb')], check=True)
        subprocess.run(['dpkg', '-i', str(base / 'packages-microsoft-prod.deb')], check=True)
        subprocess.run(['apt-get', 'update'], check=True)
        subprocess.run(['apt-get', 'install', '-y', 'msodbcsql18'], check=True,
                       env={**os.environ, 'ACCEPT_EULA': 'Y', 'DEBIAN_FRONTEND': 'noninteractive'})
    private = base / 'private.json'
    fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(payload['private'], output)
    subprocess.run([python, '-m', 'evaluation.sql.azure_remote_setup', '--role', payload['role']],
                   cwd=repo, check=True)
except Exception as error:
    message = str(error)
    for value in [payload.get('bundle_url', ''), *payload.get('private', {}).values()]:
        if isinstance(value, str) and value:
            message = message.replace(value, '[REDACTED]')
    print('INSTALL_FAILED ' + type(error).__name__ + ': ' + message[:2500], flush=True)
    raise SystemExit(1) from None
'''


def verify_blob_snapshot(container, tables):
    """Synapse expands folder wildcards: reject any extra or missing Parquet files."""
    for table, keys in tables.items():
        pattern = table_pattern(keys)
        prefix = pattern[:-len('*.parquet')]
        actual = {blob.name for blob in container.list_blobs(name_starts_with=prefix)
                  if blob.name.endswith('.parquet')}
        if actual != set(keys):
            raise ValueError('Blob folder differs from the saved manifest for table ' + table)


def launch_script(payload):
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    installer = base64.b64encode(INSTALL_PYTHON.encode()).decode()
    return f'''set -eu
umask 077
export HOME=/root
if systemctl is-active --quiet sebs-sql-configure; then
    echo CONFIGURE_ALREADY_RUNNING
    exit 1
fi
mkdir -p {BASE}
/opt/sebs-azure/venv/bin/python - <<'SEBS_WRITE_SETUP'
import base64
from pathlib import Path
base = Path('{BASE}')
(base / 'configure-payload.json').write_bytes(base64.b64decode({encoded!r}))
(base / 'configure-payload.json').chmod(0o600)
(base / 'install.py').write_bytes(base64.b64decode({installer!r}))
(base / 'install.py').chmod(0o600)
(base / 'configure-ready.json').unlink(missing_ok=True)
(base / 'configure.log').write_text('Starting application configuration\\n')
SEBS_WRITE_SETUP
systemd-run --unit sebs-sql-configure --collect \\
  --setenv=HOME=/root --setenv=PYTHONUNBUFFERED=1 \\
  --property=StandardOutput=append:{BASE}/configure.log \\
  --property=StandardError=append:{BASE}/configure.log \\
  {BASE}/venv/bin/python {BASE}/install.py
echo SEBS_CONFIGURE_SUBMITTED
'''


def vm_command(cli, plan, role, script):
    remote = cli.put_text('configure-' + role + '.sh', script)
    result = cli.run(['vm', 'run-command', 'invoke', '-g', plan['resource_group'],
                     '-n', plan[role + '_name'], '--command-id', 'RunShellScript',
                     '--scripts', '@' + remote])
    return '\n'.join(value.get('message', '') for value in result.get('value', []))


def start(folder, retry_failed=False):
    request = folder / 'configure.json'
    if request.exists() and not retry_failed:
        raise RuntimeError('Application setup was already submitted. Run status; do not submit again.')
    function = json.loads((folder / 'function.json').read_text())
    plan = json.loads((folder / 'infra.json').read_text())
    trace = json.loads((folder / 'trace.json').read_text())
    private = json.loads((folder / 'private.json').read_text())
    if function['region'] != plan['region']:
        raise ValueError('Function and infrastructure regions differ')
    fingerprint = manifest_fingerprint(trace['input'])
    if fingerprint != function['input_fingerprint'] or trace['input'].get('access_mode') != 'azure':
        raise ValueError('Prepared trace and function snapshot differ')
    with AzureControl(plan['subscription_id']) as cli:
        cli.secrets.extend(value for value in private.values() if isinstance(value, str))
        workspace = cli.run(['synapse', 'workspace', 'show', '-g', plan['resource_group'], '-n', plan['workspace']])
        if workspace['provisioningState'] != 'Succeeded':
            raise RuntimeError('Synapse workspace is not ready')
        vms = {}
        for role in ['server', 'driver']:
            info = cli.run(['vm', 'show', '-g', plan['resource_group'], '-n', plan[role + '_name'], '--show-details'])
            if info.get('provisioningState') != 'Succeeded' or info.get('powerState') != 'VM running':
                raise RuntimeError(role + ' VM is not running')
            if info['location'] != plan['region']:
                raise ValueError('VM region differs from the input snapshot')
            vms[role] = info
        driver_ip = str(ipaddress.IPv4Address(vms['driver']['publicIps']))
        cli.run(['synapse', 'workspace', 'firewall-rule', 'create', '-g', plan['resource_group'],
            '--workspace-name', plan['workspace'], '--name', 'sebs-cloud-driver',
            '--start-ip-address', driver_ip, '--end-ip-address', driver_ip])
        connection = cli.run(['storage', 'account', 'show-connection-string',
                             '-g', function['resource_group'], '-n', function['storage_account']])['connectionString']
        cli.secrets.append(connection)
        storage = BlobServiceClient.from_connection_string(connection)
        container_name = trace['input']['bucket']['bucket']
        container = storage.get_container_client(container_name)
        verify_blob_snapshot(container, trace['input']['tables'])
        now = datetime.datetime.now(datetime.timezone.utc)
        expires = now + datetime.timedelta(hours=48)
        sas = generate_container_sas(account_name=function['storage_account'], container_name=container_name,
            account_key=storage.credential.account_key, permission=ContainerSasPermissions(read=True, list=True),
            start=now - datetime.timedelta(minutes=5), expiry=expires, protocol='https')
        cli.secrets.append(sas)
        public = {**plan, 'function': function, 'input_fingerprint': fingerprint,
                  'sql_endpoint': workspace['connectivityEndpoints']['sqlOnDemand'],
                  'input_container_url': container.url, 'input_sas_expires_at': expires.isoformat(),
                  'service_workers': 2, 'service_threads_per_query': 2,
                  'service_memory_limit_per_query': '1536MB',
                  'function_resources': 'Consumption host allocation; retain actual returned measurements'}
        private.update(storage_connection_string=connection, input_sas=sas)
        write(folder / 'private.json', private, private=True)
        write(folder / 'comparison.json', public)
        bundle = folder / 'source.tar.gz'
        digest = make_bundle(bundle, public, folder / 'trace.json')
        key = 'evaluation/sql/azure-source/' + digest + '/source.tar.gz'
        source = container.get_blob_client(key)
        with bundle.open('rb') as stream:
            source.upload_blob(stream, overwrite=True)
        bundle_url = source.url + '?' + sas
        cli.secrets.append(bundle_url)
        write(request, {'sha256': digest, 'blob_key': key, 'submitted_at': now.isoformat(), 'roles': {}})
        state = json.loads(request.read_text())
        for role in ['server', 'driver']:
            payload_private = private if role == 'driver' else {
                'storage_connection_string': connection, 'service_token': private['service_token']}
            payload = {'role': role, 'sha256': digest, 'bundle_url': bundle_url, 'private': payload_private}
            state['roles'][role] = 'requested'
            write(request, state)
            print('Submitting ' + role + ' application setup...', flush=True)
            output = vm_command(cli, plan, role, launch_script(payload))
            if 'SEBS_CONFIGURE_SUBMITTED' not in output:
                print(cli.redact(output)[-2500:])
                raise RuntimeError('Setup submission was not confirmed; inspect status before retrying')
            state['roles'][role] = 'submitted'
            write(request, state)
            print(role + ': background setup submitted', flush=True)
        print('Application setup submitted. Run status; no benchmark queries submitted.', flush=True)


def status(folder):
    plan = json.loads((folder / 'infra.json').read_text())
    with AzureControl(plan['subscription_id']) as cli:
        for role in ['server', 'driver']:
            script = '''set -eu
if test -f /opt/sebs-azure/configure-ready.json; then
  /opt/sebs-azure/venv/bin/python -c 'import json; print("SEBS_READY " + json.dumps(json.load(open("/opt/sebs-azure/configure-ready.json"))))'
else
  echo SETUP_NOT_READY
  systemctl show sebs-sql-configure -p ActiveState -p SubState -p Result || true
  tail -n 15 /opt/sebs-azure/configure.log 2>/dev/null || true
fi
'''
            output = vm_command(cli, plan, role, script)
            print(role + ':', flush=True)
            print(output, flush=True)
            for line in output.splitlines():
                if line.startswith('SEBS_READY '):
                    write(folder / (role + '-ready.json'), json.loads(line[len('SEBS_READY '):]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'status'])
    parser.add_argument('--state-dir', type=Path, default=Path('results/sql-azure-sweden'))
    parser.add_argument('--retry-failed', action='store_true',
                        help='Resubmit setup after inspecting its failure; never resubmits benchmark queries')
    args = parser.parse_args()
    if args.command == 'start':
        start(args.state_dir, args.retry_failed)
    else:
        status(args.state_dir)


if __name__ == '__main__':
    main()
