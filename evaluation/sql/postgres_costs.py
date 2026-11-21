"""Collect owned AWS resource metadata and public list prices; never run SQL.

This is cost evidence, not an invoice or a final cost estimate. Snapshot resource
allocation, workload observation cost and total campaign uptime are distinct.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import boto3
from botocore.config import Config

from sebs.storage.aws_postgresql import AWSPostgreSQL
from sebs.storage.sql_config import sql_config


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, default=str) + '\n')
    temporary.replace(path)


def selected(value, fields):
    return {key: value[key] for key in fields if key in value}


def stack_inventory(cf, stack_id):
    stack = cf.describe_stacks(StackName=stack_id)['Stacks'][0]
    resources = [row for page in cf.get_paginator('list_stack_resources').paginate(StackName=stack_id)
                 for row in page['StackResourceSummaries']]
    events = [row for page in cf.get_paginator('describe_stack_events').paginate(StackName=stack_id)
              for row in page['StackEvents']]
    # Exclude parameters, outputs, resource properties and status reasons. These
    # are unnecessary for pricing and can contain deployment configuration.
    return {'stack': selected(stack, ['StackId', 'StackName', 'CreationTime',
                                     'LastUpdatedTime', 'StackStatus']),
            'resources': [selected(r, ['LogicalResourceId', 'PhysicalResourceId',
                                      'ResourceType', 'LastUpdatedTimestamp', 'ResourceStatus']) for r in resources],
            'events': [selected(e, ['EventId', 'Timestamp', 'LogicalResourceId',
                                   'PhysicalResourceId', 'ResourceType', 'ResourceStatus']) for e in events]}


def instance_inventory(ec2, instance_id, owned):
    if not any(r.get('PhysicalResourceId') == instance_id and
               r['ResourceType'] == 'AWS::EC2::Instance' for r in owned['resources']):
        raise ValueError('Instance is not a member of the recorded stack')
    instance = ec2.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]
    volume_ids = [r['Ebs']['VolumeId'] for r in instance.get('BlockDeviceMappings', []) if 'Ebs' in r]
    volumes = ec2.describe_volumes(VolumeIds=volume_ids)['Volumes'] if volume_ids else []
    return {'instance': selected(instance, ['InstanceId', 'InstanceType', 'LaunchTime', 'State',
             'Placement', 'Architecture', 'PlatformDetails', 'UsageOperation', 'PublicIpAddress',
             'RootDeviceName', 'BlockDeviceMappings', 'InstanceLifecycle']),
            'volumes': [selected(v, ['VolumeId', 'VolumeType', 'Size', 'Iops', 'Throughput',
                                     'CreateTime', 'State', 'Attachments']) for v in volumes]}


def price_queries(region, instance_types):
    queries = {f'compute-{kind}': ('AmazonEC2', {'regionCode': region, 'instanceType': kind,
        'operatingSystem': 'Linux', 'tenancy': 'Shared', 'preInstalledSw': 'NA',
        'capacitystatus': 'Used', 'productFamily': 'Compute Instance'}) for kind in sorted(instance_types)}
    queries.update({
        'gp3-storage': ('AmazonEC2', {'regionCode': region, 'usagetype': 'EBS:VolumeUsage.gp3'}),
        'gp3-throughput': ('AmazonEC2', {'regionCode': region, 'usagetype': 'EBS:VolumeP-Throughput.gp3'}),
        'gp3-iops': ('AmazonEC2', {'regionCode': region, 'usagetype': 'EBS:VolumeP-IOPS.gp3'}),
        'lambda': ('AWSLambda', {'regionCode': region}),
        'vpc': ('AmazonVPC', {'regionCode': region}),
        'secrets': ('AWSSecretsManager', {'regionCode': region})})
    # Usage prefixes differ by region. The EBS selectors above are intentionally
    # restricted to this study's region instead of pretending to be portable.
    if region != 'us-east-1':
        raise ValueError('This cost collector currently supports us-east-1 only')
    return queries


def collect_prices(client, queries, directory):
    counts, errors = {}, {}
    for name, (service, filters) in queries.items():
        target = directory / (name + '.json')
        try:
            request = {'ServiceCode': service, 'FormatVersion': 'aws_v1',
                       'Filters': [{'Type': 'TERM_MATCH', 'Field': k, 'Value': v} for k, v in filters.items()]}
            if target.exists():
                result = read(target)
                if result['request'] != request:
                    raise ValueError('Recorded pricing request differs')
            else:
                products = [json.loads(item) for page in client.get_paginator('get_products').paginate(**request)
                            for item in page.get('PriceList', [])]
                result = {'captured_at': datetime.now(timezone.utc).isoformat(), 'request': request,
                          'source': 'AWS Price List Query API GetProducts', 'products': products}
                save(target, result)
            counts[name] = len(result['products'])
            if not counts[name]:
                errors[name] = 'No matching products; do not infer a zero price'
        except Exception as error:
            errors[name] = type(error).__name__ + ': ' + str(error)
    return {'product_counts': counts, 'errors': errors, 'complete': not errors}


def collect(storage_path, driver_dir, output, session=None):
    config = sql_config(read(storage_path)['sql'])
    if config.provider != 'aws' or config.region != 'us-east-1':
        raise ValueError('Use the existing us-east-1 AWS SQL attachment')
    session = session or boto3.Session()
    options = Config(connect_timeout=10, read_timeout=30, retries={'total_max_attempts': 2})
    ec2 = session.client('ec2', region_name=config.region, config=options)
    cf = session.client('cloudformation', region_name=config.region, config=options)
    _, stack, outputs = AWSPostgreSQL(config, session).info()
    driver = read(driver_dir / 'instance.json')
    plan = read(driver_dir / 'preflight.json')
    if driver['region'] != config.region or plan['region'] != config.region:
        raise ValueError('Driver and database regions differ')
    if driver['stack_id'].split(':')[4] != stack['StackId'].split(':')[4]:
        raise ValueError('Driver and database accounts differ')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = output / stamp
    report = {'captured_at': datetime.now(timezone.utc).isoformat(), 'region': config.region,
        'scope': 'Current owned resource allocation, creation history and public prices; no SQL or cloud mutations',
        'uptime_note': 'EC2 LaunchTime is the latest launch/start, not proof of cumulative billable runtime. '
            'Historical restarts/stops, cleanup timestamps and usage meters must be reconciled separately.',
        'price_note': 'Current public catalog; not a historical invoice. Credits, free tiers and discounts not applied.',
        'resources': {}}
    save(folder / 'inventory.json', report)
    types = set()
    for name, stack_id, instance_id in [('database', stack['StackId'], outputs['InstanceId']),
                                       ('driver', driver['stack_id'], driver['instance_id'])]:
        owned = stack_inventory(cf, stack_id)
        item = instance_inventory(ec2, instance_id, owned)
        report['resources'][name] = {**owned, **item}
        types.add(item['instance']['InstanceType'])
        save(folder / 'inventory.json', report)
        print(name + ':', item['instance']['InstanceType'], item['instance']['State']['Name'],
              'disk GiB=' + str(sum(v['Size'] for v in item['volumes'])), flush=True)
    endpoint = ec2.describe_vpc_endpoints(VpcEndpointIds=[outputs['EndpointId']])['VpcEndpoints'][0]
    report['endpoint'] = selected(endpoint, ['VpcEndpointId', 'VpcEndpointType', 'State', 'CreationTimestamp',
                                            'ServiceName', 'SubnetIds', 'NetworkInterfaceIds'])
    secret = session.client('secretsmanager', region_name=config.region, config=options).describe_secret(SecretId=outputs['SecretArn'])
    report['secret'] = selected(secret, ['ARN', 'Name', 'CreatedDate', 'DeletedDate', 'LastChangedDate'])
    function = session.client('lambda', region_name=config.region, config=options).get_function_configuration(FunctionName=plan['lambda_function'])
    report['lambda'] = selected(function, ['FunctionName', 'Runtime', 'Architectures', 'MemorySize',
                                           'Timeout', 'LastModified', 'CodeSha256', 'EphemeralStorage'])
    save(folder / 'inventory.json', report)
    pricing = session.client('pricing', region_name='us-east-1', config=options)
    status = collect_prices(pricing, price_queries(config.region, types), folder / 'pricing')
    save(folder / 'pricing-status.json', status)
    save(output / 'latest.json', {'directory': str(folder.resolve()), 'pricing_complete': status['complete']})
    for name, count in status['product_counts'].items():
        print('Price products', name + ':', count)
    for name, error in status['errors'].items():
        print('Unresolved pricing', name + ':', error)
    print('Saved:', folder)
    print('No SQL queries or cloud mutations. This is cost evidence; dollar report not yet computed.')
    return report, status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['collect', 'report'])
    parser.add_argument('--storage-configuration', type=Path, default=Path('results/postgresql-aws-storage.json'))
    parser.add_argument('--state-dir', type=Path, default=Path('results/postgresql-aws-cloud'))
    parser.add_argument('--output-dir', type=Path, default=Path('results/postgresql-aws-costs'))
    parser.add_argument('--snapshot', type=Path, help='Saved timestamped cost inventory directory; report action only')
    parser.add_argument('--results-dir', type=Path, default=Path('results'))
    parser.add_argument('--throughput-state', type=Path, default=Path('results/postgresql-aws-sf10-disk500'))
    args = parser.parse_args()
    if args.action == 'collect':
        collect(args.storage_configuration, args.state_dir, args.output_dir)
    else:
        from evaluation.sql.postgres_cost_report import generate
        snapshot = args.snapshot
        if snapshot is None:
            snapshot = Path(read(args.output_dir/'latest.json')['directory'])
        generate(snapshot, args.state_dir, args.results_dir, args.throughput_state)


if __name__ == '__main__':
    main()
