"""Read-only verification of recorded Azure PostgreSQL experiment resources."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


def targets(plan, registry, inventory):
    identifiers = {plan[k] for k in ('function_app_id', 'server_vm_id', 'driver_vm_id')}
    for entry in registry.values():
        identifiers.add(entry['app_id'])
        identifiers.update(entry.get('resource_ids', []))
    identifiers.update(r['id'] for r in inventory if r.get('id'))
    result = {}
    for resource_id in sorted(identifiers):
        parts = resource_id.strip('/').split('/')
        if (len(parts) < 8 or parts[0].lower() != 'subscriptions' or
                parts[1].lower() != plan['subscription'].lower() or parts[2].lower() != 'resourcegroups'):
            raise ValueError('Recorded resource is outside the expected subscription')
        result[resource_id.lower()] = {'id': resource_id, 'group': parts[3]}
    return list(result.values())


def verify(cli, expected):
    groups, checks, retained = {}, [], []
    for group in sorted({r['group'].lower() for r in expected}):
        try:
            info = cli.run(['group', 'show', '--name', group], missing_ok=True)
            groups[group] = {'absent': info is None, 'error': None}
            if info is not None:
                existing = cli.run(['resource', 'list', '-g', group])
                known = {r['id'].lower() for r in expected}
                retained.extend({k: r.get(k) for k in ('id', 'name', 'type', 'location')}
                                for r in existing if r['id'].lower() not in known)
        except RuntimeError as error:
            groups[group] = {'absent': None, 'error': str(error)}
    for resource in expected:
        group = groups[resource['group'].lower()]
        if group['absent'] is True:
            checks.append({**resource, 'absent': True, 'evidence': 'Parent resource group absent', 'error': None})
            continue
        try:
            info = cli.run(['resource', 'show', '--ids', resource['id']], missing_ok=True)
            checks.append({**resource, 'absent': info is None, 'evidence': 'Exact resource lookup', 'error': None})
        except RuntimeError as error:
            checks.append({**resource, 'absent': None, 'error': str(error)})
    return {'checked_at': datetime.now(timezone.utc).isoformat(), 'groups': groups, 'resources': checks,
            'all_checked_experiment_resources_removed': bool(checks) and all(r['absent'] is True for r in checks),
            'other_retained_group_resources': retained,
            'scope': 'Recorded SQL group resources, driver, Function and registered support resources only. '
                     'Does not audit other subscriptions/groups or purge soft-deleted Key Vault names.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path('results/postgresql-azure-cloud'))
    parser.add_argument('--storage-state-dir', type=Path, default=Path('results/postgresql-azure-storage'))
    args = parser.parse_args()
    def read(path): return json.loads(path.read_text())
    plan = read(args.state_dir / 'driver-plan.json')
    evidence = Path(read(args.state_dir / 'latest-cost-evidence.json')['directory'])
    registry = read(args.storage_state_dir / 'functions.json')
    expected = targets(plan, registry, read(evidence / 'sql-resources.json'))
    from sebs.azure.control import AzureControl
    with AzureControl(plan['subscription']) as cli:
        result = verify(cli, expected)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = args.state_dir / 'cleanup-verification' / (stamp + '.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2) + '\n')
    for row in result['resources']:
        print(('ABSENT' if row['absent'] is True else 'PRESENT' if row['absent'] is False else 'UNKNOWN'), row['id'])
        if row.get('error'): print(' ', row['error'])
    print('All checked experiment resources removed:', result['all_checked_experiment_resources_removed'])
    print('Other retained resources in checked groups:', json.dumps(result['other_retained_group_resources']))
    print('Saved:', destination)
    print('Read-only verification; no SQL or deletions performed.')


if __name__ == '__main__':
    main()
