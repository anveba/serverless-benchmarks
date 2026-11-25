# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.

import datetime, io, json, os, uuid

import azure.functions as func


if 'NOSQL_STORAGE_DATABASE' in os.environ:

    from . import nosql

    nosql.nosql.get_instance(
        os.environ['NOSQL_STORAGE_DATABASE'],
        os.environ['NOSQL_STORAGE_URL'],
        os.environ['NOSQL_STORAGE_CREDS']
    )

if 'STORAGE_CONNECTION_STRING' in os.environ:

    from . import storage
    client = storage.storage.get_instance(os.environ['STORAGE_CONNECTION_STRING'])


# Optional durable receipts for long native PostgreSQL validation jobs. Ordinary
# SeBS requests keep their existing HTTP behavior. No SQL execution is retried.
def _sql_job_receipt(payload, context):
    job = payload.get('sebs_sql_result')
    if job is None:
        return None
    import re
    from urllib.parse import urlsplit
    config = json.loads(os.environ.get('SEBS_SQL_CONFIG', '{}'))
    if config.get('type') != 'postgresql' or config.get('provider') != 'azure':
        raise ValueError('SQL result receipts require native Azure PostgreSQL')
    if not isinstance(job, dict) or set(job) != {'job_id', 'upload_url'}:
        raise ValueError('Invalid SQL result receipt')
    job_id = job['job_id']
    if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('Invalid SQL job identifier')
    parts = urlsplit(job['upload_url'])
    storage = dict(item.split('=', 1) for item in os.environ['AzureWebJobsStorage'].split(';') if '=' in item)
    if (parts.scheme != 'https' or parts.netloc != storage['AccountName'] + '.blob.core.windows.net'
            or parts.path != '/sebs-sql-results/' + job_id + '.json'
            or not parts.query or parts.fragment):
        raise ValueError('SQL result receipt must target this function storage account')
    receipt = {'job_id': job_id, 'upload_url': job['upload_url'],
               'invocation_id': context.invocation_id}
    initial = {'state': 'running', 'job_id': job_id, 'invocation_id': context.invocation_id,
               'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    receipt['etag'] = _sql_job_upload(receipt, initial, create=True)
    if not receipt['etag']:
        raise RuntimeError('SQL receipt claim has no ETag; no query started')
    return receipt


def _sql_job_upload(receipt, value, create=False):
    from urllib.request import Request, build_opener, HTTPRedirectHandler
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    headers = {'Content-Type': 'application/json', 'x-ms-blob-type': 'BlockBlob',
               'x-ms-version': '2023-11-03'}
    headers['If-None-Match' if create else 'If-Match'] = '*' if create else receipt['etag']
    request = Request(receipt['upload_url'], data=json.dumps(value).encode(), headers=headers, method='PUT')
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        return response.headers.get('ETag')


def _sql_job_finish(receipt, response=None, error=None):
    if receipt is None:
        return
    value = {'state': 'complete' if error is None else 'failed', 'job_id': receipt['job_id'],
             'invocation_id': receipt['invocation_id'],
             'finished_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if error is None:
        value['response'] = response
    else:
        # The SQL wrapper deliberately hides credentials and untrusted SQL values.
        value['exception_type'] = type(error).__name__
        value['exception'] = str(error)[:1000]
    _sql_job_upload(receipt, value)


# TODO: usual trigger
# implement support for blob and others
def main(req: func.HttpRequest, context: func.Context) -> func.HttpResponse:
    if (getattr(req, 'method', '').upper() == 'GET'
            and getattr(req, 'params', {}).get('sebs-sql-health') == '1'):
        config = json.loads(os.environ.get('SEBS_SQL_CONFIG', '{}'))
        if config.get('type') == 'postgresql' and config.get('provider') == 'azure':
            return func.HttpResponse(json.dumps({'sql_result_receipt_version': 1,
                'request_id': context.invocation_id}), mimetype='application/json')
    income_timestamp = datetime.datetime.now().timestamp()
    req_json = req.get_json()

    req_json['request-id'] = context.invocation_id
    req_json['income-timestamp'] = income_timestamp
    receipt = _sql_job_receipt(req_json, context)
    begin = datetime.datetime.now()
    # We are deployed in the same directory
    from . import function
    try:
        ret = function.handler(req_json)
    except Exception as error:
        _sql_job_finish(receipt, error=error)
        raise
    end = datetime.datetime.now()

    log_data = ret
    if 'logs' in req_json:
        log_data['time'] = (end - begin) / datetime.timedelta(microseconds=1)
        results_begin = datetime.datetime.now()
        from . import storage
        storage_inst = storage.storage.get_instance()
        b = req_json.get('logs').get('bucket')
        req_id = context.invocation_id
        storage_inst.upload_stream(b, '{}.json'.format(req_id),
                io.BytesIO(json.dumps(log_data).encode('utf-8')))
        results_end = datetime.datetime.now()
        results_time = (results_end - results_begin) / datetime.timedelta(microseconds=1)
    else:
        results_time = 0

    # cold test
    is_cold = False
    fname = os.path.join('/tmp','cold_run')
    if not os.path.exists(fname):
        is_cold = True
        container_id = str(uuid.uuid4())[0:8]
        with open(fname, 'a') as f:
            f.write(container_id)
    else:
        with open(fname, 'r') as f:
            container_id = f.read()

    is_cold_worker = False
    global cold_marker
    try:
        _ = cold_marker
    except NameError:
        cold_marker = True
        is_cold_worker = True

    response = {
            'begin': begin.strftime('%s.%f'),
            'end': end.strftime('%s.%f'),
            'results_time': results_time,
            'result': log_data,
            'is_cold': is_cold,
            'is_cold_worker': is_cold_worker,
            'container_id': container_id,
            'environ_container_id': os.environ['CONTAINER_NAME'],
            'request_id': context.invocation_id
        }
    _sql_job_finish(receipt, response=response)
    return func.HttpResponse(json.dumps(response), mimetype="application/json")

