"""Connection-only probe checks using fake credentials and PostgreSQL clients."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evaluation.sql.postgres_connections import REMOTE, script, server_log_script


class ConnectionProbeTests(unittest.TestCase):
    def test_server_log_check_uses_bounded_window_and_reports_errors_and_allocation(self):
        source=server_log_script('2026-11-20T20:03:16+00:00','2026-11-20T20:06:16+00:00')
        result=subprocess.run(['bash','-n'],input=source,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        program=source.split("<<'SERVER_LOGS'\n")[1].split('\nSERVER_LOGS\n')[0]
        logs=SimpleNamespace(returncode=0,stdout='',stderr='ERROR: could not resize shared memory segment: No space left on device\nSTATEMENT: SELECT secret-looking-data\n')
        capture=io.StringIO()
        with patch('subprocess.run',return_value=logs) as run, \
             patch('subprocess.check_output',side_effect=[json.dumps([{'HostConfig':{'ShmSize':268435456,'Memory':6442450944}}]),'shm 256M']), \
             redirect_stdout(capture):
            exec(compile(program,'server-log-probe','exec'),{})
        args=run.call_args.args[0]
        self.assertIn('--since',args)
        self.assertIn('--until',args)
        self.assertIn('2026-11-20T20:03:16+00:00',args)
        self.assertIn('No space left on device',capture.getvalue())
        self.assertIn('268435456',capture.getvalue())
        self.assertNotIn('secret-looking-data',capture.getvalue())

    def test_probe_connects_without_querying_and_redacts_failure_password(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'comparison').mkdir()
            (root/'comparison/preflight.json').write_text(json.dumps({'region':'region','secret_arn':'arn'}))
            cert=root/'fixture.crt'
            cert.write_text('fixture certificate')
            settings={'host':'127.0.0.1','port':5432,'dbname':'db','user':'reader',
                      'password':'private-value','sslmode':'verify-full','sslrootcert':str(cert)}
            sql=SimpleNamespace(sql=lambda:SimpleNamespace(credentials=lambda:(dict(settings),False)))
            package=ModuleType('driver_function')
            package.sql=sql
            connection=Mock()
            pg=SimpleNamespace(connect=Mock(side_effect=[RuntimeError('TLS failed private-value'),connection,
                                                        connection,connection]))
            s3=Mock()
            def aws_client(service_name, *, region_name):
                self.assertEqual(service_name,'s3')
                self.assertEqual(region_name,'region')
                return s3
            boto=SimpleNamespace(client=Mock(side_effect=aws_client))
            payload={'rounds':2,'workers':2,'bucket':'bucket','key':'reports/probe.json'}
            program=REMOTE.replace('__PAYLOAD__',repr(payload)).replace(
                '/opt/sebs-pg-driver/connection-checks',str(root/'checkpoints'))
            old=os.getcwd()
            try:
                os.chdir(root)
                with patch.dict('sys.modules',{'boto3':boto,'psycopg':pg,'driver_function':package}), \
                     patch.dict(os.environ,{},clear=False),redirect_stdout(io.StringIO()):
                    exec(compile(program,'connection-probe','exec'),{})
            finally:
                os.chdir(old)
            raw=s3.put_object.call_args.kwargs['Body'].decode()
            report=json.loads(raw)
            self.assertEqual(report['attempts'],4)
            self.assertEqual(report['successful_connections'],3)
            self.assertEqual(report['benchmark_queries_executed'],0)
            self.assertNotIn('private-value',raw)
            self.assertIn('[REDACTED]',raw)
            self.assertEqual(pg.connect.call_count,4)
            connection.execute.assert_not_called()
            connection.cursor.assert_not_called()
            self.assertEqual(connection.close.call_count,3)
            saved=json.loads((root/'checkpoints/probe/report.json').read_text())
            self.assertEqual(saved,report)

    def test_upload_failure_preserves_completed_report_on_driver_and_in_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'comparison').mkdir()
            (root/'comparison/preflight.json').write_text(json.dumps({'region':'region','secret_arn':'arn'}))
            cert=root/'fixture.crt'
            cert.write_text('fixture certificate')
            package=ModuleType('driver_function')
            package.sql=SimpleNamespace(sql=lambda:SimpleNamespace(credentials=lambda:({
                'host':'127.0.0.1','sslrootcert':str(cert),'password':'private-value'},False)))
            connection=Mock()
            pg=SimpleNamespace(connect=Mock(return_value=connection))
            s3=Mock()
            s3.put_object.side_effect=RuntimeError('upload failed')
            def aws_client(service_name, *, region_name):
                return s3
            boto=SimpleNamespace(client=aws_client)
            payload={'rounds':1,'workers':2,'bucket':'bucket','key':'reports/upload-failed.json'}
            program=REMOTE.replace('__PAYLOAD__',repr(payload)).replace(
                '/opt/sebs-pg-driver/connection-checks',str(root/'checkpoints'))
            old=os.getcwd()
            output=io.StringIO()
            try:
                os.chdir(root)
                with patch.dict('sys.modules',{'boto3':boto,'psycopg':pg,'driver_function':package}), \
                     patch.dict(os.environ,{},clear=False),redirect_stdout(output):
                    with self.assertRaisesRegex(RuntimeError,'upload failed'):
                        exec(compile(program,'connection-probe','exec'),{})
            finally:
                os.chdir(old)
            report=json.loads((root/'checkpoints/upload-failed/report.json').read_text())
            self.assertEqual(report['successful_connections'],2)
            self.assertIn('"benchmark_queries_executed": 0',output.getvalue())
            self.assertIn('REPORT_SAVED_ON_DRIVER',output.getvalue())
            self.assertNotIn('private-value',output.getvalue())
            pg.connect.assert_called()
            connection.execute.assert_not_called()

    def test_probe_command_shell_syntax_and_no_embedded_credentials(self):
        source=script({'rounds':3,'workers':8,'bucket':'bucket','key':'reports/test.json'})
        result=subprocess.run(['bash','-n'],input=source,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY',source)
        self.assertNotIn('admin.json',source)
        self.assertNotIn('cursor.execute',source)


if __name__=='__main__':
    unittest.main()
