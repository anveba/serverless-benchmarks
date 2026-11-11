"""Prepare the shared Azure SF1 snapshot/trace without executing SQL queries."""
import argparse
import copy
import json
import os
from pathlib import Path

from sebs.sebs import SeBS
from sebs.azure.cli import AzureCLI
from sebs.config import SeBSConfig

ORDER = ['q6', 'q1', 'q12', 'q3', 'q21']


def make_trace(input_config, count=30):
    if type(count) is not int or count < 5 or count % 5:
        raise ValueError('count must be a positive multiple of five')
    if input_config.get('scale_factor') != 1 or input_config.get('access_mode') != 'azure':
        raise ValueError('The comparison requires SF1 with native Azure access')
    requests = [{'at': i * 2, 'query': ORDER[i % 5]} for i in range(count)]
    return {'schema_version': 1, 'input': copy.deepcopy(input_config), 'scale_factor': 1,
        'requests': requests,
        'settings': {'benchmark': '710.sql-query', 'input-size': 'small',
                     'max-in-flight': 2, 'requests': copy.deepcopy(requests)},
        'validation_in_timing': False, 'warmup': False, 'automatic_retries': False}


def prepare(config_path, cache, output, count, deploy_function=False):
    config = json.loads(config_path.read_text())
    if config['deployment']['name'] != 'azure':
        raise ValueError('An Azure configuration is required')
    requested_region = config['deployment']['azure'].get('region') if 'azure' in config['deployment'] else None
    trace_path = output / 'trace.json'
    plan_path = output / 'function.json'
    if trace_path.exists() and plan_path.exists():
        trace = json.loads(trace_path.read_text())
        if len(trace['requests']) != count:
            raise ValueError('Prepared trace has a different count; use another output directory')
        plan = json.loads(plan_path.read_text())
        if requested_region and plan['region'] != requested_region:
            raise ValueError('Prepared state uses another region; use a separate cache and output directory')
        print('Reusing prepared trace:', trace_path)
        return plan
    # Explicitly remove the Q6 smoke-only selection; no query is submitted here.
    config['experiments'].setdefault('benchmark-inputs', {})['710.sql-query'] = {'access_mode': 'azure'}
    output.mkdir(parents=True, exist_ok=True)
    with SeBS(str(cache), str(output / 'prepare'), verbose=False) as sebs:
        deployment = sebs.get_deployment(config)
        try:
            deployment.initialize()
            functions = {k: v for k, v in sebs.cache_client.get_all_functions('azure').items()
                         if v.get('benchmark') == '710.sql-query'}
            if requested_region and deployment.config.region != requested_region:
                raise ValueError('Cached Azure region overrides config; choose a fresh regional cache')
            if len(functions) > 1 or (not functions and not deploy_function):
                raise ValueError('Expected one deployed SQL function, or pass --deploy-function with a fresh regional cache')
            settings = sebs.get_experiment_config(config['experiments'])
            benchmark = sebs.get_benchmark('710.sql-query', deployment, settings)
            data = benchmark.prepare_input(deployment.system_resources, 'small', replace_existing=True)
            if deploy_function:
                benchmark.benchmark_config.memory = config['experiments'].get('sql-workload', {}).get('memory', 1536)
                deployed = deployment.get_function(benchmark)
                name, function = deployed.name, deployed.serialize()
            else:
                name, function = next(iter(functions.items()))
            endpoints = [t['url'] for t in function['triggers'] if t['type'] == 'HTTP']
            if len(endpoints) != 1:
                raise ValueError('Expected one HTTP trigger')
            trace = make_trace(data, count)
            plan = {'provider': 'azure', 'region': deployment.config.region,
                    'subscription_id': deployment.config.credentials.subscription_id,
                    'resource_group': deployment.config.resources.resource_group(deployment.cli_instance),
                    'function_name': name, 'function_endpoint': endpoints[0],
                    'function_code_hash': function['hash'],
                    'storage_account': deployment.config.resources.data_storage_account(deployment.cli_instance).account_name,
                    'input_fingerprint': None, 'sql_invocations_submitted': 0}
            from evaluation.sql.service import manifest_fingerprint
            plan['input_fingerprint'] = manifest_fingerprint(data)
            trace_path.write_text(json.dumps(trace, indent=2) + '\n')
            plan_path.write_text(json.dumps(plan, indent=2) + '\n')
            print(f"Prepared SF1: {sum(len(v) for v in data['tables'].values())} Parquet objects, {count} arrivals, 2 slots")
            print('Saved:', trace_path, plan_path)
            print('No SQL queries submitted; ' + ('function deployed/reused through SeBS.' if deploy_function else 'function not redeployed.'))
            return plan
        finally:
            deployment.shutdown()


