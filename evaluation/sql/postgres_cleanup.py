"""Archive billing reports, remove the exact retired Lambda log group, verify IDs.

All resources come from the saved cost inventory. No account-wide deletion, SQL,
provisioning or secret-value retrieval. The finish action deletes only that log
group after the Lambda is absent and the report archive has been saved locally.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from evaluation.sql.postgres_costs import read, save


def find_group(logs, name):
    for page in logs.get_paginator('describe_log_groups').paginate(logGroupNamePrefix=name):
        for group in page.get('logGroups', []):
            if group['logGroupName'] == name:
                return group
    return None


def probe(call, absent_codes, state):
    try:
        return state(call())
    except ClientError as error:
        code = error.response['Error']['Code']
        return 'absent' if code in absent_codes else 'ERROR:' + code


def archive_and_delete(logs, lam, function, folder):
    absent = probe(lambda:lam.get_function_configuration(FunctionName=function),
                   ['ResourceNotFoundException'], lambda _: 'present')
    if absent != 'absent':
        raise ValueError('Lambda is not confirmed absent; no log deletion: ' + absent)
    name = '/aws/lambda/' + function
    group = find_group(logs, name)
    if group is None:
        print('Lambda log group already absent.')
        return
    identity = {'log_group':name, 'creation_time':group['creationTime'], 'arn':group['arn']}
    archive, marker = folder/'lambda-billing-reports.json', folder/'archive-complete.json'
    if marker.exists():
        previous = read(marker)
        if previous['identity'] != identity or hashlib.sha256(archive.read_bytes()).hexdigest() != previous['sha256']:
            raise ValueError('Archive identity/hash differs; no deletion')
    else:
        events = {}
        for page in logs.get_paginator('filter_log_events').paginate(logGroupName=name,
                filterPattern='"REPORT RequestId:"'):
            for event in page.get('events', []):
                # Preserve only native billing lines; no application log bodies.
                if event['message'].lstrip().startswith('REPORT RequestId:'):
                    events[event['eventId']] = event
        save(archive, {'identity':identity, 'captured_at':datetime.now(timezone.utc).isoformat(),
            'scope':'Native REPORT lines only, all currently retained log streams',
            'events':sorted(events.values(), key=lambda e:(e['timestamp'],e['eventId']))})
        save(marker, {'identity':identity, 'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
                      'reports':len(events)})
    # Detect replacement of the group while the archive was being collected.
    current = find_group(logs, name)
    if current is None:
        print('Log group disappeared after archiving; no deletion needed.')
        return
    if current['creationTime'] != identity['creation_time'] or current['arn'] != identity['arn']:
        raise ValueError('Log group identity changed; no deletion')
    print('Native billing reports saved:',read(marker)['reports'],flush=True)
    logs.delete_log_group(logGroupName=name)
    save(folder/'log-deletion.json', {'requested_at':datetime.now(timezone.utc).isoformat(), 'identity':identity})
    print('Deleted exact log group:',name)


def verify(session, inventory, region, function, resources_id, folder):
    options = Config(connect_timeout=10, read_timeout=30, retries={'total_max_attempts':1})
    clients = {name:session.client(name, region_name=region, config=options)
               for name in ['cloudformation','ec2','lambda','s3','logs','secretsmanager']}
    cf, ec2 = clients['cloudformation'], clients['ec2']
    findings = {}
    for name, resource in inventory['resources'].items():
        stack_id = resource['stack']['StackId']
        try:
            status = cf.describe_stacks(StackName=stack_id)['Stacks'][0]['StackStatus']
            findings[name+'_stack'] = 'absent' if status=='DELETE_COMPLETE' else status
        except ClientError as error:
            code = error.response['Error']['Code']
            message = error.response['Error'].get('Message','')
            findings[name+'_stack'] = 'absent' if code=='ValidationError' and 'does not exist' in message else 'ERROR:'+code
        instance_id = resource['instance']['InstanceId']
        findings[name+'_instance'] = probe(
            lambda:ec2.describe_instances(InstanceIds=[instance_id]), ['InvalidInstanceID.NotFound'],
            lambda r: 'absent' if not r['Reservations'] else ('absent' if r['Reservations'][0]['Instances'][0]['State']['Name']=='terminated'
                                                            else r['Reservations'][0]['Instances'][0]['State']['Name']))
        for volume in resource['volumes']:
            volume_id = volume['VolumeId']
            findings[volume_id] = probe(lambda:ec2.describe_volumes(VolumeIds=[volume_id]),
                ['InvalidVolume.NotFound'], lambda r:'present' if r['Volumes'] else 'absent')
    findings['private_endpoint'] = probe(lambda:ec2.describe_vpc_endpoints(VpcEndpointIds=[inventory['endpoint']['VpcEndpointId']]),
        ['InvalidVpcEndpointId.NotFound'], lambda r:'absent' if all(e['State']=='deleted' for e in r['VpcEndpoints']) else 'present')
    findings['database_secret'] = probe(lambda:clients['secretsmanager'].describe_secret(SecretId=inventory['secret']['ARN']),
        ['ResourceNotFoundException'], lambda r:'scheduled_for_deletion' if r.get('DeletedDate') else 'present')
    findings['lambda_function'] = probe(lambda:clients['lambda'].get_function_configuration(FunctionName=function),
        ['ResourceNotFoundException'], lambda r:'present')
    findings['benchmark_bucket'] = probe(lambda:clients['s3'].head_bucket(Bucket='sebs-benchmarks-'+resources_id),
        ['404','NoSuchBucket','NotFound'], lambda r:'present')
    findings['lambda_logs'] = 'present' if find_group(clients['logs'],'/aws/lambda/'+function) else 'absent'
    report = {'checked_at':datetime.now(timezone.utc).isoformat(), 'findings':findings,
        'all_checked_resources_removed':all(v=='absent' for v in findings.values()),
        'scope':'Recorded driver/database stacks, instances, volumes, private endpoint, secret, Lambda, benchmark bucket and Lambda logs only. '
                'Other deployments/account resources and historical charges are outside this check.'}
    save(folder/'verification.json',report)
    for name,state in findings.items():
        print(name + ':',state)
    print('All checked experiment resources removed:',report['all_checked_resources_removed'])
    print('Saved:',folder/'verification.json')
    return report


def run(snapshot, folder, finish, session=None):
    inventory = read(snapshot/'inventory.json')
    function = inventory['lambda']['FunctionName']
    match = re.fullmatch(r'sebs_([a-f0-9]{8})_720_python_3_12_x64',function)
    if not match:
        raise ValueError('Expected the recorded SeBS PostgreSQL function name')
    account = inventory['resources']['database']['stack']['StackId'].split(':')[4]
    region = inventory['region']
    session = session or boto3.Session()
    if session.client('sts',region_name=region).get_caller_identity()['Account'] != account:
        raise ValueError('AWS account differs from saved inventory')
    if inventory['resources']['driver']['stack']['StackId'].split(':')[4] != account:
        raise ValueError('Driver account differs from saved inventory')
    folder.mkdir(parents=True, exist_ok=True)
    if finish:
        options = Config(connect_timeout=10,read_timeout=30,retries={'total_max_attempts':1})
        archive_and_delete(session.client('logs',region_name=region,config=options),
            session.client('lambda',region_name=region,config=options),function,folder)
    return verify(session,inventory,region,function,match[1],folder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['finish','verify'])
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,default=Path('results/postgresql-cleanup-final'))
    args=parser.parse_args()
    result=run(args.snapshot,args.output_dir,args.action=='finish')
    if not result['all_checked_resources_removed']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
