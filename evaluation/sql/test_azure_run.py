"""Submission guards and complete/failed job checkpoints; no cloud or SQL calls."""
import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from evaluation.sql.azure_run import (REMOTE_RUNNER, digest, job_folder,
    launch_script, percentile, start, status_script, make_tasks, summarize_suite)


class AzureRunTests(unittest.TestCase):
    def fixture(self, base):
        repo = base / 'repo'
        (repo / 'comparison').mkdir(parents=True)
        trace = {'requests': [{'at': 0, 'query': 'q6'}],
                 'input': {'bucket': {'bucket': 'fixture'}, 'tables': {}, 'scale_factor': 1}}
        (repo / 'comparison/trace.json').write_text(json.dumps(trace))
        (repo / 'comparison/private-driver.json').write_text(json.dumps({'region': 'swedencentral', 'vm_size': 'fixture'}))
        (base / 'configure-ready.json').write_text('{}')
        (base / 'private.json').write_text(json.dumps({'storage_connection_string': 'fixture-connection'}))
        return {'run_id': 'comparison-01', 'backends': ['vm', 'functions', 'synapse'],
                'blob_prefix': 'evaluation/fixture/', 'trace_sha256': digest(trace),
                'source_code_sha256': 'fixture-sha'}

    def namespace(self):
        namespace = {'__name__': 'test_remote_runner'}
        exec(compile(REMOTE_RUNNER, '<remote-runner>', 'exec'), namespace)
        return namespace

    def fake_child(self, failed_backend=None):
        def child(args, **kwargs):
            backend = args[args.index('--backend') + 1]
            output = Path(args[args.index('--output-dir') + 1])
            output.mkdir()
            fail = backend == failed_backend
            summary = {'submitted': 1, 'valid': 0 if fail else 1,
                       'execution_failures': int(fail), 'observation_seconds': 1.0}
            (output / 'summary.json').write_text(json.dumps(summary))
            (output / 'requests.json').write_text('[]')
            return Mock(returncode=int(fail))
        return child

    def test_complete_job_runs_each_backend_once_and_uploads_checkpoints(self):
        namespace = self.namespace()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            settings = self.fixture(base)
            with patch('azure.storage.blob.BlobServiceClient.from_connection_string') as blob, \
                 patch('subprocess.run', side_effect=self.fake_child()) as child, \
                 redirect_stdout(io.StringIO()):
                code = namespace['execute'](settings, base)
            self.assertEqual(code, 0)
            self.assertEqual(child.call_count, 3)
            self.assertEqual([call.args[0][call.args[0].index('--backend') + 1]
                              for call in child.call_args_list], settings['backends'])
            state = json.loads((base / 'runs/comparison-01/run.json').read_text())
            self.assertEqual(state['status'], 'complete')
            self.assertEqual(state['completed_backends'], settings['backends'])
            self.assertGreater(blob.return_value.get_container_client.return_value.upload_blob.call_count, 3)
            with patch('subprocess.run') as child:
                with self.assertRaisesRegex(ValueError, 'duplicate replay'):
                    namespace['execute'](settings, base)
                child.assert_not_called()

    def test_failed_backend_stops_before_next_backend_and_is_not_retried(self):
        namespace = self.namespace()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            settings = self.fixture(base)
            with patch('azure.storage.blob.BlobServiceClient.from_connection_string'), \
                 patch('subprocess.run', side_effect=self.fake_child('functions')) as child, \
                 redirect_stdout(io.StringIO()):
                code = namespace['execute'](settings, base)
            self.assertEqual(code, 1)
            self.assertEqual(child.call_count, 2)
            state = json.loads((base / 'runs/comparison-01/run.json').read_text())
            self.assertEqual(state['status'], 'failed')
            self.assertEqual(state['completed_backends'], ['vm'])
            self.assertFalse((base / 'runs/comparison-01/synapse').exists())

    def test_mismatched_trace_is_rejected_before_queries(self):
        namespace = self.namespace()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            settings = self.fixture(base)
            settings['trace_sha256'] = 'different'
            with patch('subprocess.run') as child:
                with self.assertRaisesRegex(ValueError, 'trace differs'):
                    namespace['execute'](settings, base)
                child.assert_not_called()

    def test_local_submission_marker_blocks_accidental_second_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            out = job_folder(base, 'comparison-01')
            out.mkdir(parents=True)
            (out / 'command.json').write_text('{}')
            with patch('evaluation.sql.azure_run.AzureControl') as control:
                with self.assertRaisesRegex(ValueError, 'already submitted'):
                    start(base, 'comparison-01')
                control.assert_not_called()

    def test_generated_scripts_parse_and_use_detached_driver_job(self):
        launch = launch_script({'run_id': 'comparison-01'})
        status = status_script('comparison-01')
        for script, delimiter in [(launch, 'SEBS_WRITE_RUN'), (status, 'SEBS_RUN_STATUS')]:
            result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            embedded = script.split("<<'" + delimiter + "'\n")[1].split('\n' + delimiter)[0]
            ast.parse(embedded)
        self.assertIn('systemd-run', launch)
        self.assertIn('exist_ok=False', launch)
        self.assertNotIn('AZURE_SECRET_PASSWORD', launch)

    def test_run_ids_cannot_escape_result_directory_or_inject_shell(self):
        for value in ['../outside', 'a/b', 'x;id', '', 'A', 'x'*49]:
            with self.assertRaises(ValueError):
                job_folder(Path('/tmp/fixture'), value)
            with self.assertRaises(ValueError):
                launch_script({'run_id': value})

    def test_full_plan_matches_aws_counts_and_rotates_backend_order(self):
        tasks = make_tasks('all')
        self.assertEqual(len(tasks), 24)
        self.assertEqual(sum(t['count'] for t in tasks), 2400)
        self.assertEqual(len({t['id'] for t in tasks}), 24)
        for workload, runs in [('moderate', 9), ('burst', 9), ('sparse', 6)]:
            self.assertEqual(sum(t['workload'] == workload for t in tasks), runs)
        self.assertEqual([t['backend'] for t in tasks[:3]], ['vm', 'functions', 'synapse'])
        self.assertEqual([t['backend'] for t in tasks[3:6]], ['functions', 'synapse', 'vm'])
        self.assertEqual(len(make_tasks('all', count=10, repetitions=1)), 9)
        with self.assertRaises(ValueError):
            make_tasks('all', count=7)
        with self.assertRaises(ValueError):
            make_tasks('sparse', repetitions=0)

    def test_derived_schedules_preserve_input_and_match_aws_intervals(self):
        namespace = self.namespace()
        source = {'input': {'scale_factor': 1, 'tables': {'lineitem': ['fixture']}},
                  'requests': [{'at': 0, 'query': 'q6'}], 'settings': {'max-in-flight': 1}}
        for name, interval in [('moderate', 2), ('burst', 0), ('sparse', 30)]:
            trace = namespace['workload_trace'](source, name, 100)
            self.assertEqual(trace['input'], source['input'])
            self.assertEqual(len(trace['requests']), 100)
            self.assertEqual(trace['requests'][-1]['at'], 99*interval)
            self.assertEqual(trace['settings']['max-in-flight'], 2)
            self.assertEqual(trace['requests'], trace['settings']['requests'])
            for query in ['q1', 'q3', 'q6', 'q12', 'q21']:
                self.assertEqual(sum(r['query'] == query for r in trace['requests']), 20)
        self.assertEqual(len(source['requests']), 1)
        self.assertEqual(source['settings']['max-in-flight'], 1)

    def test_repeated_job_passes_derived_trace_and_separate_paths_to_each_backend(self):
        namespace = self.namespace()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            settings = self.fixture(base)
            settings['tasks'] = make_tasks('burst', count=5, repetitions=2)
            fake = self.fake_child()
            traces = []
            def child(args, **kwargs):
                trace = json.loads(Path(args[args.index('--trace')+1]).read_text())
                traces.append(trace)
                return fake(args, **kwargs)
            with patch('azure.storage.blob.BlobServiceClient.from_connection_string'), \
                 patch('subprocess.run', side_effect=child) as process, redirect_stdout(io.StringIO()):
                self.assertEqual(namespace['execute'](settings, base), 0)
            self.assertEqual(process.call_count, 6)
            self.assertEqual(len({call.args[0][call.args[0].index('--output-dir')+1]
                                  for call in process.call_args_list}), 6)
            self.assertTrue(all(trace == traces[0] for trace in traces))
            self.assertTrue(all(r['at'] == 0 for r in traces[0]['requests']))
            state = json.loads((base/'runs/comparison-01/run.json').read_text())
            self.assertEqual(state['planned_runs'], 6)
            self.assertEqual(len(state['completed_backends']), 6)

    def test_another_active_job_is_rejected_before_backend_calls(self):
        import fcntl
        namespace = self.namespace()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            settings = self.fixture(base)
            (base/'runs').mkdir()
            with (base/'runs/.comparison.lock').open('a') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch('subprocess.run') as child:
                    with self.assertRaisesRegex(ValueError, 'already active'):
                        namespace['execute'](settings, base)
                    child.assert_not_called()

    def test_suite_summary_aggregates_repetitions_and_rejects_mismatched_inputs(self):
        namespace = self.namespace()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            out = job_folder(base, 'suite')
            out.mkdir(parents=True)
            tasks = make_tasks('moderate', count=5, repetitions=2)
            (out/'command.json').write_text(json.dumps({'settings': {'tasks': tasks}}))
            source = {'input': {'scale_factor': 1, 'tables': {'lineitem': ['same']}}, 'settings': {}}
            trace = namespace['workload_trace'](source, 'moderate', 5)
            for task in tasks:
                folder = out/task['id']
                folder.mkdir(parents=True)
                records = [{'index': i, 'valid': True, 'failure': False, 'total_latency_s': i+1,
                            'service_latency_s': i+1, 'queue_delay_s': 0, 'cold_start': False}
                           for i in range(5)]
                (folder/'requests.json').write_text(json.dumps(records))
                (folder/'trace.json').write_text(json.dumps({'trace': trace}))
                (folder/'summary.json').write_text(json.dumps({'submitted': 5, 'valid': 5,
                    'execution_failures': 0, 'observation_seconds': 10}))
            with redirect_stdout(io.StringIO()):
                summarize_suite(base, 'suite')
            report = json.loads((out/'comparison-summary.json').read_text())
            self.assertTrue(report['complete_and_valid'])
            for data in report['workloads']['moderate'].values():
                self.assertEqual(data['runs'], 2)
                self.assertEqual(data['submitted'], 10)
                self.assertEqual(data['mean_latency_s'], 3)
                self.assertEqual(data['p95_latency_s'], 5)
            changed = out/'moderate/repeat-2/functions/trace.json'
            other = json.loads(changed.read_text())
            other['trace']['input']['scale_factor'] = 10
            changed.write_text(json.dumps(other))
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'traces differ'):
                summarize_suite(base, 'suite')

    def test_percentile_matches_interpolated_tail(self):
        self.assertAlmostEqual(percentile([1, 2, 4], .95), 3.8)


if __name__ == '__main__':
    unittest.main()
