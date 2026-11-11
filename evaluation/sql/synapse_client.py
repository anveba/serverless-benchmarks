"""Synapse serverless SQL adapter for the existing single-query replay runner."""
import datetime
import hashlib
import re
import time
import uuid

from evaluation.sql.service import json_value, manifest_fingerprint, queries
from sebs.faas.function import ExecutionResult

COLLATION = 'Latin1_General_100_BIN2_UTF8'
IDENTIFIER = re.compile(r'[a-z_][a-z0-9_]*')
KEY = re.compile(r'[A-Za-z0-9._=/-]+')


def identifier(value):
    if not IDENTIFIER.fullmatch(value):
        raise ValueError('Invalid SQL identifier')
    return '[' + value + ']'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def query_sql(query):
    """Keep the benchmark SQL; T-SQL expresses the two LIMIT clauses as TOP."""
    sql = queries.QUERIES[query].strip()
    match = re.search(r'\s+LIMIT\s+(\d+)\s*$', sql, re.IGNORECASE)
    if match:
        sql = re.sub(r'^SELECT\b', 'SELECT TOP (' + match[1] + ')',
                     sql[:match.start()], count=1, flags=re.IGNORECASE)
    return sql


def table_pattern(keys):
    if not keys or len(set(keys)) != len(keys) or any(not KEY.fullmatch(key) for key in keys):
        raise ValueError('Invalid or duplicate Parquet manifest keys')
    parents = {key.rsplit('/', 1)[0] for key in keys if '/' in key and key.endswith('.parquet')}
    if len(parents) != 1 or any('/' not in key or not key.endswith('.parquet') for key in keys):
        raise ValueError('Each Synapse table must use one Parquet folder')
    if any(part in ('.', '..') for key in keys for part in key.split('/')):
        raise ValueError('Relative path components are not allowed')
    return parents.pop() + '/*.parquet'


def view_sql(table, keys):
    return (f'CREATE OR ALTER VIEW dbo.{identifier(table)} AS SELECT * FROM OPENROWSET('
            f'BULK {literal(table_pattern(keys))}, DATA_SOURCE = \'sebs_input\', '
            "FORMAT = 'PARQUET') AS parquet_rows")


def odbc_value(value):
    return '{' + str(value).replace('}', '}}') + '}'


def connect(state, private, database=None):
    import pyodbc
    # Disable implicit connection reuse so per-request connection costs are visible.
    pyodbc.pooling = False
    server = state['sql_endpoint']
    if not re.fullmatch(r'[a-z0-9-]+-ondemand\.sql\.azuresynapse\.net', server):
        raise ValueError('Expected a Synapse serverless SQL endpoint')
    database = database or state['sql_database']
    connection_string = (
        'DRIVER={ODBC Driver 18 for SQL Server};SERVER=tcp:' + server + ',1433;'
        'DATABASE=' + odbc_value(database) + ';UID=' + odbc_value(state['sql_admin']) + ';'
        'PWD=' + odbc_value(private['sql_password']) + ';'
        'Encrypt=yes;TrustServerCertificate=no;ConnectRetryCount=0;')
    return pyodbc.connect(connection_string, autocommit=True, timeout=30)


class SynapseClient:
    backend = 'synapse-serverless'
    cold_start_semantics = 'not_applicable; retain managed-engine startup and statistics overhead'

    def __init__(self, state, private):
        self.state, self.private = state, private
        self.endpoint = 'synapse://' + state['sql_endpoint'] + '/' + state['sql_database']

    def health(self):
        con = connect(self.state, self.private)
        try:
            cursor = con.cursor()
            cursor.execute('SELECT CAST(SERVERPROPERTY(\'ProductVersion\') AS varchar(128)), '
                           'CAST(DATABASEPROPERTYEX(DB_NAME(), \'Collation\') AS varchar(128))')
            version, collation = cursor.fetchone()
            cursor.execute("SELECT name FROM sys.views WHERE schema_id = SCHEMA_ID('dbo')")
            views = sorted(row[0] for row in cursor.fetchall())
        finally:
            con.close()
        return {'engine': 'Synapse serverless SQL', 'version': version, 'collation': collation,
                'views': views, 'input_fingerprint': self.state['input_fingerprint'],
                'connection_policy': 'fresh_per_request', 'result_reuse': 'no application result cache',
                'query_sql_sha256': hashlib.sha256(
                    '\n'.join(query_sql(q) for q in sorted(queries.QUERIES)).encode()).hexdigest()}

    def verify(self, health, trace):
        if health['input_fingerprint'] != manifest_fingerprint(trace['input']):
            raise ValueError('Synapse state and trace input differ')
        if health['collation'] != COLLATION or not set(trace['input']['tables']).issubset(health['views']):
            raise ValueError('Synapse database views or collation do not match setup')

    def invoke(self, query):
        sql = query_sql(query)
        begin = datetime.datetime.now()
        started = time.perf_counter()
        con = None
        request_id = str(uuid.uuid4())
        metrics = {'billed_bytes': None, 'data_processed_bytes': None,
                   'request_id_source': 'client-generated UUID; not an Azure request ID'}
        failure, rows, error = False, None, None
        try:
            con = connect(self.state, self.private)
            con.timeout = 900
            connected = time.perf_counter()
            cursor = con.cursor()
            cursor.execute('/* sebs_request=' + request_id + ' */\n' + sql)
            rows = [[json_value(v) for v in row] for row in cursor.fetchall()]
            metrics.update(connect_s=connected-started, execute_and_fetch_s=time.perf_counter()-connected)
        except Exception as exception:
            failure = True
            error = str(exception)
            for key in ['sql_password', 'input_sas', 'storage_connection_string']:
                value = self.private.get(key)
                if value:
                    error = error.replace(value, '[REDACTED]')
            error = type(exception).__name__ + ': ' + error[:1500]
        finally:
            if con is not None:
                con.close()
        result = ExecutionResult.from_times(begin, datetime.datetime.now())
        result.request_id, result.stats.cold_start, result.stats.failure = request_id, None, failure
        result.output = {'request_id': request_id, 'query': query, 'provider_metrics': metrics}
        if failure:
            result.output['error'] = error
        else:
            result.output['result'] = {'queries': {query: rows}}
        return result