def check_providers(az, register=False):
    states = {}
    for namespace in ['Microsoft.Compute', 'Microsoft.Network', 'Microsoft.Synapse', 'Microsoft.Sql']:
        info = az(['provider', 'show', '--namespace', namespace, '--output', 'json'])
        state = info['registrationState']
        if register and state not in ('Registered', 'Registering'):
            print('Requesting provider registration:', namespace, flush=True)
            az(['provider', 'register', '--namespace', namespace, '--output', 'none'], False)
            state = az(['provider', 'show', '--namespace', namespace, '--output', 'json'])['registrationState']
        states[namespace] = state
        print(namespace + ':', state, flush=True)
    return states


def eligible_vm_candidates(skus, region):
    candidates = []
    for sku in skus:
        if sku.get('resourceType') != 'virtualMachines':
            continue
        caps = {item['name']: item['value'] for item in sku.get('capabilities', [])}
        if caps.get('CpuArchitectureType', 'x64').lower() != 'x64':
            continue
        try:
            cpus, memory = float(caps.get('vCPUs', 0)), float(caps.get('MemoryGB', 0))
        except ValueError:
            continue
        if cpus != 2 or not 4 <= memory <= 16:
            continue
        # Zone restrictions do not prohibit a regional deployment without --zone.
        location_restricted = False
        for restriction in sku.get('restrictions', []):
            if restriction.get('type') != 'Location':
                continue
            locations = restriction.get('restrictionInfo', {}).get('locations') or restriction.get('values', [])
            if not locations or region.lower() in [name.lower() for name in locations]:
                location_restricted = True
        if location_restricted:
            continue
        candidates.append({'name': sku['name'], 'family': sku.get('family'),
            'vcpus': int(cpus), 'memory_gib': memory,
            'burstable': sku['name'].startswith('Standard_B')})
    return sorted(candidates, key=lambda s: (s['burstable'], s['memory_gib'], s['name']))


def preflight(plan, register=False):
    import docker
    required = ['AZURE_SECRET_APPLICATION_ID', 'AZURE_SECRET_TENANT', 'AZURE_SECRET_PASSWORD']
    if not all(os.environ.get(k) for k in required):
        raise ValueError('Export the three SeBS Azure credential variables in this terminal')
    cli = AzureCLI(SeBSConfig(), docker.from_env())
    secret = os.environ['AZURE_SECRET_PASSWORD']

    def az(arguments, expect_json=True):
        status, (stdout, stderr) = cli.docker_instance.exec_run(
            ['az', *arguments, '--only-show-errors'], user='docker_user', demux=True)
        if status:
            message = (stderr or b'').decode(errors='replace').replace(secret, '[REDACTED]')
            raise RuntimeError(f'Azure preflight command failed: {message}')
        return json.loads(stdout) if expect_json else None

    try:
        az(['login', '--service-principal', '--username', os.environ[required[0]],
            '--tenant', os.environ[required[1]], '--password=' + secret, '--output', 'none'], False)
        az(['account', 'set', '--subscription', plan['subscription_id']], False)
        states = check_providers(az, register)
        if states['Microsoft.Compute'] != 'Registered':
            print('Compute registration is not complete. Wait a few minutes and rerun this command; saved data is reused.')
            return
        print('Checking all two-vCPU x64 VM alternatives in ' + plan['region'] + '...', flush=True)
        skus = az(['vm', 'list-skus', '--location', plan['region'],
                   '--resource-type', 'virtualMachines', '--all', '--output', 'json'])
        candidates = eligible_vm_candidates(skus, plan['region'])
        for candidate in candidates:
            print('Eligible regional VM:', json.dumps(candidate), flush=True)
        if not candidates:
            print('No unrestricted 2-vCPU x64 size with 4–16 GiB found in this region.')
        usage = az(['vm', 'list-usage', '--location', plan['region'], '--output', 'json'])
        families = {str(s['family']).lower() for s in candidates}
        for item in usage:
            name = item['name']['value']
            if name.lower() == 'cores' or name.lower() in families:
                print('Quota:', name, 'used=', item['currentValue'], 'limit=', item['limit'], flush=True)
        if not usage:
            print('No compute quota data returned; registration may still be propagating. Retry later.')
        print('Preflight complete. No VMs or Synapse workspaces created.')
    finally:
        cli.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('configs/sql-azure.json'))
    parser.add_argument('--cache', type=Path, default=Path('cache-sql-azure-northeurope'))
    parser.add_argument('--output-dir', type=Path, default=Path('results/sql-azure-comparison'))
    parser.add_argument('--count', type=int, default=30)
    parser.add_argument('--register-providers', action='store_true',
                        help='Register Compute, Network, Synapse and SQL for this subscription; creates no VMs/workspace')
    parser.add_argument('--deploy-function', action='store_true',
                        help='Deploy/reuse the function through SeBS without invoking SQL')
    parser.add_argument('--skip-preflight', action='store_true',
                        help='Skip repeated availability checks after checking this region separately')
    args = parser.parse_args()
    if args.count < 5 or args.count % 5:
        parser.error('count must be a positive multiple of five')
    plan = prepare(args.config, args.cache, args.output_dir, args.count, args.deploy_function)
    if not args.skip_preflight:
        preflight(plan, args.register_providers)


if __name__ == '__main__':
    main()
