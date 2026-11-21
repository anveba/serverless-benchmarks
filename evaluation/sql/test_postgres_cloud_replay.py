"""Offline paired-replay checks. No PostgreSQL server or AWS credentials needed."""
import ast
import base64
from datetime import datetime, timezone
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace, ModuleType
import unittest
import zlib
from unittest.mock import Mock, patch

from evaluation.sql.postgres_cloud_replay import (
    ROOT, SOURCES, DirectPostgresClient, collect, official_answers, reference_validator,
    remote_run, start, start_script, summarize, workload_trace, write,
)


class PostgresCloudReplayTests(unittest.TestCase):
    def test_direct_failure_preserves_only_the_wrappers_redacted_stage_and_class(self):
        client=DirectPostgresClient(self.settings()['plan'],self.settings()['trace']['input'])
        worker=Mock()
        worker.handler.side_effect=RuntimeError('SQL request failed at connect (OperationalError); no query was retried')
        with patch.object(client,'worker',return_value=worker):
            result=client.invoke('q12')
        self.assertTrue(result.stats.failure)
        self.assertEqual(result.output['failure_stage'],'connect')
        self.assertEqual(result.output['cause_type'],'OperationalError')
        worker.handler.assert_called_once()
        worker.handler.side_effect=RuntimeError('unexpected private password=secret-value')
        with patch.object(client,'worker',return_value=worker):
            result=client.invoke('q12')
        self.assertNotIn('secret-value',json.dumps(result.output))
        self.assertNotIn('failure_stage',result.output)

    def test_named_workloads_preserve_data_and_generate_expected_arrival_schedules(self):
        original = self.settings()['trace']
        original_copy = json.loads(json.dumps(original))
        for name, interval in [('moderate',2),('burst',0),('sparse',30)]:
            with self.subTest(workload=name):
                trace = workload_trace(original,name,100,2)
                self.assertEqual(trace['input'],original['input'])
                self.assertEqual(trace['settings']['memory'],512)
                self.assertEqual(trace['settings']['max-in-flight'],2)
                self.assertEqual(len(trace['requests']),100)
                self.assertEqual(trace['requests'][-1]['at'],99*interval)
                self.assertEqual([r['query'] for r in trace['requests'][:5]],['q6','q1','q12','q3','q21'])
                self.assertEqual(trace['requests'],trace['settings']['requests'])
        self.assertEqual(original,original_copy)
        self.assertEqual(workload_trace(original),original)

    def test_invalid_workload_options_are_rejected_before_any_cloud_call(self):
        original = self.settings()['trace']
        for options in [dict(count=100),dict(workload='unknown'),dict(workload='moderate',count=0),
                        dict(workload='moderate',count=True),dict(max_in_flight=0)]:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    workload_trace(original,**options)

    def test_full_start_submits_same_expanded_workload_for_both_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            clients=self.launch_fixture(folder)
            clients['ssm'].send_command.return_value={'Command':{'CommandId':'moderate'}}
            with patch('evaluation.sql.postgres_cloud_replay.boto3.client',side_effect=lambda name,**kwargs:clients[name]), \
                 patch('evaluation.sql.postgres_cloud_replay.official_answers',return_value={'q6':[['42']]}):
                start(folder,'moderate-100',3,workload='moderate',count=100,max_in_flight=2)
            saved=json.loads((folder/'runs/moderate-100/settings.json').read_text())
            self.assertEqual(len(saved['trace']['requests']),100)
            self.assertEqual(saved['trace']['requests'][-1]['at'],198)
            self.assertEqual(saved['trace']['settings']['max-in-flight'],2)
            self.assertEqual(saved['repetitions'],3)
            self.assertEqual(saved['plan']['input'],saved['trace']['input'])
            clients['ssm'].send_command.assert_called_once()

    def launch_fixture(self, folder):
        settings = self.settings()
        plan = {**settings['plan'], 'input': settings['trace']['input']}
        write(folder/'preflight.json', plan)
        write(folder/'trace.json', settings['trace'])
        write(folder/'instance.json', {'stack_id': 'driver-stack', 'instance_id': 'driver'})
        write(folder/'artifact.json', {'bucket': 'staging', 'report_prefix': 'reports/owned/'})
        write(folder/'launch-request.json', {'ClientRequestToken': 'owned'})
        cf, ec2, ssm = Mock(), Mock(), Mock()
        cf.describe_stacks.return_value = {'Stacks': [{'StackStatus': 'CREATE_COMPLETE',
            'Tags': [{'Key': 'sebs-driver-owner', 'Value': 'owned'}]}]}
        ec2.describe_instances.return_value = {'Reservations': [{'Instances': [{
            'InstanceId': 'server', 'InstanceType': 'm7i-flex.large',
            'State': {'Name': 'running'}, 'RootDeviceName': '/dev/sda1',
            'Placement': {'AvailabilityZone': 'us-east-1a'},
            'BlockDeviceMappings': [{'DeviceName': '/dev/sda1', 'Ebs': {'VolumeId': 'volume'}}]}]}]}
        ec2.describe_volumes.return_value = {'Volumes': [{'VolumeId': 'volume', 'VolumeType': 'gp3',
            'Size': 64, 'Iops': 3000, 'Throughput': 125}]}
        ec2.describe_volumes_modifications.return_value = {'VolumesModifications': [{
            'VolumeId': 'volume', 'ModificationState': 'completed', 'TargetThroughput': 125,
            'StartTime': datetime(2026,11,20,14,21,48,tzinfo=timezone.utc),
            'EndTime': datetime(2026,11,20,14,25,0,tzinfo=timezone.utc)}]}
        return {'cloudformation': cf, 'ec2': ec2, 'ssm': ssm}

    def test_full_start_serializes_native_aws_timestamps_and_checkpoints_before_single_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            clients = self.launch_fixture(folder)
            marker = folder/'runs/comparison-01/submission.json'
            def submit(**kwargs):
                self.assertIsNone(json.loads(marker.read_text())['command_id'])
                script = kwargs['Parameters']['commands'][0]
                program = ast.parse(script.split("<<'INSTALL'\n")[1].split('\nINSTALL\n')[0])
                packed = next(node.args[0].value for node in ast.walk(program)
                              if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
                              and node.func.attr=='b64decode')
                payload = json.loads(zlib.decompress(base64.b64decode(packed)))
                self.assertEqual(payload['settings']['allocation']['modification']['StartTime'], '2026-11-20T14:21:48+00:00')
                self.assertEqual(payload['settings']['allocation']['modification']['EndTime'], '2026-11-20T14:25:00+00:00')
                return {'Command': {'CommandId': 'submitted-once'}}
            clients['ssm'].send_command.side_effect = submit
            with patch('evaluation.sql.postgres_cloud_replay.boto3.client', side_effect=lambda name,**kwargs:clients[name]), \
                 patch('evaluation.sql.postgres_cloud_replay.official_answers', return_value={'q6':[['42']]}):
                start(folder,'comparison-01',2)
                start(folder,'comparison-01',2)
            self.assertEqual(clients['ssm'].send_command.call_count,1)
            self.assertEqual(json.loads(marker.read_text())['command_id'],'submitted-once')
            saved = json.loads((folder/'runs/comparison-01/settings.json').read_text())
            self.assertEqual(saved['allocation']['modification']['StartTime'],'2026-11-20T14:21:48+00:00')

    def test_lost_ssm_response_retains_ambiguous_marker_and_prevents_resubmission(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            clients = self.launch_fixture(folder)
            clients['ssm'].send_command.side_effect = TimeoutError('response lost')
            with patch('evaluation.sql.postgres_cloud_replay.boto3.client', side_effect=lambda name,**kwargs:clients[name]), \
                 patch('evaluation.sql.postgres_cloud_replay.official_answers', return_value={'q6':[['42']]}):
                with self.assertRaises(TimeoutError):
                    start(folder,'comparison-01',2)
                start(folder,'comparison-01',2)
            clients['ssm'].send_command.assert_called_once()
            marker = json.loads((folder/'runs/comparison-01/submission.json').read_text())
            self.assertIsNone(marker['command_id'])

    def test_cached_reference_extension_is_loaded_without_reinstalling_or_changing_origin(self):
        con = Mock()
        con.__enter__ = Mock(return_value=con)
        con.__exit__ = Mock(return_value=False)
        def execute(statement, *args):
            if statement.startswith('INSTALL'):
                raise RuntimeError('Cached extension origin differs; installation must not be attempted')
            return SimpleNamespace(fetchone=lambda: (True,) if statement.startswith('SELECT installed') else ('revenue\n42\n',))
        con.execute.side_effect = execute
        with patch('duckdb.connect', return_value=con):
            self.assertEqual(official_answers(['q6']), {'q6': [['42']]})
        self.assertIn(('LOAD tpch',), [call.args for call in con.execute.call_args_list])

    def test_missing_reference_extension_is_installed_over_https_then_loaded(self):
        con = Mock()
        con.__enter__ = Mock(return_value=con)
        con.__exit__ = Mock(return_value=False)
        con.execute.side_effect = lambda statement, *args: SimpleNamespace(
            fetchone=lambda: (False,) if statement.startswith('SELECT installed') else ('revenue\n42\n',))
        with patch('duckdb.connect', return_value=con):
            self.assertEqual(official_answers(['q6']), {'q6': [['42']]})
        statements = [call.args[0] for call in con.execute.call_args_list]
        self.assertLess(statements.index("INSTALL tpch FROM 'https://extensions.duckdb.org'"), statements.index('LOAD tpch'))

    def settings(self):
        return {'run_id': 'comparison-01', 'plan': {'region': 'us-east-1', 'server_instance_id': 'server',
                'lambda_function': 'function'}, 'repetitions': 2, 'artifact': {'bucket': 'staging'},
                'report_prefix': 'reports/owned/comparison-01/', 'allocation': {'volume': {'Throughput': 125}},
                'trace': {'input': {'namespace': 'schema', 'scale_factor': 1, 'input_fingerprint': 'f'*64,
                                   'connection_policy': 'fresh'},
                          'settings': {'max-in-flight': 2, 'memory': 512},
                          'requests': [{'at': 0, 'query': 'q6'}]}, 'expected': {'q6': [['42']]}}

    def test_reference_uses_benchmark_tolerance_and_rejects_wrong_answers(self):
        validator = reference_validator({'q6': [['42']]})
        payload = {'scale_factor': 1, 'queries': ['q6']}
        self.assertIsNone(validator.validate_output(None, payload, {'result': {'queries': {'q6': [[42.001]]}}}, 'python'))
        self.assertIsNotNone(validator.validate_output(None, payload, {'result': {'queries': {'q6': [[99]]}}}, 'python'))

    def test_direct_workers_run_verbatim_handler_with_distinct_clients_and_cache_per_thread(self):
        barrier = threading.Barrier(2)
        package = ModuleType('driver_function')
        package.__path__ = []
        sql = ModuleType('driver_function.sql')
        clients = []
        class Client:
            @staticmethod
            def get_instance():
                return None
            def __init__(self):
                clients.append(self)
            def query(self, statement, *args, **kwargs):
                return {'rows': [[42]], 'measurement': {'query_time': 12}}
        sql.sql = Client
        package.sql = sql
        queries = ModuleType('driver_function.queries')
        queries.QUERIES = {'q6': 'SELECT 42'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'driver_function').mkdir()
            (root/'driver_function/function.py').write_bytes((ROOT/SOURCES['function/function.py']).read_bytes())
            with patch.dict('sys.modules', {'driver_function': package, 'driver_function.sql': sql,
                                           'driver_function.queries': queries}):
                with patch('evaluation.sql.postgres_cloud_replay.ROOT', root):
                    client = DirectPostgresClient(self.settings()['plan'], self.settings()['trace']['input'])
                    def worker():
                        first = client.worker()
                        barrier.wait(timeout=5)
                        self.assertIs(first, client.worker())
                        return client.invoke('q6')
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [pool.submit(worker) for _ in range(2)]
                        results = [f.result(timeout=10) for f in futures]
        self.assertEqual(len(clients), 2)
        self.assertEqual([result.output['result']['queries']['q6'] for result in results], [[[42]], [[42]]])
        self.assertTrue(all(result.stats.cold_start is None for result in results))

    def test_saved_submission_never_submits_a_second_workload(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write(folder/'runs/comparison-01/submission.json', {'command_id': None})
            with patch('evaluation.sql.postgres_cloud_replay.boto3.client') as aws:
                start(folder, 'comparison-01', 2)
                aws.assert_not_called()

    def test_submission_script_compiles_and_uses_single_run_directory_and_unit(self):
        script = start_script(self.settings(), {})
        result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        ast.parse(script.split("<<'INSTALL'\n")[1].split('\nINSTALL\n')[0])
        self.assertIn('exist_ok=False', script)
        self.assertIn('systemd-run --unit=sebs-pg-comparison-01', script)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', script)

    def test_compressed_transfer_restores_exact_source_bytes_before_execution(self):
        data = '# exact UTF-8 source: π\nvalue = 42\n'.encode()
        name = 'evaluation/sql/lambda_client.py'
        sources = {name: {'data':data.decode(), 'sha256':hashlib.sha256(data).hexdigest()}}
        script = start_script(self.settings(), sources)
        program = script.split("<<'INSTALL'\n")[1].split('\nINSTALL\n')[0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'evaluation/sql').mkdir(parents=True)
            program = program.replace('/opt/sebs-pg-driver/runs/comparison-01', str(root/'run'))
            result = subprocess.run([sys.executable,'-B','-c',program],cwd=root,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual((root/name).read_bytes(),data)
            # The directory guard prevents restarting an already installed run.
            repeated = subprocess.run([sys.executable,'-B','-c',program],cwd=root,text=True,capture_output=True)
            self.assertNotEqual(repeated.returncode,0)

    def test_two_repetitions_reverse_backend_order_and_upload_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write(folder/'settings.json', self.settings())
            with patch('evaluation.sql.postgres_cloud_replay.verify_runtime'), \
                 patch('evaluation.sql.postgres_cloud_replay.reference_validator'), \
                 patch('evaluation.sql.postgres_cloud_replay.boto3.client') as aws, \
                 patch('evaluation.sql.postgres_cloud_replay.DirectPostgresClient', return_value=SimpleNamespace(backend='postgresql')), \
                 patch('evaluation.sql.lambda_client.LambdaClient', return_value=SimpleNamespace(backend='lambda')), \
                 patch('evaluation.sql.replay_service.run', return_value=True) as run:
                remote_run(folder)
                self.assertEqual([call.args[1].backend for call in run.call_args_list], ['postgresql','lambda','lambda','postgresql'])
                self.assertEqual(json.loads((folder/'status.json').read_text())['state'], 'complete')
                aws.return_value.upload_file.assert_called()
                with self.assertRaisesRegex(ValueError, 'already started'):
                    remote_run(folder)

    def test_failed_answers_stop_further_phases_and_preserve_failure_status(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write(folder/'settings.json', self.settings())
            with patch('evaluation.sql.postgres_cloud_replay.verify_runtime'), \
                 patch('evaluation.sql.postgres_cloud_replay.reference_validator'), \
                 patch('evaluation.sql.postgres_cloud_replay.boto3.client'), \
                 patch('evaluation.sql.replay_service.run', return_value=False) as run:
                with self.assertRaisesRegex(RuntimeError, 'invalid answers'):
                    remote_run(folder)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(json.loads((folder/'status.json').read_text())['state'], 'failed')

    def test_collection_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            settings = self.settings()
            write(folder/'runs/comparison-01/settings.json', settings)
            with patch('evaluation.sql.postgres_cloud_replay.boto3.client') as aws:
                aws.return_value.get_paginator.return_value.paginate.return_value = [
                    {'Contents': [{'Key': settings['report_prefix']+'../escape.json'}]}]
                with self.assertRaisesRegex(ValueError, 'Unexpected result key'):
                    collect(folder, 'comparison-01')
                aws.return_value.download_file.assert_not_called()

    def test_summary_checks_completion_and_does_not_invent_missing_lambda_billing(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            root = folder/'runs/comparison-01/collected'
            settings = self.settings()
            settings['repetitions'] = 1
            write(root/'settings.json', settings)
            write(root/'status.json', {'state':'complete', 'completed_runs': 2})
            for backend in ['postgresql','lambda']:
                out = root/'repeat-1'/backend
                write(out/'requests.json', [{'index':0,'query':'q6','valid':True,'failure':False,'cold_start':False,
                                            'queue_delay_s':0,'total_latency_s':1}])
                write(out/'responses.json', {'0':{'query':'q6','measurement':{'q6':{'query_time':1000}},
                                                'provider_metrics':{'native_report_available':False}}})
                write(out/'trace.json', {'trace':settings['trace']})
                write(out/'summary.json', {'valid':1,'observation_seconds':1})
            report = summarize(folder,'comparison-01')
            self.assertIsNone(report['backends']['lambda']['billed_gb_seconds'])
            write(root/'status.json', {'state':'failed','completed_runs':1})
            with self.assertRaisesRegex(ValueError,'incomplete'):
                summarize(folder,'comparison-01')


if __name__ == '__main__':
    unittest.main()
