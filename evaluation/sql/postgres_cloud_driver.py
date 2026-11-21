"""Own a separate AWS driver for direct PostgreSQL versus Lambda experiments.

Launch and readiness checks execute no benchmark SQL. All AWS actions are explicit
CLI operations; importing this module is offline. Preserve the state directory.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import tarfile
import uuid
from zipfile import ZipFile

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from evaluation.sql.launch_driver import make_bundle as source_bundle
from evaluation.sql.postgres_cloud_preflight import SOURCES, ROOT, verify_package


PREFIX = Path('/opt/sebs-pg-driver')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + '\n')


def bundle(destination, plan, trace_path, root=ROOT):
    """Reuse the comparison source allowlist; include exact deployed SQL sources.

    The package's Amazon Linux native libraries are not imported on Ubuntu. Its
    Python sources are copied verbatim and its Psycopg version is pinned instead.
    """
    archive = Path(plan['source_archive'])
    verify_package(archive, plan['lambda_code_sha256'], root)
    with ZipFile(archive) as package:
        versions = {part.split('psycopg-')[1].split('.dist-info/')[0]
                    for part in package.namelist()
                    if '/psycopg-' in part and part.endswith('.dist-info/METADATA')}
        if versions != {'3.3.9'}:
            raise ValueError('Expected deployed Psycopg 3.3.9; do not silently change drivers')
        exact = {member.replace('function/', 'driver_function/'): package.read(member)
                 for member in SOURCES}
    source_bundle(destination, plan, trace_path)
    # Repack the existing allowlisted archive to add the verified runtime package.
    intermediate = destination.with_suffix('.source')
    destination.replace(intermediate)
    try:
        with tarfile.open(intermediate, 'r:gz') as source, tarfile.open(destination, 'w:gz') as target:
            for member in source.getmembers():
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
            for name, data in {**exact, 'driver_function/__init__.py': b'',
                    'comparison/preflight.json': json.dumps(plan, indent=2).encode()}.items():
                entry = tarfile.TarInfo(name)
                entry.mode, entry.size = 0o644, len(data)
                target.addfile(entry, io.BytesIO(data))
    finally:
        intermediate.unlink(missing_ok=True)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def bootstrap(bucket, key, digest, region):
    return f'''#!/bin/bash
set -euo pipefail
exec > /var/log/sebs-pg-driver-bootstrap.log 2>&1
export HOME=/root
export AWS_DEFAULT_REGION={region!r}
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-dev build-essential libcurl4-openssl-dev libssl-dev
mkdir -p /opt/sebs-pg-driver
python3 -m venv /opt/sebs-pg-driver/venv
/opt/sebs-pg-driver/venv/bin/pip install boto3
/opt/sebs-pg-driver/venv/bin/python - <<'DOWNLOAD'
import hashlib
from pathlib import Path
import tarfile
import boto3
archive = Path('/opt/sebs-pg-driver/code.tar.gz')
boto3.client('s3', region_name={region!r}).download_file({bucket!r}, {key!r}, str(archive))
if hashlib.sha256(archive.read_bytes()).hexdigest() != {digest!r}:
    raise RuntimeError('Driver bundle checksum mismatch')
repo = Path('/opt/sebs-pg-driver/repo')
repo.mkdir(exist_ok=True)
with tarfile.open(archive, 'r:gz') as package:
    package.extractall(repo, filter='data')
DOWNLOAD
/opt/sebs-pg-driver/venv/bin/pip install -e '/opt/sebs-pg-driver/repo[sql]'
cd /opt/sebs-pg-driver/repo
/opt/sebs-pg-driver/venv/bin/python -B -m evaluation.sql.postgres_cloud_driver verify-local \\
  --state-dir comparison
touch /opt/sebs-pg-driver/bootstrap.complete
'''


def template(plan, artifact, root_device):
    account, partition = plan['secret_arn'].split(':')[4], plan['secret_arn'].split(':')[1]
    function_arn = f"arn:{partition}:lambda:{plan['region']}:{account}:function:{plan['lambda_function']}"
    s3arn = f"arn:{partition}:s3:::{plan['staging_bucket']}/"
    policy = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['secretsmanager:GetSecretValue'], 'Resource': plan['secret_arn']},
        {'Effect': 'Allow', 'Action': ['lambda:InvokeFunction', 'lambda:GetFunctionConfiguration'], 'Resource': function_arn},
        {'Effect': 'Allow', 'Action': ['s3:GetObject'], 'Resource': s3arn + artifact['key']},
        {'Effect': 'Allow', 'Action': ['s3:PutObject'], 'Resource': s3arn + artifact['report_prefix'] + '*'},
    ]}
    return {'AWSTemplateFormatVersion': '2010-09-09',
        'Description': 'SeBS PostgreSQL comparison driver only; database owned separately',
        'Resources': {
            'Role': {'Type': 'AWS::IAM::Role', 'Properties': {
                'AssumeRolePolicyDocument': {'Version': '2012-10-17', 'Statement': [{
                    'Effect': 'Allow', 'Principal': {'Service': 'ec2.amazonaws.com'}, 'Action': 'sts:AssumeRole'}]},
                'ManagedPolicyArns': [f'arn:{partition}:iam::aws:policy/AmazonSSMManagedInstanceCore'],
                'Policies': [{'PolicyName': 'ExactSQLComparison', 'PolicyDocument': policy}]}},
            'Profile': {'Type': 'AWS::IAM::InstanceProfile', 'Properties': {'Roles': [{'Ref': 'Role'}]}},
            'Driver': {'Type': 'AWS::EC2::Instance', 'Properties': {
                'ImageId': plan['driver_ami'], 'InstanceType': plan['driver_instance_type'],
                'IamInstanceProfile': {'Ref': 'Profile'},
                'MetadataOptions': {'HttpTokens': 'required', 'HttpEndpoint': 'enabled'},
                'NetworkInterfaces': [{'DeviceIndex': '0', 'SubnetId': plan['subnet_id'],
                    'GroupSet': [plan['lambda_security_group_id']], 'AssociatePublicIpAddress': True}],
                'BlockDeviceMappings': [{'DeviceName': root_device, 'Ebs': {
                    'VolumeSize': 30, 'VolumeType': 'gp3', 'DeleteOnTermination': True}}],
                'Tags': [{'Key': 'Name', 'Value': 'sebs-postgresql-comparison-driver'}],
                'UserData': {'Fn::Base64': bootstrap(plan['staging_bucket'], artifact['key'], artifact['sha256'], plan['region'])}}}},
        'Outputs': {'InstanceId': {'Value': {'Ref': 'Driver'}}}}


def find_stack(cf, name):
    try:
        return cf.describe_stacks(StackName=name)['Stacks'][0]
    except ClientError as error:
        if error.response['Error']['Code'] == 'ValidationError' and 'does not exist' in error.response['Error']['Message']:
            return None
        raise


def check_owner(stack, request):
    tags = {tag['Key']: tag['Value'] for tag in stack.get('Tags', [])}
    if tags.get('sebs-driver-owner') != request['ClientRequestToken']:
        raise ValueError('Existing stack belongs to another driver; refusing to use or delete it')


def submit_stack(cf, request, folder):
    stack = find_stack(cf, request['StackName'])
    if stack is not None:
        check_owner(stack, request)
        save(folder / 'stack.json', {'stack_id': stack['StackId'], 'stack_name': stack['StackName']})
        print('Existing owned driver stack:', stack['StackStatus'])
        return
    # Request was persisted before any create call; same token/name on every retry.
    if (folder / 'stack.json').exists() or (folder / 'delete-request.json').exists():
        raise ValueError('Driver was previously created/deleted; use a new state directory after inspection')
    response = cf.create_stack(**request)
    save(folder / 'stack.json', {'stack_id': response['StackId'], 'stack_name': request['StackName']})
    print('Driver stack submitted:', response['StackId'])


def launch(folder, trace_path):
    plan = json.loads((folder / 'preflight.json').read_text())
    trace = json.loads(trace_path.read_text())
    if trace['input'] != plan['input'] or trace['settings']['memory'] != plan['lambda_memory_mb']:
        raise ValueError('Trace differs from the verified preflight')
    if plan['quota_headroom_ok'] is False:
        raise ValueError('Driver would exceed the preflight vCPU quota')
    cf = boto3.client('cloudformation', region_name=plan['region'], config=Config(retries={'total_max_attempts': 1}))
    request_path = folder / 'launch-request.json'
    identity = hashlib.sha256(json.dumps({'plan': plan, 'trace': trace}, sort_keys=True).encode()).hexdigest()
    if request_path.exists():
        if json.loads((folder / 'owner.json').read_text())['input_sha256'] != identity:
            raise ValueError('State contains a different launch plan/trace; preserve it and inspect')
        request = json.loads(request_path.read_text())
    else:
        token = uuid.uuid4().hex
        archive = folder / 'driver-code.tar.gz'
        digest = bundle(archive, plan, trace_path)
        artifact = {'bucket': plan['staging_bucket'], 'key': f'evaluation/postgres/driver/{token}/{digest}.tar.gz',
                    'sha256': digest, 'report_prefix': f'reports/postgres-driver/{token}/'}
        ec2 = boto3.client('ec2', region_name=plan['region'])
        image = ec2.describe_images(ImageIds=[plan['driver_ami']])['Images'][0]
        request = {'StackName': 'sebs-pg-driver-' + token[:12], 'ClientRequestToken': token,
                   'Capabilities': ['CAPABILITY_IAM'], 'Tags': [{'Key': 'sebs-driver-owner', 'Value': token}],
                   'TemplateBody': json.dumps(template(plan, artifact, image['RootDeviceName']))}
        # Upload immutable source before submitting an instance which will fetch it.
        boto3.client('s3', region_name=plan['region']).upload_file(str(archive), artifact['bucket'], artifact['key'])
        save(folder / 'artifact.json', artifact)
        save(folder / 'owner.json', {'input_sha256': identity, 'sql_stack_id': plan['stack_id']})
        save(folder / 'trace.json', trace)
        save(request_path, request)
    print('Launching one', plan['driver_instance_type'], 'driver with a 30 GiB disk; AWS usage/charges apply.', flush=True)
    submit_stack(cf, request, folder)
    print('Run status next. No benchmark SQL submitted.')


def status(folder):
    plan = json.loads((folder / 'preflight.json').read_text())
    request = json.loads((folder / 'launch-request.json').read_text())
    cf = boto3.client('cloudformation', region_name=plan['region'])
    stack = find_stack(cf, request['StackName'])
    if stack is None:
        print('Driver stack not found. No resources created by this status command.')
        return
    check_owner(stack, request)
    print('Driver stack:', stack['StackStatus'])
    if stack['StackStatus'] != 'CREATE_COMPLETE':
        for event in cf.describe_stack_events(StackName=stack['StackId'])['StackEvents'][:8]:
            if 'FAILED' in event['ResourceStatus']:
                print(event['LogicalResourceId'], event.get('ResourceStatusReason', ''))
        return
    instance = next(value['OutputValue'] for value in stack['Outputs'] if value['OutputKey'] == 'InstanceId')
    save(folder / 'instance.json', {'instance_id': instance, 'stack_id': stack['StackId'], 'region': plan['region']})
    ssm = boto3.client('ssm', region_name=plan['region'])
    online = ssm.describe_instance_information(Filters=[{'Key': 'InstanceIds', 'Values': [instance]}])['InstanceInformationList']
    if not online or online[0]['PingStatus'] != 'Online':
        print('SSM is not Online yet; rerun status shortly.')
        return
    # Resume the previous read-only inspection if its result was not available.
    marker = folder / 'status-command.json'
    if marker.exists():
        command_id = json.loads(marker.read_text())['command_id']
    else:
        command = "if test -f /opt/sebs-pg-driver/bootstrap.complete; then cat /opt/sebs-pg-driver/ready.json; else echo BOOTSTRAP_NOT_READY; tail -n 12 /var/log/sebs-pg-driver-bootstrap.log 2>/dev/null || true; fi"
        response = ssm.send_command(InstanceIds=[instance], DocumentName='AWS-RunShellScript',
                                   Parameters={'commands': [command]}, TimeoutSeconds=60)
        command_id = response['Command']['CommandId']
        save(marker, {'command_id': command_id})
    try:
        result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance)
    except ssm.exceptions.InvocationDoesNotExist:
        print('Readiness check submitted; rerun status shortly.')
        return
    if result['Status'] in ('Pending', 'InProgress', 'Delayed'):
        print('Readiness check:', result['Status'], '; rerun status shortly.')
        return
    save(folder / 'last-readiness.json', result)
    marker.unlink()
    print(result['StandardOutputContent'])
    if result.get('StandardErrorContent'):
        print(result['StandardErrorContent'])
    print('No benchmark SQL submitted.')


def verify_local(folder):
    """Bootstrap check: verify code, permissions, secret format and TCP reachability.

    It never calls the benchmark handler or executes PostgreSQL statements.
    """
    import os
    import psycopg
    from driver_function import sql
    from driver_function.function import handler  # noqa: F401

    plan = json.loads((folder / 'preflight.json').read_text())
    if sys.version_info[:2] != (3, 12) or psycopg.__version__ != '3.3.9':
        raise ValueError('Driver must use Python 3.12 and Psycopg 3.3.9')
    for member, source in SOURCES.items():
        if (ROOT / member.replace('function/', 'driver_function/')).read_bytes() != (ROOT / source).read_bytes():
            raise ValueError('Driver source differs from the deployed benchmark')
    health = boto3.client('lambda', region_name=plan['region']).get_function_configuration(FunctionName=plan['lambda_function'])
    if health['CodeSha256'] != plan['lambda_code_sha256'] or health['MemorySize'] != plan['lambda_memory_mb']:
        raise ValueError('Lambda changed since preflight; inspect before measurement')
    if health.get('State') != 'Active' or health.get('LastUpdateStatus') != 'Successful':
        raise ValueError('Lambda is not ready')
    os.environ['SEBS_SQL_CONFIG'] = json.dumps({'type': 'postgresql', 'region': plan['region'], 'secret_arn': plan['secret_arn']})
    settings, _ = sql.sql.get_instance().credentials()
    with socket.create_connection((settings['host'], settings['port']), timeout=10):
        pass
    ready = {'status': 'DRIVER_READY', 'region': plan['region'], 'python': sys.version.split()[0],
             'psycopg': psycopg.__version__, 'lambda_source_matches': True, 'private_postgres_tcp': True,
             'benchmark_queries_submitted': 0}
    save(PREFIX / 'ready.json', ready)
    print(json.dumps(ready))


def remove(folder, confirm):
    plan = json.loads((folder / 'preflight.json').read_text())
    request = json.loads((folder / 'launch-request.json').read_text())
    cf = boto3.client('cloudformation', region_name=plan['region'], config=Config(retries={'total_max_attempts': 1}))
    stack = find_stack(cf, request['StackName'])
    if stack is None:
        print('Driver stack absent. Source artifact remains in the SQL stack staging bucket.')
        return
    check_owner(stack, request)
    if not confirm:
        print('Preview only:', stack['StackId'], '; owns driver VM, 30 GiB disk, role and profile.')
        print('Preserve/download results first, then add --delete-driver. Database is separately owned.')
        return
    if stack['StackStatus'] == 'DELETE_IN_PROGRESS':
        print('Driver deletion already in progress.')
        return
    save(folder / 'delete-request.json', {'stack_id': stack['StackId']})
    cf.delete_stack(StackName=stack['StackId'])
    print('Driver deletion requested; rerun status until the stack is absent. Database retained.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['launch', 'status', 'verify-local', 'remove'])
    parser.add_argument('--state-dir', type=Path, default=Path('results/postgresql-aws-cloud'))
    parser.add_argument('--trace', type=Path, default=Path('results/postgresql-aws-sf1-workload/sql-workload/trace.json'))
    parser.add_argument('--delete-driver', action='store_true')
    args = parser.parse_args()
    if args.action == 'launch':
        launch(args.state_dir, args.trace)
    elif args.action == 'status':
        status(args.state_dir)
    elif args.action == 'verify-local':
        verify_local(args.state_dir)
    else:
        remove(args.state_dir, args.delete_driver)


if __name__ == '__main__':
    main()
