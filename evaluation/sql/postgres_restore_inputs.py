"""Restore the measured SF1 Parquet cache from original content-addressed S3 files.

Only reads cloud objects. Verifies every byte hash and the complete recorded
fingerprint before publishing the local cache. Existing local files are backed
up. PostgreSQL, Lambda and the failed loader checkpoints are not changed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import uuid

import boto3

from sebs.benchmark import load_benchmark_input
from sebs.storage.aws_postgresql import AWSPostgreSQL
from sebs.storage.postgresql import build_dataset
from sebs.storage.postgresql_loader import file_sha256
from sebs.storage.sql_config import sql_config


ROOT=Path(__file__).resolve().parents[2]
METADATA={'scale_factor':1,'input_size':'small','workload':'TPC-H'}


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n')
    temporary.replace(path)


def restore_cache(report, destination, fetch):
    """fetch(table, sha256, target) reads an original immutable cloud artifact."""
    declaration=load_benchmark_input(str(ROOT/'benchmarks/700.analytics/720.sql-postgresql')).allocate_sql()
    if report.get('metadata')!=METADATA or set(report.get('tables',{}))!=set(declaration):
        raise ValueError('Expected the original five-table SF1/small TPC-H dataset report')
    content={'tables':{},'metadata':report['metadata']}
    for table,keys in declaration.items():
        spec=report['tables'][table]
        if spec['primary_key']!=keys or not spec['sha256']:
            raise ValueError('Recorded table/key declaration differs from the benchmark')
        for digest in spec['sha256']:
            if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('Invalid recorded content hash')
        content['tables'][table]={'primary_key':keys,'sha256':spec['sha256']}
    fingerprint=hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()
    if fingerprint!=report['fingerprint']:
        raise ValueError('Recorded dataset report fingerprint is inconsistent')
    destination.parent.mkdir(parents=True,exist_ok=True)
    copied,downloaded=0,0
    with tempfile.TemporaryDirectory(prefix='sebs-sf1-restore-',dir=destination.parent) as temporary:
        staged=Path(temporary)/'small'
        staged.mkdir()
        tables={}
        for table in declaration:
            hashes=report['tables'][table]['sha256']
            available={file_sha256(p):p for p in (destination/table).glob('*.parquet')}
            folder=staged/table
            folder.mkdir()
            files=[]
            # Zero-padding preserves the recorded manifest order under sorted().
            for index,digest in enumerate(hashes):
                target=folder/f'data_{index:05d}.parquet'
                if digest in available:
                    shutil.copyfile(available[digest],target)
                    copied+=1
                else:
                    fetch(table,digest,target)
                    downloaded+=1
                if file_sha256(target)!=digest:
                    raise ValueError('Downloaded/copied input failed SHA256 verification: '+table)
                files.append(target)
            (staged/(table+'.complete')).touch()
            tables[table]={'files':files,'primary_key':declaration[table]}
            print('Verified',table,':',len(files),'files',flush=True)
        verified=build_dataset('restore-verification','720.sql-postgresql','small',tables,METADATA)
        if verified['fingerprint']!=report['fingerprint']:
            raise ValueError('Restored inputs do not match the measured fingerprint')
        backup=None
        if destination.exists():
            backup=destination.with_name(destination.name+'.before-restore-'+uuid.uuid4().hex[:10])
            destination.rename(backup)
        try:
            staged.rename(destination)
        except BaseException:
            if backup is not None:
                backup.rename(destination)
            raise
    return {'fingerprint':report['fingerprint'],'destination':str(destination),
            'backup':str(backup) if backup else None,'downloaded_files':downloaded,'reused_local_files':copied,
            'cloud_objects_modified':0,'benchmark_queries_executed':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=Path('results/postgresql-aws-cloud'))
    parser.add_argument('--storage-configuration',type=Path,default=Path('results/postgresql-aws-storage.json'))
    args=parser.parse_args()
    plan=json.loads((args.state_dir/'preflight.json').read_text())
    payload=plan['input']
    if payload.get('scale_factor')!=1:
        parser.error('This recovery helper restores the measured SF1 inputs only')
    config=sql_config(json.loads(args.storage_configuration.read_text())['sql'])
    if config.provider!='aws':
        parser.error('Existing AWS PostgreSQL storage attachment required')
    _,stack,outputs=AWSPostgreSQL(config).info()
    if stack['StackId']!=plan['stack_id'] or outputs['StagingBucket']!=plan['staging_bucket']:
        raise ValueError('Storage attachment differs from the measured deployment')
    report=json.loads((Path(config.state_dir)/'datasets'/(payload['namespace']+'.json')).read_text())
    if report['namespace']!=payload['namespace'] or report['fingerprint']!=payload['input_fingerprint']:
        raise ValueError('Saved dataset report differs from the measured workload')
    s3=boto3.client('s3',region_name=plan['region'])
    def fetch(table,digest,target):
        key=f"datasets/{report['fingerprint']}/{table}/{digest}.parquet"
        s3.download_file(plan['staging_bucket'],key,str(target))
    destination=Path(tempfile.gettempdir())/'sebs-tpch/sf1/small'
    result=restore_cache(report,destination,fetch)
    save(args.state_dir/'input-restore.json',result)
    print('Original measured input fingerprint restored:',result['fingerprint'])
    print('Local cache:',result['destination'])
    print('Previous local cache backup:',result['backup'])
    print('Downloads:',result['downloaded_files'],'reused files:',result['reused_local_files'])
    print('No database changes, Lambda invocations or cloud object writes.')


if __name__=='__main__':
    main()
