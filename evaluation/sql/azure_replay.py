"""Replay the prepared trace from the Azure driver; explicitly invoked after setup."""
import argparse
import json
from pathlib import Path

from evaluation.sql.azure_client import AzureFunctionsClient
from evaluation.sql.replay_service import ServiceClient, run
from evaluation.sql.synapse_client import SynapseClient, connect

BASE = Path('/opt/sebs-azure')


def processed_snapshot(plan, private):
    """Workspace aggregate meter outside replay timing, not per-query scan bytes."""
    con = None
    try:
        con = connect(plan, private, 'master')
        cursor = con.cursor()
        cursor.execute('SELECT * FROM sys.dm_external_data_processed')
        names = [column[0] for column in cursor.description]
        return {'rows': [dict(zip(names, [str(v) if v is not None else None for v in row]))
                         for row in cursor.fetchall()], 'error': None,
                'scope': 'workspace aggregate day/week/month counters; not per-query bytes'}
    except Exception as error:
        return {'rows': None, 'error': type(error).__name__}
    finally:
        if con is not None:
            con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', required=True, choices=['vm', 'functions', 'synapse'])
    parser.add_argument('--trace', type=Path, default=Path('comparison/trace.json'))
    parser.add_argument('--plan', type=Path, default=Path('comparison/private-driver.json'))
    parser.add_argument('--private', type=Path, default=BASE / 'private.json')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise ValueError('Output directory already exists; inspect saved results, do not replay blindly')
    if not (BASE / 'configure-ready.json').exists():
        raise ValueError('Driver setup has not completed')
    trace = json.loads(args.trace.read_text())
    plan = json.loads(args.plan.read_text())
    private = json.loads(args.private.read_text())
    before = None
    if args.backend == 'vm':
        client = ServiceClient(plan['server_endpoint'], private['service_token'])
        client.backend = 'azure-vm-duckdb'
    elif args.backend == 'functions':
        client = AzureFunctionsClient(plan['function'], trace['input'])
    else:
        client = SynapseClient(plan, private)
        before = processed_snapshot(plan, private)
    valid = run(trace, client, args.output_dir)
    if args.backend == 'synapse':
        (args.output_dir / 'data-processed.json').write_text(json.dumps({
            'before': before, 'after': processed_snapshot(plan, private),
            'note': 'Aggregate snapshots outside measured timing; includes any concurrent workspace activity. '
                    'Do not treat compressed input size as query billing.'}, indent=2) + '\n')
    summary_path = args.output_dir / 'summary.json'
    summary = json.loads(summary_path.read_text())
    summary['cost_note'] = ('Cost not yet computed. Functions requires native Azure execution meters; '
                            'VM cost includes the observation window and idle time; Synapse uses data processed '
                            'and includes automatic statistics. Storage and driver costs are separate.')
    summary_path.write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2), flush=True)
    raise SystemExit(0 if valid else 1)


if __name__ == '__main__':
    main()
