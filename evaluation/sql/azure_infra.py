"""Provision the Azure VM pair and a Synapse SERVERLESS workspace; no SQL runs."""
import argparse
import json
import os
from pathlib import Path
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from evaluation.sql.azure_control import AzureControl
from evaluation.sql.azure_prepare import check_providers

IMAGE = 'Canonical:ubuntu-24_04-lts:cvm:latest'
SIZE = 'Standard_EC2as_v5'


def write(path, value, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, indent=2) + '\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, 'w') as output:
        if private:
            os.fchmod(output.fileno(), 0o600)
        output.write(data)


def bootstrap():
    return '''#!/bin/bash
set -euo pipefail
export HOME=/root
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-dev build-essential libcurl4-openssl-dev libssl-dev curl ca-certificates unixodbc-dev
mkdir -p /opt/sebs-azure
python3 -m venv /opt/sebs-azure/venv
/opt/sebs-azure/venv/bin/pip install duckdb==2.0.0 azure-storage-blob pyodbc requests certifi
# Server and driver application setup follows after provisioning; no queries here.
touch /opt/sebs-azure/bootstrap.complete
'''


def vm_args(plan, role, image, bootstrap_file, key_file):
    args = ['vm', 'create', '--resource-group', plan['resource_group'], '--name', plan[role + '_name'],
        '--location', plan['region'], '--size', plan['vm_size'], '--image', image,
        '--admin-username', 'azureuser', '--ssh-key-values', key_file,
        '--authentication-type', 'ssh',
        '--os-disk-size-gb', '32', '--storage-sku', 'StandardSSD_LRS',
        '--os-disk-delete-option', 'Delete', '--nic-delete-option', 'Delete',
        '--vnet-name', plan['vnet'], '--subnet', 'sql', '--nsg', plan[role + '_nsg'],
        '--nsg-rule', 'NONE', '--private-ip-address', plan[role + '_private_ip'],
        '--public-ip-address', plan[role + '_name'] + '-ip', '--public-ip-sku', 'Standard',
        '--custom-data', bootstrap_file, '--no-wait']
    if plan['vm_security_type'] == 'ConfidentialVM':
        args += ['--security-type', 'ConfidentialVM', '--enable-vtpm', 'true',
                 '--enable-secure-boot', 'true', '--os-disk-security-encryption-type', 'VMGuestStateOnly']
    # For ordinary VMs leave the non-confidential security defaults to the CLI;
    # record the actual securityProfile from Azure in the status response.
    return args


def initial_plan(function):
    suffix = function['function_name'].split('-')[1]
    return {'region': function['region'], 'subscription_id': function['subscription_id'],
        'resource_group': 'sebs-sql-eval-' + suffix,
        'function_resource_group': function['resource_group'],
        'vm_size': SIZE, 'vm_security_type': 'ConfidentialVM', 'image': IMAGE,
        'server_name': 'sebs-sql-server-' + suffix, 'driver_name': 'sebs-sql-driver-' + suffix,
        'server_nsg': 'sebs-sql-server-nsg', 'driver_nsg': 'sebs-sql-driver-nsg',
        'server_private_ip': '10.42.0.4', 'driver_private_ip': '10.42.0.5',
        'server_endpoint': 'http://10.42.0.4:8080', 'vnet': 'sebs-sql-vnet',
        'workspace': 'sebs-sql-' + suffix, 'workspace_storage': 'sebssyn' + suffix,
        'sql_database': 'sebs_sql', 'sql_admin': 'sebsadmin',
        'source_function': function['function_name'],
        'cost_note': 'Two VMs, disks, public IPv4, workspace storage; no dedicated SQL/Spark pool'}



def can_submit(path):
    # Pending/accepted requests require reconciliation, never blind resubmission.
    return not path.exists() or json.loads(path.read_text()).get('status') == 'rejected'


def submit_request(path, identity, invoke):
    if not can_submit(path):
        raise RuntimeError('Request already recorded; inspect provisioning status before resubmitting')
    write(path, {**identity, 'status': 'requested'})
    try:
        invoke()
    except RuntimeError as error:
        # These Azure validation responses explicitly reject the request. A
        # transport timeout remains unknown and keeps the requested marker.
        rejected = next((code for code in ['CustomerSubscriptionNotRegisteredWithSqlRp',
            'ValidationFailed', 'RequestDisallowedByAzure', 'AuthorizationFailed', 'SkuNotAvailable']
            if code in str(error)), None)
        if rejected:
            write(path, {**identity, 'status': 'rejected', 'error_code': rejected})
        raise
    write(path, {**identity, 'status': 'submitted'})


