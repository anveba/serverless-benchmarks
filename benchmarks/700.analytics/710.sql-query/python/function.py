# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
import datetime
import decimal
import json
import math
import os
import re
import resource
import shutil
import tempfile

import duckdb

from . import queries
from . import storage
client = storage.storage.get_instance()

# extensions are bundled by package.sh
EXTENSION_DIR = os.path.join(os.path.dirname(os.path.dirname(duckdb.__file__)), 'duckdb_extensions')
# headroom for memory DuckDB does not track
MEMORY_LIMIT_FRACTION = 0.75
# request values end up in SQL statements and S3 keys
BUCKET_PATTERN = re.compile(r'sebs-benchmarks-[a-z0-9._-]+')
KEY_PATTERN = re.compile(r'[A-Za-z0-9._=/-]+')


def validate_event(event):
    bucket = event['bucket']
    if not BUCKET_PATTERN.fullmatch(bucket['bucket']):
        raise ValueError('Invalid bucket name')
    if not KEY_PATTERN.fullmatch(bucket['output']):
        raise ValueError('Invalid output prefix')

    for tables in (event.get('tables', {}), event.get('inputs', {})):
        for table, keys in tables.items():
            if table not in queries.INPUT_TABLES:
                raise ValueError('Invalid table name')
            if not all(KEY_PATTERN.fullmatch(key) for key in keys):
                raise ValueError('Invalid object key')

    output = event.get('output', {})
    if not KEY_PATTERN.fullmatch(output.get('prefix', 'unused')):
        raise ValueError('Invalid output prefix')
    if output.get('key', queries.PARTITION_KEY) != queries.PARTITION_KEY:
        raise ValueError('Invalid partition key')
    if not isinstance(output.get('count', 1), int) or output.get('count', 1) < 1:
        raise ValueError('Invalid partition count')


def elapsed_us(begin):
    return (datetime.datetime.now() - begin) / datetime.timedelta(microseconds=1)


def memory_limit_mb():
    if 'AWS_LAMBDA_FUNCTION_MEMORY_SIZE' in os.environ:
        return int(os.environ['AWS_LAMBDA_FUNCTION_MEMORY_SIZE'])
    try:
        with open('/sys/fs/cgroup/memory.max') as f:
            limit = f.read().strip()
        return None if limit == 'max' else int(limit) // 2**20
    except OSError:
        return None


def thread_count():
    cpus = os.cpu_count()
    try:
        with open('/sys/fs/cgroup/cpu.max') as f:
            quota, period = f.read().split()
        if quota != 'max':
            cpus = min(cpus, math.ceil(int(quota) / int(period)))
    except OSError:
        pass
    return cpus


def storage_access():
    """Return the URL scheme and the DuckDB secret for direct access to storage."""
    if 'MINIO_ADDRESS' in os.environ:
        return 's3', ("TYPE s3, KEY_ID '{MINIO_ACCESS_KEY}', SECRET '{MINIO_SECRET_KEY}', "
                      "ENDPOINT '{MINIO_ADDRESS}', URL_STYLE 'path', USE_SSL false, "
                      "REGION 'us-east-1'").format(**os.environ)
    # set by SeBS on GCP, from the HMAC key of the deployment
    if 'GCS_HMAC_KEY_ID' in os.environ:
        return 'gs', "TYPE gcs, KEY_ID '{GCS_HMAC_KEY_ID}', SECRET '{GCS_HMAC_SECRET}'".format(**os.environ)
    if 'AWS_SESSION_TOKEN' in os.environ:
        return 's3', ("TYPE s3, KEY_ID '{AWS_ACCESS_KEY_ID}', SECRET '{AWS_SECRET_ACCESS_KEY}', "
                      "SESSION_TOKEN '{AWS_SESSION_TOKEN}', REGION '{AWS_REGION}'").format(**os.environ)
    raise RuntimeError('Direct access to storage is not configured on this platform, use download mode.')


def connect(workdir, access_mode):
    # new connection per query, otherwise warm runs read from DuckDB's cache instead of S3
    con = duckdb.connect(config={
        'extension_directory': EXTENSION_DIR,
        'autoinstall_known_extensions': False,
        'autoload_known_extensions': False,
        'home_directory': workdir,
        'temp_directory': os.path.join(workdir, 'spill'),
        'enable_external_file_cache': False,
        'threads': thread_count(),
    })
    memory = memory_limit_mb()
    if memory is not None:
        con.execute("SET memory_limit='{}MB'".format(int(memory * MEMORY_LIMIT_FRACTION)))
    if access_mode == 'httpfs':
        con.execute('LOAD httpfs')
        con.execute('CREATE SECRET storage ({})'.format(storage_access()[1]))
    return con