def provision(folder, vm_size=None, security_type=None, image_urn=None):
    path = folder / 'infra.json'
    if path.exists():
        plan = json.loads(path.read_text())
        for field, value in [('vm_size', vm_size), ('vm_security_type', security_type), ('image', image_urn)]:
            if value is not None and plan[field] != value:
                raise ValueError('Existing deployment settings differ; use a fresh state directory')
    else:
        plan = initial_plan(json.loads((folder / 'function.json').read_text()))
        if vm_size:
            plan['vm_size'] = vm_size
        if security_type:
            plan['vm_security_type'] = security_type
        if image_urn:
            plan['image'] = image_urn
        elif plan['vm_security_type'] == 'Standard':
            plan['image'] = 'Canonical:ubuntu-24_04-lts:server:latest'
        write(path, plan)
    secret_path = folder / 'private.json'
    if not secret_path.exists():
        write(secret_path, {'sql_password': 'Aa1!' + secrets.token_urlsafe(32),
                            'service_token': secrets.token_hex(32)}, private=True)
    private = json.loads(secret_path.read_text())
    with AzureControl(plan['subscription_id']) as cli:
        cli.secrets.append(private['sql_password'])
        states = check_providers(cli.run, register=True)
        if any(value != 'Registered' for value in states.values()):
            print('Provider registration is in progress. Wait a few minutes and rerun provision; no new resources requested.')
            return
        image = cli.run(['vm', 'image', 'show', '--location', plan['region'], '--urn', plan.get('resolved_image', plan['image'])])
        if image.get('plan'):
            raise RuntimeError('Image has Marketplace purchase terms; stop before creating billable VMs')
        if image.get('hyperVGeneration') != 'V2':
            raise RuntimeError('This evaluation requires a Gen2 image')
        plan['resolved_image'] = plan['image'].rsplit(':', 1)[0] + ':' + image['name']
        write(path, plan)
        print('Image verified:', plan['resolved_image'], flush=True)
        cli.run(['group', 'create', '--name', plan['resource_group'], '--location', plan['region']])
        # New workspace storage is empty; all measured queries read SeBS's existing input snapshot.
        storage = cli.run(['storage', 'account', 'show', '-g', plan['resource_group'],
                           '-n', plan['workspace_storage']], missing_ok=True)
        if storage is None:
            print('Creating Synapse default storage...', flush=True)
            cli.run(['storage', 'account', 'create', '-g', plan['resource_group'], '-n', plan['workspace_storage'],
                '-l', plan['region'], '--sku', 'Standard_LRS', '--kind', 'StorageV2',
                '--enable-hierarchical-namespace', 'true', '--allow-blob-public-access', 'false'])
        workspace = cli.run(['synapse', 'workspace', 'show', '-g', plan['resource_group'],
                             '-n', plan['workspace']], missing_ok=True)
        if workspace is None and can_submit(folder / 'workspace-request.json'):
            from azure.storage.blob import BlobServiceClient
            connection = cli.run(['storage', 'account', 'show-connection-string', '-g', plan['resource_group'],
                                   '-n', plan['workspace_storage']])['connectionString']
            from azure.core.exceptions import ResourceExistsError
            try:
                BlobServiceClient.from_connection_string(connection).create_container('workspace')
            except ResourceExistsError:
                pass
            account_id = cli.run(['storage', 'account', 'show', '-g', plan['resource_group'],
                                   '-n', plan['workspace_storage']])['id']
            print('Requesting Synapse workspace with built-in SERVERLESS SQL only...', flush=True)
            submit_request(folder / 'workspace-request.json', {'workspace': plan['workspace']},
                lambda: cli.run(['synapse', 'workspace', 'create', '-g', plan['resource_group'], '-n', plan['workspace'],
                    '-l', plan['region'], '--storage-account', account_id, '--file-system', 'workspace',
                    '--sql-admin-login-user', plan['sql_admin'], '--sql-admin-login-password', private['sql_password'],
                    '--no-wait'], json_output=False))
        elif workspace is None:
            raise RuntimeError('Workspace request is pending/unknown but resource is not visible; inspect its status before launching VMs')
        elif workspace.get('provisioningState') in ('Failed', 'Canceled', 'Cancelled'):
            raise RuntimeError('Workspace provisioning failed; inspect the Azure deployment error before launching VMs')
        print('Preparing private VM network...', flush=True)
        cli.run(['network', 'vnet', 'create', '-g', plan['resource_group'], '-n', plan['vnet'],
            '-l', plan['region'], '--address-prefixes', '10.42.0.0/16', '--subnet-name', 'sql',
            '--subnet-prefixes', '10.42.0.0/24'])
        for role in ['server', 'driver']:
            cli.run(['network', 'nsg', 'create', '-g', plan['resource_group'], '-n', plan[role + '_nsg'], '-l', plan['region']])
            cli.run(['network', 'nsg', 'rule', 'create', '-g', plan['resource_group'],
                '--nsg-name', plan[role + '_nsg'], '-n', 'DenyInbound', '--priority', '4000',
                '--direction', 'Inbound', '--access', 'Deny', '--protocol', '*',
                '--source-address-prefixes', '*', '--source-port-ranges', '*',
                '--destination-address-prefixes', '*', '--destination-port-ranges', '*'])
        cli.run(['network', 'nsg', 'rule', 'create', '-g', plan['resource_group'],
            '--nsg-name', plan['server_nsg'], '-n', 'DriverSql', '--priority', '100',
            '--direction', 'Inbound', '--access', 'Allow', '--protocol', 'Tcp',
            '--source-address-prefixes', plan['driver_private_ip'] + '/32', '--source-port-ranges', '*',
            '--destination-address-prefixes', plan['server_private_ip'] + '/32', '--destination-port-ranges', '8080'])
        public = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        key = cli.put_text('public-key.txt', public)
        script = cli.put_text('bootstrap.sh', bootstrap())
        for role in ['server', 'driver']:
            request = folder / (role + '-request.json')
            existing = cli.run(['vm', 'show', '-g', plan['resource_group'], '-n', plan[role + '_name']], missing_ok=True)
            if existing is not None or not can_submit(request):
                print(role + ': launch already recorded; use status, not a duplicate launch', flush=True)
                continue
            print('Requesting ' + role + ' VM (' + plan['vm_size'] + '); consumes account usage...', flush=True)
            submit_request(request, {'vm_name': plan[role + '_name'], 'size': plan['vm_size'], 'image': plan['resolved_image']},
                lambda: cli.run(vm_args(plan, role, plan['resolved_image'], script, key), json_output=False))
        print('Provisioning requested. Run the status command; no SQL queries submitted.', flush=True)