def create_views(con, bucket, tables, access_mode, workdir):
    for table, keys in tables.items():
        if access_mode == 'httpfs':
            scheme = storage_access()[0]
            paths = ['{}://{}/{}'.format(scheme, bucket, key) for key in keys]
        else:
            paths = []
            for idx, key in enumerate(keys):
                paths.append(os.path.join(workdir, '{}-{}.parquet'.format(table, idx)))
                client.download(bucket, key, paths[-1])
        con.execute('CREATE VIEW {} AS SELECT * FROM read_parquet({})'.format(table, paths))


def json_value(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


def fetch_rows(con, sql):
    return [[json_value(value) for value in row] for row in con.execute(sql).fetchall()]


def run_statement(con, workdir, run):
    profile = os.path.join(workdir, 'profile.json')
    con.execute("CALL enable_logging('HTTP')")
    con.execute("SET enable_profiling='json'")
    con.execute("SET profiling_output='{}'".format(profile))

    begin = datetime.datetime.now()
    result = run()
    query_time = elapsed_us(begin)

    # read before the next statement overwrites it
    with open(profile) as f:
        bytes_read = json.load(f)['io']['total_bytes_read']
    con.execute('PRAGMA disable_profiling')
    requests = dict(con.execute(
        "SELECT request.type, count(*) FROM duckdb_logs_parsed('HTTP') GROUP BY 1"
    ).fetchall())

    return result, {
        'query_time': query_time,
        'bytes_read': bytes_read,
        'get_requests': requests.get('GET', 0),
        'head_requests': requests.get('HEAD', 0),
    }


def run_sql(bucket, tables, access_mode, execute):
    workdir = tempfile.mkdtemp()
    try:
        begin = datetime.datetime.now()
        con = connect(workdir, access_mode)
        connect_time = elapsed_us(begin)

        begin = datetime.datetime.now()
        create_views(con, bucket, tables, access_mode, workdir)
        setup_time = elapsed_us(begin)

        result, measurement = run_statement(con, workdir, lambda: execute(con, workdir))
        con.close()
        measurement.update({'connect_time': connect_time, 'setup_time': setup_time})
        return result, measurement
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_queries(event):
    bucket = event['bucket']['bucket']
    access_mode = event.get('access_mode', 'httpfs')
    tables = event['tables']

    results = {}
    measurements = {}
    for name in event['queries']:
        query_tables = {table: tables[table] for table in queries.TABLES[name]}
        results[name], measurements[name] = run_sql(
            bucket, query_tables, access_mode,
            lambda con, workdir: fetch_rows(con, queries.QUERIES[name])
        )

    return {
        'result': {
            'queries': results
        },
        'measurement': {
            'access_mode': access_mode,
            'threads': thread_count(),
            'memory_limit_mb': memory_limit_mb(),
            'peak_rss_mb': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
            'queries': measurements
        }
    }


def write_object(con, sql, workdir, bucket, prefix):
    path = os.path.join(workdir, 'output.parquet')
    con.execute("COPY ({}) TO '{}' (FORMAT parquet)".format(sql, path))
    return client.upload(bucket, prefix + '.parquet', path)


def write_partitions(con, sql, workdir, bucket, prefix, key, count):
    path = os.path.join(workdir, 'partitions')
    con.execute(
        "COPY (SELECT *, hash({key}) % {count} AS sebs_partition FROM ({sql})) "
        "TO '{path}' (FORMAT parquet, PARTITION_BY (sebs_partition))".format(
            key=key, count=count, sql=sql, path=path
        )
    )
    partitions = {}
    for directory in os.listdir(path):
        partition = directory.split('=')[1]
        for idx, file in enumerate(os.listdir(os.path.join(path, directory))):
            partitions.setdefault(partition, []).append(client.upload(
                bucket,
                '{}-{}-{}.parquet'.format(prefix, partition, idx),
                os.path.join(path, directory, file)
            ))
    return partitions


def run_stage(event):
    bucket = event['bucket']['bucket']
    sql = queries.STAGES[event['stage']]
    output = event['output']
    prefix = os.path.join(event['bucket']['output'], output.get('prefix', ''))

    if output['type'] == 'rows':
        execute = lambda con, workdir: {'rows': fetch_rows(con, sql)}
    elif output['type'] == 'object':
        execute = lambda con, workdir: {'key': write_object(con, sql, workdir, bucket, prefix)}
    else:
        execute = lambda con, workdir: {'partitions': write_partitions(
            con, sql, workdir, bucket, prefix, output['key'], output['count']
        )}

    result, measurement = run_sql(bucket, event['inputs'], event.get('access_mode', 'httpfs'), execute)
    return {
        'result': result,
        'measurement': measurement
    }


def handler(event):
    validate_event(event)
    if event.get('operation') == 'stage':
        return run_stage(event)
    return run_queries(event)