def status(folder):
    plan = json.loads((folder / 'infra.json').read_text())
    with AzureControl(plan['subscription_id']) as cli:
        workspace = cli.run(['synapse', 'workspace', 'show', '-g', plan['resource_group'], '-n', plan['workspace']], missing_ok=True)
        print('Synapse:', workspace.get('provisioningState') if workspace else 'not found yet', flush=True)
        for role in ['server', 'driver']:
            info = cli.run(['vm', 'show', '-g', plan['resource_group'], '-n', plan[role + '_name'], '--show-details'], missing_ok=True)
            if not info:
                print(role + ': not found yet; inspect resource-group deployments if this persists', flush=True)
                continue
            print(role + ':', info.get('provisioningState'), info.get('powerState'),
                  'security=' + ((info.get('securityProfile') or {}).get('securityType') or 'Standard'), flush=True)
            if info.get('provisioningState') == 'Succeeded' and info.get('powerState') == 'VM running':
                script = 'if test -f /opt/sebs-azure/bootstrap.complete; then echo BOOTSTRAP_READY; else echo BOOTSTRAP_PENDING; tail -n 8 /var/log/cloud-init-output.log; fi'
                result = cli.run(['vm', 'run-command', 'invoke', '-g', plan['resource_group'], '-n', plan[role + '_name'],
                    '--command-id', 'RunShellScript', '--scripts', script])
                for value in result.get('value', []):
                    print(value.get('message', ''), flush=True)


def cleanup(folder, apply):
    plan = json.loads((folder / 'infra.json').read_text())
    function = json.loads((folder / 'function.json').read_text())
    expected = initial_plan(function)['resource_group']
    if plan['resource_group'] != expected or plan['resource_group'] == function['resource_group']:
        raise ValueError('Unexpected cleanup group; refusing deletion')
    with AzureControl(plan['subscription_id']) as cli:
        resources = cli.run(['resource', 'list', '-g', plan['resource_group']])
        print('Evaluation group:', plan['resource_group'])
        for resource in resources:
            print(resource['type'], resource['name'])
        print('Preserve local results and collect remote runs before deleting.')
        print('Existing SeBS function/input storage is in a separate group and is retained.')
        if apply:
            cli.run(['group', 'delete', '-n', plan['resource_group'], '--yes', '--no-wait'], json_output=False)
            print('Group deletion requested; verify completion with Azure before assuming all charges stopped.')
        else:
            print('Preview only. Add --apply to delete this evaluation group.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['provision', 'status', 'cleanup'])
    parser.add_argument('--state-dir', type=Path, default=Path('results/sql-azure-comparison'))
    parser.add_argument('--apply', action='store_true', help='Apply cleanup rather than preview it')
    parser.add_argument('--vm-size', help='Selected quota-eligible VM size for a new deployment')
    parser.add_argument('--security-type', choices=['Standard', 'ConfidentialVM'])
    parser.add_argument('--image', help='Override image URN for a new deployment')
    args = parser.parse_args()
    if args.command == 'provision':
        provision(args.state_dir, args.vm_size, args.security_type, args.image)
    elif args.command == 'status':
        status(args.state_dir)
    else:
        cleanup(args.state_dir, args.apply)


if __name__ == '__main__':
    main()
